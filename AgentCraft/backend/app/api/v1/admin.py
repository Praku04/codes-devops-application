"""Admin dashboard + approve / reject / delete / restore / purge users."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.core.deps import get_current_admin
from app.core.exceptions import AppError
from app.core.state_machine import Platform, ProjectPath
from app.db.session import (
    DeletedUserRow,
    ProjectRow,
    UserRow,
    _iso_ist,
    get_session,
    purge_deleted_user,
    restore_user,
    tombstone_for,
)
from app.models.diagrams import DiagramSet, DiagramSetView
from app.models.schemas import (
    AdminSessionInfo,
    AdminUserView,
    DeleteUserResponse,
    ProjectBrief,
    ProjectPlan,
    PurgeUserResponse,
    RestoreUserResponse,
    UserOut,
)
from app.services.export import documents as export_documents
from app.services.projects import project_service

router = APIRouter(prefix="/admin")


def _pipeline(row: ProjectRow, *, documents: int, answers: int) -> tuple[str | None, bool]:
    """
    Which context pipeline this session took, and whether we had to work it out.

    `projects.path` is nullable — it has to be, because every session created before the
    two paths existed has no value there, and because a session that was abandoned at the
    fork never chose one. Reporting all of those as "not chosen" would be wrong for the
    first group: they did follow a pipeline, and what they hold says which. So a recorded
    path is used as-is, and otherwise the evidence decides — with `inferred` set, so the
    panel can say "documents (inferred)" rather than claiming the row said so.

    Documents are checked first: on a session that has both (a document upload followed by
    the interview's follow-up questions) the documents are what the brief was built from.
    """
    recorded = (row.path or "").strip().lower()
    try:
        return ProjectPath(recorded).value, False
    except ValueError:
        pass
    if documents:
        return ProjectPath.DOCS.value, True
    if answers:
        return ProjectPath.INTERVIEW.value, True
    return None, False


def _session_info(row: ProjectRow) -> AdminSessionInfo:
    platform = None
    platform_enum: Platform | None = None
    if row.platform:
        try:
            platform_enum = Platform(row.platform)
            platform = platform_enum.value
        except ValueError:
            platform = row.platform

    # Uploaded filenames live on the brief, not the plan, so they survive even when a
    # session was abandoned before generation — which is exactly when an admin wants to
    # know what was handed in. The interview answers sit beside them, and are the same
    # kind of evidence for the other path.
    document_names: list[str] = []
    answer_count = 0
    if row.brief_json:
        try:
            brief = ProjectBrief.model_validate(json.loads(row.brief_json))
            document_names = [d.filename for d in brief.documents]
            answer_count = len([v for v in brief.interview_answers.values() if (v or "").strip()])
        except Exception:
            pass

    path, path_inferred = _pipeline(row, documents=len(document_names), answers=answer_count)

    # Blueprint metadata straight off the stored set. Deliberately *not* through
    # `project_service.diagrams()`: that renders all three PNGs to report their pixel
    # sizes, which would put a matplotlib draw per session into every /admin/users call.
    has_diagrams = False
    diagrams_frozen = False
    diagram_version = 0
    diagram_title: str | None = None
    diagram_steps = 0
    diagram_actors = 0
    diagram_revisions = 0
    diagram_last_scope: str | None = None
    if row.diagrams_json:
        try:
            dset = DiagramSet.model_validate(json.loads(row.diagrams_json))
            has_diagrams = True
            diagrams_frozen = dset.frozen
            diagram_version = dset.version
            diagram_title = dset.model.title or None
            diagram_steps = len(dset.model.steps)
            diagram_actors = len(dset.model.actors)
            diagram_revisions = len(dset.revisions)
            # v1 is the first draw, not a change, so it is not a "last scope" — a blueprint
            # nobody revised should say nothing rather than claim an all-views edit.
            followups = [r for r in dset.revisions if r.version > 1]
            diagram_last_scope = followups[-1].scope if followups else None
        except Exception:
            pass

    agent_names: list[str] = []
    skill_names: list[str] = []
    rule_names: list[str] = []
    if row.plan_json:
        try:
            plan = ProjectPlan.model_validate(json.loads(row.plan_json))
            agent_names = [a.name for a in plan.agents]
            skill_names = [s.name for s in plan.skills]
            if platform_enum:
                # Derived at export time, so a plan can carry `rules: []` — an older Claude
                # plan always does, and any IDE does when the LLM returned none.
                from app.services.deduction.project_rules import ensure_project_rules

                rule_names = [r.name for r in ensure_project_rules(plan, platform_enum)]
            else:
                rule_names = [r.name for r in plan.rules]
        except Exception:
            pass

    return AdminSessionInfo(
        id=row.id,
        name=row.name,
        state=row.state,
        platform=platform,
        updated_at=_iso_ist(row.updated_at),
        agent_count=len(agent_names),
        skill_count=len(skill_names),
        rule_count=len(rule_names),
        agent_names=agent_names,
        skill_names=skill_names,
        rule_names=rule_names,
        document_names=document_names,
        path=path,
        path_inferred=path_inferred,
        interview_answer_count=answer_count,
        has_diagrams=has_diagrams,
        diagrams_frozen=diagrams_frozen,
        diagram_version=diagram_version,
        diagram_title=diagram_title,
        diagram_step_count=diagram_steps,
        diagram_actor_count=diagram_actors,
        diagram_revision_count=diagram_revisions,
        diagram_last_scope=diagram_last_scope,
        # A plan means SDD.md and its three architectural views exist. Read off the column
        # rather than rendered here, for the same reason as the blueprint metadata above:
        # `architecture_png` draws with matplotlib, and doing that per session would put nine
        # renders per user into every /admin/users call.
        has_plan=bool(row.plan_json),
    )


def _user_view(u: UserRow, projects: list[ProjectRow]) -> AdminUserView:
    return AdminUserView(
        id=u.id,
        email=u.email,
        name=u.name,
        role=getattr(u, "role", None) or "user",
        status=getattr(u, "status", None) or "pending",
        created_at=_iso_ist(u.created_at),
        session_count=len(projects),
        sessions=[_session_info(p) for p in projects],
    )


def _deleted_user_view(d: DeletedUserRow, projects: list[ProjectRow]) -> AdminUserView:
    """
    Render an archived account as a card the admin can see and restore.

    `status` is the synthetic "deleted" so the UI has one field to filter on; the real
    status the account held is carried separately and comes back on restore.
    """
    return AdminUserView(
        id=d.id,
        email=d.email,
        name=d.name or "",
        role=d.role or "user",
        status="deleted",
        created_at=_iso_ist(d.user_created_at),
        session_count=len(projects),
        sessions=[_session_info(p) for p in projects],
        deleted_at=_iso_ist(d.deleted_at),
        deleted_by=d.deleted_by,
        previous_status=d.status,
        restorable=bool(d.password_hash),
    )


def _projects_of(db, user_id: str) -> list[ProjectRow]:
    return (
        db.query(ProjectRow)
        .filter(ProjectRow.user_id == user_id)
        .order_by(ProjectRow.updated_at.desc())
        .all()
    )


@router.get("/users", response_model=list[AdminUserView])
def list_users(_admin: UserOut = Depends(get_current_admin)) -> list[AdminUserView]:
    """
    List all users with session artifact names (read-only).

    Deleted accounts are included with `status: "deleted"` — otherwise the admin has no
    way to see who was removed, or to restore them.
    """
    with get_session() as db:
        out: list[AdminUserView] = []
        for u in db.query(UserRow).order_by(UserRow.created_at.desc()).all():
            out.append(_user_view(u, _projects_of(db, u.id)))
        for d in db.query(DeletedUserRow).order_by(DeletedUserRow.deleted_at.desc()).all():
            out.append(_deleted_user_view(d, _projects_of(db, d.id)))
        return out


@router.post("/users/{user_id}/approve", response_model=AdminUserView)
def approve_user(
    user_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> AdminUserView:
    with get_session() as db:
        row = db.get(UserRow, user_id)
        if not row:
            raise AppError("User not found", status_code=404)
        if (getattr(row, "role", None) or "user") == "admin":
            raise AppError("Cannot change admin status this way", status_code=400)
        row.status = "approved"
        db.commit()
        db.refresh(row)
        projects = (
            db.query(ProjectRow)
            .filter(ProjectRow.user_id == row.id)
            .order_by(ProjectRow.updated_at.desc())
            .all()
        )
        return _user_view(row, projects)


@router.delete("/users/{user_id}", response_model=DeleteUserResponse)
def delete_user(
    user_id: str,
    admin: UserOut = Depends(get_current_admin),
) -> DeleteUserResponse:
    """
    Revoke an account (any status, including approved) and archive it.

    The `users` row goes, so every login and every existing token stops working at
    once. The archive row keeps the password hash and prior status so **restore** can
    put the person back, and lets login say "deleted by the admin" instead of a generic
    credentials error.

    Their workspaces are kept, still owned by the archived id — deleting them would make
    restore worthless, and nothing can read them while the user row is absent. `purge`
    is the endpoint that erases both.
    """
    with get_session() as db:
        row = db.get(UserRow, user_id)
        if not row:
            raise AppError("User not found", status_code=404)
        if (getattr(row, "role", None) or "user") == "admin":
            raise AppError("The super admin account cannot be deleted", status_code=400)
        email = row.email
        name = row.name
        kept_sessions = db.query(ProjectRow).filter(ProjectRow.user_id == row.id).count()
        # Archive first, while the email/name/hash are still readable.
        db.merge(tombstone_for(row, deleted_by=admin.email))
        db.delete(row)
        db.commit()

    return DeleteUserResponse(
        id=user_id,
        email=email,
        name=name,
        deleted_sessions=kept_sessions,
        message=(
            f"Deleted {name or email}. They can no longer log in, and their "
            f"{kept_sessions} workspace{'' if kept_sessions == 1 else 's'} "
            f"{'is' if kept_sessions == 1 else 'are'} held for restore."
        ),
    )


@router.post("/users/{user_id}/restore", response_model=RestoreUserResponse)
def restore_deleted_user(
    user_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> RestoreUserResponse:
    """
    Undo a delete — the account returns with its old password, status, and workspaces.

    Same password as before: the hash was archived, never re-derived, so the person logs
    in with the credentials they already had.
    """
    try:
        result = restore_user(user_id)
    except ValueError as exc:
        raise AppError(str(exc), status_code=400) from exc
    if result is None:
        raise AppError("No deleted user with this id", status_code=404)

    row, restored_sessions = result
    with get_session() as db:
        fresh = db.get(UserRow, row.id)
        view = _user_view(fresh or row, _projects_of(db, row.id))

    status_note = (
        "they can log in now"
        if view.status == "approved"
        else f"status is back to {view.status}"
    )
    return RestoreUserResponse(
        user=view,
        restored_sessions=restored_sessions,
        message=(
            f"Restored {view.name or view.email} with "
            f"{restored_sessions} workspace{'' if restored_sessions == 1 else 's'} — "
            f"{status_note}."
        ),
    )


@router.delete("/users/{user_id}/purge", response_model=PurgeUserResponse)
def purge_user(
    user_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> PurgeUserResponse:
    """
    Erase a deleted account permanently — the archive and every workspace it held.

    After this the id cannot be restored, and the person is no longer told their account
    was deleted; they get the generic credentials error like any unknown email.
    """
    result = purge_deleted_user(user_id)
    if result is None:
        raise AppError("No deleted user with this id", status_code=404)

    email, purged = result
    return PurgeUserResponse(
        id=user_id,
        email=email,
        purged_sessions=purged,
        message=(
            f"Permanently erased {email} and {purged} "
            f"workspace{'' if purged == 1 else 's'}. This cannot be restored."
        ),
    )


@router.post("/users/{user_id}/reject", response_model=AdminUserView)
def reject_user(
    user_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> AdminUserView:
    with get_session() as db:
        row = db.get(UserRow, user_id)
        if not row:
            raise AppError("User not found", status_code=404)
        if (getattr(row, "role", None) or "user") == "admin":
            raise AppError("Cannot reject the admin account", status_code=400)
        row.status = "rejected"
        db.commit()
        db.refresh(row)
        projects = (
            db.query(ProjectRow)
            .filter(ProjectRow.user_id == row.id)
            .order_by(ProjectRow.updated_at.desc())
            .all()
        )
        return _user_view(row, projects)


# ── Read-only blueprint access for the admin panel ──────────────────────────────
#
# These duplicate three of /projects' endpoints for one reason: every handler there begins
# `project_service.get(project_id, user_id=user.id)`, which 404s on a project the caller
# does not own rather than leaking that it exists. That is the correct behaviour for a
# user and exactly wrong for an admin looking at someone else's session, so the admin
# gets its own set behind `get_current_admin` with no owner filter.
#
# Read-only on purpose. Nothing here draws, redraws, freezes or deletes a blueprint —
# the panel shows what the user produced, it does not edit their work.


@router.get("/projects/{project_id}/diagrams", response_model=DiagramSetView | None)
def project_diagrams(
    project_id: str,
    _admin: UserOut = Depends(get_current_admin),
) -> DiagramSetView | None:
    """The blueprint of any session. Null — not 404 — when none was drawn, as for the owner."""
    return project_service.diagrams(project_id)


@router.get("/projects/{project_id}/diagrams/{kind}.png")
def project_diagram_png(
    project_id: str,
    kind: str,
    version: int | None = Query(
        None, description="An earlier version still in the change history. Defaults to current."
    ),
    _admin: UserOut = Depends(get_current_admin),
) -> Response:
    """PNG bytes, fetched as a blob by the panel — auth is a Bearer header, not a cookie."""
    data = project_service.diagram_png(project_id, kind, version=version)
    suffix = f"-v{version}" if version else ""
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-cache",
            "Content-Disposition": f'inline; filename="{kind}{suffix}.png"',
        },
    )


@router.get("/projects/{project_id}/architecture/{kind}.png")
def project_architecture_png(
    project_id: str,
    kind: str,
    _admin: UserOut = Depends(get_current_admin),
) -> Response:
    """One of SDD.md §3.2's architectural views — `logical`, `development` or `deployment`.

    The counterpart to the owner's `/projects/{id}/plan/architecture/{kind}.png`, so the panel
    can show the same three figures it shows for the blueprint. No version parameter: these are
    drawn from the current design, so there is no earlier revision to ask for.
    """
    data = project_service.architecture_png(project_id, kind)
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-cache",
            "Content-Disposition": f'inline; filename="{kind}-view.png"',
        },
    )


@router.get("/projects/{project_id}/solution-design.{fmt}")
def project_solution_design(
    project_id: str,
    fmt: str,
    _admin: UserOut = Depends(get_current_admin),
) -> Response:
    """A user's `SDD.md` as a `pdf` or `docx` download.

    The counterpart to the owner's `/projects/{id}/plan/solution-design.{fmt}`, and byte for
    byte the same document: both call one service method, which parses the markdown once. So a
    reviewer reading the panel's copy is reading what the user has.

    Read-only like the rest of this file — the document is laid out, never regenerated.
    """
    data, filename = project_service.solution_design_document(project_id, fmt)
    return Response(
        content=data,
        media_type=export_documents.media_type(fmt),
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
