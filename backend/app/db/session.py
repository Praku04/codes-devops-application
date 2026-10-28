"""SQLite persistence via SQLAlchemy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, String, Text, create_engine, func, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.core.config import get_settings
from app.core.state_machine import Platform, ProjectPath, ProjectState, blockers_for
from app.models.schemas import (
    InterviewQuestion,
    ProjectBrief,
    ProjectPlan,
    ProjectStatus,
    SessionSummary,
    UploadedDocument,
)

# India Standard Time (no DST) — session timestamps are exposed in IST for the UI.
IST = timezone(timedelta(hours=5, minutes=30))


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="AgentCraft User")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")  # user | admin
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeletedUserRow(Base):
    """
    Archive of an account the super admin removed.

    Two jobs. It explains the removal — without it a deleted person would see the
    generic "invalid email or password", and a stale JWT would be indistinguishable
    from a forged one. And it holds everything needed to put the account back:
    password hash, role, and the status held at deletion, so **Restore** returns the
    person to exactly where they were rather than making them sign up afresh.

    Their projects are deliberately *not* deleted. Keeping the rows with the original
    `user_id` means restoring the user reattaches every workspace for free; nothing can
    reach them meanwhile because auth requires a live `users` row. Purge deletes both.
    """

    __tablename__ = "deleted_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Restore fodder. Nullable because round-7 tombstones predate these columns —
    # those rows can still be purged, and restore reports why it cannot proceed.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    user_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="Untitled Project")
    state: Mapped[str] = mapped_column(String(40), default=ProjectState.CREATED.value)
    path: Mapped[str | None] = mapped_column(String(40), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(40), nullable=True)
    brief_json: Mapped[str] = mapped_column(Text, default="{}")
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    interview_json: Mapped[str] = mapped_column(Text, default="[]")
    # The approved process blueprint (SIPOC / flow / swimlane and the model behind them).
    # Nullable so every project created before this column stays valid — and so "no
    # diagrams" remains a real state that generates exactly as it always did.
    diagrams_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


_engine = None
_SessionLocal = None
_generation_progress: dict[str, str] = {}


def set_progress(project_id: str, message: str) -> None:
    _generation_progress[project_id] = message


def clear_progress(project_id: str) -> None:
    _generation_progress.pop(project_id, None)


def get_progress(project_id: str) -> str | None:
    return _generation_progress.get(project_id)


def _migrate_schema(engine) -> None:
    """Add columns for existing SQLite DBs (create_all does not alter)."""
    with engine.begin() as conn:
        project_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(projects)")).fetchall()
        }
        if "user_id" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN user_id VARCHAR(36)"))
        if "diagrams_json" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN diagrams_json TEXT"))

        # Round-7 tombstones only explained the deletion; restore needs the account back.
        deleted_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(deleted_users)")).fetchall()
        }
        if deleted_cols:  # table absent on a fresh DB — create_all already made it
            for name, ddl in (
                ("password_hash", "ALTER TABLE deleted_users ADD COLUMN password_hash VARCHAR(255)"),
                ("role", "ALTER TABLE deleted_users ADD COLUMN role VARCHAR(20)"),
                ("status", "ALTER TABLE deleted_users ADD COLUMN status VARCHAR(20)"),
                ("user_created_at", "ALTER TABLE deleted_users ADD COLUMN user_created_at DATETIME"),
            ):
                if name not in deleted_cols:
                    conn.execute(text(ddl))

        user_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
        if "role" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user'"))
        if "status" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN status VARCHAR(20) DEFAULT 'pending'"))
            # Existing accounts stay usable after upgrade.
            conn.execute(text("UPDATE users SET status = 'approved' WHERE status IS NULL OR status = ''"))
            conn.execute(text("UPDATE users SET role = 'user' WHERE role IS NULL OR role = ''"))


def seed_super_admin() -> None:
    """Ensure the configured super admin exists and is approved."""
    from app.core.security import hash_password

    settings = get_settings()
    email = (settings.super_admin_email or "admin@ac.com").strip().lower()
    password = settings.super_admin_password or "1681149@sPk"
    name = settings.super_admin_name or "Super Admin"
    with get_session() as db:
        row = db.query(UserRow).filter(func.lower(UserRow.email) == email).first()
        if row:
            row.role = "admin"
            row.status = "approved"
            row.name = name[:120]
            # Keep password if already set unless force reset via empty check — update hash only when mismatched login path needs seed.
            if not verify_or_set_admin_password(row, password):
                row.password_hash = hash_password(password)
            db.commit()
            return
        db.add(
            UserRow(
                id=str(uuid4()),
                email=email,
                name=name[:120],
                password_hash=hash_password(password),
                role="admin",
                status="approved",
                created_at=_now(),
            )
        )
        db.commit()


DELETED_ACCOUNT_MESSAGE = (
    "Your account was deleted by the admin. "
    "You no longer have access to AgentCraft — sign up again or contact support."
)


def tombstone_for(row: UserRow, *, deleted_by: str | None = None) -> DeletedUserRow:
    """
    Archive a user about to be removed, complete enough to restore them.

    Returned rather than committed so the caller can merge it into the *same*
    session as the delete — a second SQLite connection writing while the first holds
    a read transaction can hit "database is locked".
    """
    return DeletedUserRow(
        id=row.id,
        email=(row.email or "").strip().lower(),
        name=row.name or "",
        deleted_at=_now(),
        deleted_by=deleted_by,
        password_hash=row.password_hash,
        role=getattr(row, "role", None) or "user",
        status=getattr(row, "status", None) or "pending",
        user_created_at=row.created_at,
    )


def was_email_deleted(email: str) -> bool:
    """True while the tombstone stands — cleared when the person signs up again."""
    target = (email or "").strip().lower()
    if not target:
        return False
    with get_session() as db:
        return (
            db.query(DeletedUserRow).filter(func.lower(DeletedUserRow.email) == target).first()
            is not None
        )


def was_user_deleted(user_id: str) -> bool:
    """True when this JWT's subject names an account the admin removed."""
    if not user_id:
        return False
    with get_session() as db:
        return db.get(DeletedUserRow, user_id) is not None


def clear_deleted_tombstone(email: str) -> None:
    """
    A fresh signup supersedes the tombstone — otherwise the new account looks deleted.

    The old workspaces are dropped with it: they belong to the previous account's id, so
    once that id can never be restored nothing will ever reach them again.
    """
    target = (email or "").strip().lower()
    if not target:
        return
    with get_session() as db:
        rows = db.query(DeletedUserRow).filter(func.lower(DeletedUserRow.email) == target).all()
        if not rows:
            return
        for row in rows:
            for p in db.query(ProjectRow).filter(ProjectRow.user_id == row.id).all():
                db.delete(p)
            db.delete(row)
        db.commit()


def restore_user(user_id: str) -> tuple[UserRow, int] | None:
    """
    Put a deleted account back, with the status it had when it was removed.

    Returns (restored row, reattached workspace count), or None when there is no
    archive for this id. Raises ValueError when the archive predates the restore
    columns or the email has since been taken by a new signup — the caller turns those
    into a 4xx rather than silently creating a broken account.
    """
    if not user_id:
        return None
    with get_session() as db:
        archived = db.get(DeletedUserRow, user_id)
        if not archived:
            return None
        if not archived.password_hash:
            raise ValueError(
                "This account was deleted by an older version that did not keep enough "
                "to restore it. Ask the person to sign up again."
            )
        email = (archived.email or "").strip().lower()
        taken = db.query(UserRow).filter(func.lower(UserRow.email) == email).first()
        if taken:
            raise ValueError(f"{email} is in use by another account — cannot restore.")

        row = UserRow(
            id=archived.id,
            email=email,
            name=archived.name or "AgentCraft User",
            password_hash=archived.password_hash,
            role=archived.role or "user",
            status=archived.status or "pending",
            created_at=archived.user_created_at or _now(),
        )
        db.add(row)
        # The projects were never deleted, so they reattach by id alone.
        reattached = db.query(ProjectRow).filter(ProjectRow.user_id == archived.id).count()
        db.delete(archived)
        db.commit()
        db.refresh(row)
        return row, reattached


def purge_deleted_user(user_id: str) -> tuple[str, int] | None:
    """
    Erase a deleted account for good — archive plus the workspaces it still owns.

    Returns (email, purged workspace count), or None when there is no archive.
    """
    if not user_id:
        return None
    with get_session() as db:
        archived = db.get(DeletedUserRow, user_id)
        if not archived:
            return None
        email = archived.email
        projects = db.query(ProjectRow).filter(ProjectRow.user_id == archived.id).all()
        for p in projects:
            db.delete(p)
        db.delete(archived)
        db.commit()
        return email, len(projects)


def verify_or_set_admin_password(row: UserRow, password: str) -> bool:
    """Return True if current hash already matches password."""
    from app.core.security import verify_password

    try:
        return verify_password(password, row.password_hash)
    except Exception:
        return False


def _safe_db_target(url: str) -> str:
    """`postgresql://host:5432/db` — the part of a DSN that is safe to print.

    A connection failure has to name the host or the message is useless, and a DSN carries a
    password, so it can never be echoed whole. `make_url` drops the credentials for us.
    """
    try:
        from sqlalchemy.engine import make_url

        u = make_url(url)
        target = u.host or ""
        if u.port:
            target = f"{target}:{u.port}"
        if u.database and not url.startswith("sqlite"):
            target = f"{target}/{u.database}"
        return f"{u.drivername}://{target}" if target else u.drivername
    except Exception:  # noqa: BLE001
        # Never let the diagnostic be the thing that crashes startup.
        return url.split("://", 1)[0] + "://…"


def init_db() -> None:
    global _engine, _SessionLocal
    settings = get_settings()
    url = settings.database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    _engine = create_engine(url, connect_args=connect_args)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    try:
        # First actual connection. A wrong or unreachable `DATABASE_URL` surfaces here, and
        # SQLAlchemy's own report is a ~40-line traceback ending in `OperationalError` — which
        # says what broke inside the driver but not that the answer is one line of `.env`. This
        # is the only place that knows both, so it says it here rather than leaving the operator
        # to read a stack trace at startup.
        Base.metadata.create_all(_engine)
    except OperationalError as exc:
        target = _safe_db_target(url)
        raise RuntimeError(
            f"Cannot reach the database at {target} — the API cannot start.\n"
            f"  driver said: {str(getattr(exc, 'orig', exc)).strip().splitlines()[0]}\n"
            "  DATABASE_URL in the repo-root .env is what points here. Check, in this order:\n"
            "    1. Is it the right database? The default is sqlite:///./agentcraft.db, which "
            "needs no server — a Postgres URL left over from another project is the usual cause "
            "of this exact error.\n"
            "    2. If the host name did not resolve at all, the endpoint is wrong, deleted, or "
            "private to a VPC this machine is not in.\n"
            "    3. If it resolved but refused or timed out, check the security group, the port, "
            "and that the instance is running.\n"
            "  To fall back to the local file database: set DATABASE_URL=sqlite:///./agentcraft.db "
            "and restart. Nothing else in the app needs changing."
        ) from exc
    if url.startswith("sqlite"):
        _migrate_schema(_engine)
    seed_super_admin()
    repair_agent_goals_bullets()
    backfill_brief_documents()


def backfill_brief_documents() -> int:
    """
    Give pre-upgrade projects a document list.

    Uploads used to be stored only as concatenated text with a "### filename"
    header per file. Those headers are enough to reconstruct the filename and
    extracted size, so existing sessions show their documents too. Byte size is
    unknown for historical uploads and stays 0. Idempotent.
    """
    from app.services.parser.documents import guess_content_type

    fixed = 0
    with get_session() as db:
        rows = db.query(ProjectRow).all()
        for row in rows:
            try:
                brief = ProjectBrief.model_validate(_loads(row.brief_json, {}))
            except Exception:
                continue
            if not brief.document_text:
                continue
            # Re-derive when a stored list contains entries that are not filenames:
            # an earlier version of this backfill mistook a document's own "### "
            # markdown headings for uploads, so those rows need correcting.
            if brief.documents and all(_looks_like_filename(d.filename) for d in brief.documents):
                continue
            docs = _documents_from_document_text(brief.document_text, guess_content_type)
            if not docs or docs == brief.documents:
                continue
            row.brief_json = brief.model_copy(update={"documents": docs}).model_dump_json()
            row.updated_at = _now()
            fixed += 1
        if fixed:
            db.commit()
    return fixed


def _looks_like_filename(name: str) -> bool:
    """
    True for "brd.md" / "SmartTutor-Guide.docx", false for "2.1 Problem Statement".

    Upload headers are always a bare filename with a supported extension, so an
    extension check plus "no spaces" separates them from document headings.
    """
    from app.services.parser.documents import ALLOWED_EXTENSIONS

    candidate = name.strip()
    if not candidate or " " in candidate:
        return False
    return Path(candidate).suffix.lower() in ALLOWED_EXTENSIONS


def _documents_from_document_text(document_text: str, content_type) -> list[UploadedDocument]:
    """
    Parse "### filename" sections back into document records.

    Only headers that name an actual upload start a new section — a document's own
    "### 2.1 Problem Statement" heading is body text and stays with its file.
    """
    sections: list[tuple[str, list[str]]] = []
    for line in document_text.split("\n"):
        if line.startswith("### ") and _looks_like_filename(line[4:]):
            sections.append((line[4:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return [
        UploadedDocument(
            filename=name,
            size_bytes=0,  # original byte size was never recorded
            content_type=content_type(name),
            chars_extracted=len("\n".join(body).strip()),
        )
        for name, body in sections
        if name
    ]


def repair_agent_goals_bullets() -> int:
    """Rewrite ## Goals to bullets on all stored plans (idempotent)."""
    from app.services.deduction.goals_format import normalize_plan_agent_goals

    fixed = 0
    with get_session() as db:
        rows = db.query(ProjectRow).filter(ProjectRow.plan_json.isnot(None)).all()
        for row in rows:
            try:
                plan = ProjectPlan.model_validate(_loads(row.plan_json, None))
            except Exception:
                continue
            if not plan:
                continue
            normalized = normalize_plan_agent_goals(plan)
            if normalized is plan:
                continue
            row.plan_json = normalized.model_dump_json()
            row.updated_at = _now()
            fixed += 1
        if fixed:
            db.commit()
    return fixed


def get_session():
    if _SessionLocal is None:
        init_db()
    return _SessionLocal()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_ist(dt: datetime | None) -> str | None:
    """Serialize datetimes as India Standard Time (UTC+05:30) ISO-8601."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # SQLite often returns naive values that were stored as UTC
        aware = dt.replace(tzinfo=timezone.utc)
    else:
        aware = dt
    return aware.astimezone(IST).isoformat()


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    return json.loads(raw)


def row_to_status(row: ProjectRow) -> ProjectStatus:
    brief = ProjectBrief.model_validate(_loads(row.brief_json, {}))
    plan = ProjectPlan.model_validate(_loads(row.plan_json, None)) if row.plan_json else None
    interview = [InterviewQuestion.model_validate(q) for q in _loads(row.interview_json, [])]
    state = ProjectState(row.state)
    platform = Platform(row.platform) if row.platform else None
    path = ProjectPath(row.path) if row.path else None
    # Flags only, read straight off the JSON. Validating the whole DiagramSet here would run
    # on every status poll, and a schema change would then break loading an old project
    # rather than just its blueprint tab.
    diagrams_raw = _loads(getattr(row, "diagrams_json", None), None) or {}
    return ProjectStatus(
        id=row.id,
        name=row.name,
        state=state,
        path=path,
        platform=platform,
        blockers=blockers_for(state, has_platform=platform is not None, has_plan=plan is not None),
        brief=brief,
        plan=plan,
        interview=interview,
        error=row.error,
        progress=_generation_progress.get(row.id),
        created_at=_iso_ist(row.created_at),
        updated_at=_iso_ist(row.updated_at),
        user_id=getattr(row, "user_id", None),
        has_diagrams=bool(diagrams_raw),
        diagrams_frozen=bool(diagrams_raw.get("frozen")),
        diagram_version=int(diagrams_raw.get("version") or 0),
    )


def row_to_session_summary(row: ProjectRow) -> SessionSummary:
    plan = ProjectPlan.model_validate(_loads(row.plan_json, None)) if row.plan_json else None
    brief = ProjectBrief.model_validate(_loads(row.brief_json, {}))
    platform = Platform(row.platform) if row.platform else None
    agent_names = [a.name for a in plan.agents] if plan else []
    skill_names = [s.name for s in plan.skills] if plan else []
    rule_names: list[str] = []
    if plan and platform:
        # Rules are derived at export time, so a plan can carry `rules: []` — always for a
        # Claude project generated before they existed, and on any IDE if the LLM returned
        # none. Resolve them the way the exporter does or the summary would report 0 rules
        # beside a populated rules folder.
        from app.services.deduction.project_rules import ensure_project_rules

        rule_names = [r.name for r in ensure_project_rules(plan, platform)]
    elif plan:
        rule_names = [r.name for r in plan.rules]
    summary = None
    if plan and plan.summary:
        summary = plan.summary[:280]
    elif brief.problem_statement:
        summary = brief.problem_statement.strip()[:280]
    return SessionSummary(
        id=row.id,
        name=row.name,
        state=ProjectState(row.state),
        platform=platform,
        summary=summary,
        agent_count=len(agent_names),
        skill_count=len(skill_names),
        rule_count=len(rule_names),
        agent_names=agent_names,
        skill_names=skill_names,
        rule_names=rule_names,
        document_names=[d.filename for d in brief.documents],
        created_at=_iso_ist(row.created_at),
        updated_at=_iso_ist(row.updated_at),
    )


class ProjectRepository:
    def create(self, name: str = "Untitled Project", *, user_id: str | None = None) -> ProjectStatus:
        with get_session() as db:
            now = _now()
            row = ProjectRow(
                id=str(uuid4()),
                user_id=user_id,
                name=name,
                state=ProjectState.CREATED.value,
                brief_json=ProjectBrief().model_dump_json(),
                interview_json="[]",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row_to_status(row)

    def get(self, project_id: str) -> ProjectStatus | None:
        with get_session() as db:
            row = db.get(ProjectRow, project_id)
            return row_to_status(row) if row else None

    def get_owned(self, project_id: str, user_id: str) -> ProjectStatus | None:
        with get_session() as db:
            row = db.get(ProjectRow, project_id)
            if not row or row.user_id != user_id:
                return None
            return row_to_status(row)

    def list(self, user_id: str | None = None) -> list[ProjectStatus]:
        with get_session() as db:
            q = db.query(ProjectRow)
            if user_id is not None:
                q = q.filter(ProjectRow.user_id == user_id)
            rows = q.order_by(ProjectRow.updated_at.desc()).all()
            return [row_to_status(r) for r in rows]

    def list_sessions(self, user_id: str) -> list[SessionSummary]:
        with get_session() as db:
            rows = (
                db.query(ProjectRow)
                .filter(ProjectRow.user_id == user_id)
                .order_by(ProjectRow.updated_at.desc())
                .all()
            )
            return [row_to_session_summary(r) for r in rows]

    def update(
        self,
        project_id: str,
        *,
        state: ProjectState | None = None,
        path: ProjectPath | None = None,
        platform: Platform | None = None,
        brief: ProjectBrief | None = None,
        plan: ProjectPlan | None = None,
        interview: list[InterviewQuestion] | None = None,
        error: str | None = None,
        clear_error: bool = False,
        clear_path: bool = False,
        clear_platform: bool = False,
        name: str | None = None,
    ) -> ProjectStatus:
        with get_session() as db:
            row = db.get(ProjectRow, project_id)
            if not row:
                raise KeyError(project_id)
            if state is not None:
                row.state = state.value
            if clear_path:
                row.path = None
            elif path is not None:
                row.path = path.value
            if clear_platform:
                row.platform = None
            elif platform is not None:
                row.platform = platform.value
            if brief is not None:
                row.brief_json = brief.model_dump_json()
            if plan is not None:
                row.plan_json = plan.model_dump_json()
            if interview is not None:
                row.interview_json = json.dumps([q.model_dump() for q in interview])
            if clear_error:
                row.error = None
            elif error is not None:
                row.error = error
            if name is not None:
                row.name = name
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return row_to_status(row)

    # Diagrams get their own accessors rather than a field on ProjectStatus: the process
    # model runs to tens of kilobytes and ProjectStatus is polled every second or two while
    # generation runs. Callers that need it ask for it.
    def get_diagrams(self, project_id: str) -> "DiagramSet | None":
        from app.models.diagrams import DiagramSet

        with get_session() as db:
            row = db.get(ProjectRow, project_id)
            raw = getattr(row, "diagrams_json", None) if row else None
            if not raw:
                return None
            try:
                return DiagramSet.model_validate(json.loads(raw))
            except Exception:
                # A set written by an older shape should not make the project unopenable —
                # the user regenerates the blueprint instead.
                return None

    def set_diagrams(self, project_id: str, diagrams: "DiagramSet") -> ProjectStatus:
        with get_session() as db:
            row = db.get(ProjectRow, project_id)
            if not row:
                raise KeyError(project_id)
            row.diagrams_json = diagrams.model_dump_json()
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return row_to_status(row)

    def clear_diagrams(self, project_id: str) -> ProjectStatus:
        with get_session() as db:
            row = db.get(ProjectRow, project_id)
            if not row:
                raise KeyError(project_id)
            row.diagrams_json = None
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return row_to_status(row)

    def delete_owned(self, project_id: str, user_id: str) -> bool:
        with get_session() as db:
            row = db.get(ProjectRow, project_id)
            if not row or row.user_id != user_id:
                return False
            db.delete(row)
            db.commit()
            return True


repo = ProjectRepository()
