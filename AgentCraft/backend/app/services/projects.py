"""Project orchestration with state machine enforcement."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.state_machine import Platform, ProjectPath, ProjectState, assert_can
from app.db.session import clear_progress, get_progress, repo, set_progress
from app.models.schemas import (
    ExportResult,
    InterviewQuestion,
    ProjectBrief,
    ProjectPlan,
    ProjectStatus,
    SessionSummary,
    UploadedDocument,
)
from app.models.diagrams import (
    DIAGRAM_KINDS,
    VIEW_NAMES,
    DiagramScope,
    DiagramSet,
    DiagramSetView,
)
from app.services.deduction.engine import deduce_plan, demo_plan
from app.services.deduction.project_rules import ensure_project_rules
from app.services.diagrams import architecture as diagram_architecture
from app.services.diagrams import build as diagram_build
from app.services.diagrams import store as diagram_store
from app.services.diagrams import views as diagram_views
from app.services.deduction.solution_design import (
    SDD_FILENAME,
    apply_solution_design_to_plan,
    fallback_solution_design,
    fallback_solution_design_views,
    generate_solution_design,
    is_solution_design_complete,
    plan_architecture_views,
    plan_needs_detailed_solution_design,
    plan_needs_solution_design,
    plan_solution_design_body,
)
from app.services.deduction.work_breakdown import (
    apply_work_breakdown_to_plan,
    fallback_work_breakdown,
    generate_work_breakdown,
    is_work_breakdown_complete,
    patch_work_breakdown_artifacts,
    plan_needs_detailed_work_breakdown,
    plan_needs_work_breakdown,
)
from app.services.deduction.goals_format import normalize_plan_agent_goals
from app.services.deduction.source_tree import (
    ensure_frontend_in_tree,
    ensure_package_inits,
    ensure_plan_source_tree,
    is_legacy_source_tree,
    sanitize_source_tree_docs_only,
    source_tree_needs_doc_sanitize,
    source_tree_needs_frontend,
    source_tree_needs_root_scaffold,
)
from app.services.export import documents as export_documents
from app.services.export.renderers import build_zip_bytes, render_files, write_directory
from app.services.interview.engine import (
    apply_answers,
    brief_from_interview,
    fresh_interview,
    generate_project_questions,
    is_complete,
    is_structured_expanded_brief,
    needs_project_questions,
    with_project_questions,
)

logger = logging.getLogger("agentcraft.projects")


def slugify_project_name(name: str | None) -> str:
    raw = (name or "agentcraft-project").strip()
    cleaned = re.sub(r"[^\w\s-]+", "", raw, flags=re.UNICODE)
    slug = re.sub(r"[\s_]+", "-", cleaned.strip())
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    return (slug or "agentcraft-project")[:64]


DEFAULT_PROJECT_NAME = "Untitled Project"


def derive_project_title(text: str, *, fallback: str = DEFAULT_PROJECT_NAME) -> str:
    """
    Readable project title from a problem statement or expanded brief.

    Strips markdown headings / bold / bullets so a structured brief does not
    become a title like "## Product **Smart Tutor AI** is an enterprise…".
    """
    raw = (text or "").strip()
    if not raw:
        return fallback

    # Prefer the Product section of an expanded brief, else the first prose line.
    body = raw
    match = re.search(r"^##\s+Product\s*\n(.+?)(?=^##\s+|\Z)", raw, flags=re.M | re.S)
    if match:
        body = match.group(1)

    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"\*\*|__|[*_`]", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        # First sentence, when it is long enough to stand alone as a title.
        sentence = re.split(r"(?<=[.!?])\s+", line)[0].strip()
        title = sentence if len(sentence) >= 12 else line
        return title[:80].rstrip(" .,:;-–—") or fallback

    return fallback


def _backfill_project_rules(status: ProjectStatus, plan: ProjectPlan) -> ProjectPlan:
    """
    Persist the rules this project's export will actually contain.

    Rules are derived, so `ensure_project_rules` is the authority: a plan with none gets
    the full project-specific set (every Claude project generated before rules existed, and
    any IDE where the LLM returned none), and a plan that has some gains the always-on
    guarantees it is missing. The Review step's Rules tab and the sessions list's rule count
    read the **plan**, so storing the resolved set here is what keeps them from disagreeing
    with the exported folder. Authored rules are never dropped or rewritten.
    """
    # No platform yet means no export target, so there is no folder to name in the bodies.
    if status.platform is None:
        return plan
    rules = ensure_project_rules(plan, status.platform)
    if not rules or [r.name for r in rules] == [r.name for r in plan.rules]:
        return plan
    return plan.model_copy(update={"rules": rules})


def _persist_normalized_plan(status: ProjectStatus, plan: ProjectPlan) -> ProjectPlan:
    """Rewrite agent ## Goals to bullets and persist when changed."""
    normalized = _backfill_project_rules(status, normalize_plan_agent_goals(plan))
    if normalized is plan:
        return plan
    repo.update(status.id, plan=normalized)
    return normalized


def _plan_with_source_tree(status: ProjectStatus) -> ProjectPlan:
    """Ensure plan has a modular source_tree; always strip scaffold to docstring-only."""
    plan = status.plan
    assert plan is not None
    plan = _persist_normalized_plan(status, plan)
    needs_rebuild = is_legacy_source_tree(plan.source_tree)
    needs_sanitize = source_tree_needs_doc_sanitize(plan.source_tree)
    needs_root = source_tree_needs_root_scaffold(plan.source_tree)
    needs_frontend = source_tree_needs_frontend(
        plan.source_tree, status.brief, plan.summary or ""
    )
    if (
        not needs_rebuild
        and not needs_sanitize
        and not needs_root
        and not needs_frontend
        and plan.source_tree
    ):
        return plan
    if needs_rebuild or not plan.source_tree:
        tree = ensure_plan_source_tree(
            plan.source_tree,
            status.brief,
            project_name=plan.project_name or status.name,
            summary=plan.summary or "",
        )
    elif needs_frontend:
        tree = ensure_frontend_in_tree(
            plan.source_tree,
            status.brief,
            project_name=plan.project_name or status.name,
            summary=plan.summary or "",
        )
    elif needs_sanitize:
        tree = sanitize_source_tree_docs_only(plan.source_tree)
    else:
        tree = ensure_package_inits(list(plan.source_tree or []))
    enriched = plan.model_copy(update={"source_tree": tree})
    enriched = patch_work_breakdown_artifacts(enriched, status.platform)
    repo.update(status.id, plan=enriched)
    return enriched


def _ensure_work_breakdown(
    status: ProjectStatus,
    plan: ProjectPlan,
    *,
    use_llm: bool = True,
) -> ProjectPlan:
    """Backfill WORKBREAKDOWN.md for existing plans.

    Preview uses instant fallback so the Files tree is never blocked on Bedrock.
    Export may call Bedrock once for a richer phased plan.
    """
    if not plan_needs_work_breakdown(plan):
        return plan
    brief = status.brief or ProjectBrief()
    platform = status.platform
    wb = ""
    if use_llm and platform:
        try:
            wb, _ = asyncio.run(generate_work_breakdown(brief, plan, platform))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Work breakdown backfill failed: %s", exc)
            wb = fallback_work_breakdown(brief, plan, platform)
    else:
        wb = fallback_work_breakdown(brief, plan, platform)
    enriched = apply_work_breakdown_to_plan(
        plan, wb, used_llm=False, complete=is_work_breakdown_complete(wb)
    )
    repo.update(status.id, plan=enriched)
    return enriched


async def generate_detailed_work_breakdown(
    status: ProjectStatus,
    plan: ProjectPlan,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
) -> ProjectPlan:
    """Bedrock-generated WORKBREAKDOWN.md (per-phase, with optional incremental saves)."""
    brief = status.brief or ProjectBrief()
    platform = status.platform
    if not platform:
        raise AppError("Platform required", status_code=400)
    try:
        wb, used_llm = await generate_work_breakdown(
            brief,
            plan,
            platform,
            on_progress=on_progress,
            on_partial=on_partial,
        )
        complete = is_work_breakdown_complete(wb)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Detailed work breakdown failed: %s", exc)
        wb = fallback_work_breakdown(brief, plan, platform)
        used_llm = False
        complete = is_work_breakdown_complete(wb)
    enriched = apply_work_breakdown_to_plan(
        plan, wb, used_llm=used_llm, complete=complete
    )
    repo.update(status.id, plan=enriched)
    return enriched


def _ensure_solution_design(
    status: ProjectStatus,
    plan: ProjectPlan,
    *,
    use_llm: bool = True,
) -> ProjectPlan:
    """Backfill SDD.md for plans generated before it existed.

    Same split as `_ensure_work_breakdown`: preview uses the instant deterministic document
    so the Files tree is never blocked on Bedrock, and export may call Bedrock once.
    """
    if not plan_needs_solution_design(plan):
        return plan
    brief = status.brief or ProjectBrief()
    platform = status.platform
    sdd = ""
    views: dict = {}
    if use_llm and platform:
        try:
            sdd, _, views = asyncio.run(generate_solution_design(brief, plan, platform))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Solution design backfill failed: %s", exc)
            sdd = fallback_solution_design(brief, plan, platform)
    else:
        sdd = fallback_solution_design(brief, plan, platform)
    if not views:
        views = fallback_solution_design_views(brief, plan)
    enriched = apply_solution_design_to_plan(
        plan, sdd, used_llm=False, complete=is_solution_design_complete(sdd), views=views
    )
    repo.update(status.id, plan=enriched)
    return enriched


async def generate_detailed_solution_design(
    status: ProjectStatus,
    plan: ProjectPlan,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> ProjectPlan:
    """Bedrock-generated SDD.md (three parallel section groups; see solution_design)."""
    brief = status.brief or ProjectBrief()
    platform = status.platform
    if not platform:
        raise AppError("Platform required", status_code=400)
    try:
        sdd, used_llm, views = await generate_solution_design(
            brief, plan, platform, on_progress=on_progress
        )
        complete = is_solution_design_complete(sdd)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Detailed solution design failed: %s", exc)
        sdd = fallback_solution_design(brief, plan, platform)
        views = fallback_solution_design_views(brief, plan)
        used_llm = False
        complete = is_solution_design_complete(sdd)
    enriched = apply_solution_design_to_plan(
        plan, sdd, used_llm=used_llm, complete=complete, views=views
    )
    repo.update(status.id, plan=enriched)
    return enriched


class ProjectService:
    def create(self, name: str = "Untitled Project", *, user_id: str) -> ProjectStatus:
        return repo.create(name=name, user_id=user_id)

    def get(self, project_id: str, *, user_id: str | None = None) -> ProjectStatus:
        if user_id is not None:
            status = repo.get_owned(project_id, user_id)
        else:
            status = repo.get(project_id)
        if not status:
            raise AppError("Project not found", status_code=404)
        # Backfill existing plans: ## Goals as bullet lists (esp. VAPT agent).
        if status.plan:
            plan = status.plan
            normalized = normalize_plan_agent_goals(plan)
            if normalized is not plan:
                plan = normalized
            # Projects generated without rules (every Claude project, plus any plan the
            # LLM returned none for) get the derived set here — the Review step and the
            # sessions rule count read the plan, so this is what populates them.
            plan = _backfill_project_rules(status, plan)
            if source_tree_needs_root_scaffold(plan.source_tree):
                plan = plan.model_copy(
                    update={"source_tree": ensure_package_inits(list(plan.source_tree or []))}
                )
            if source_tree_needs_frontend(plan.source_tree, status.brief, plan.summary or ""):
                plan = plan.model_copy(
                    update={
                        "source_tree": ensure_frontend_in_tree(
                            plan.source_tree,
                            status.brief,
                            project_name=plan.project_name or status.name,
                            summary=plan.summary or "",
                        )
                    }
                )
            plan = patch_work_breakdown_artifacts(plan, status.platform)
            if plan is not status.plan:
                status = repo.update(project_id, plan=plan)
        return status

    def list(self, *, user_id: str) -> list[ProjectStatus]:
        return repo.list(user_id=user_id)

    def list_sessions(self, *, user_id: str) -> list[SessionSummary]:
        return repo.list_sessions(user_id)

    def delete(self, project_id: str, *, user_id: str) -> None:
        status = self.get(project_id, user_id=user_id)
        if not repo.delete_owned(project_id, user_id):
            raise AppError("Project not found", status_code=404)
        # Cached blueprint PNGs go with it — the row that pointed at them is gone.
        diagram_store.clear(project_id)
        # Remove cached export zips for this project (any platform suffix).
        exports = Path("exports")
        if exports.is_dir():
            for path in exports.glob(f"{project_id}-*.zip"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not delete export zip %s", path)
        _ = status

    def set_path(self, project_id: str, path: ProjectPath) -> ProjectStatus:
        status = self.get(project_id)
        action = "choose_path_docs" if path == ProjectPath.DOCS else "choose_path_interview"
        nxt = assert_can(action, status.state)
        interview: list[InterviewQuestion] | None = None
        if path == ProjectPath.INTERVIEW:
            # Re-entering the interview path (e.g. Back to path choice, then
            # forward again) must keep prior answers — only seed when empty.
            interview = list(status.interview) if status.interview else fresh_interview()
        return repo.update(
            project_id,
            state=nxt,
            path=path,
            interview=interview,
            clear_error=True,
        )

    def rollback(self, project_id: str) -> ProjectStatus:
        """One-step wizard rollback (docs/interview/platform/review)."""
        status = self.get(project_id)
        state = status.state

        if state in {ProjectState.AWAITING_DOCS, ProjectState.AWAITING_INTERVIEW}:
            assert_can("rollback_to_created", state)
            # Keep interview answers and brief: the user is only reconsidering the
            # path, and set_path restores them if they come back to this one.
            return repo.update(
                project_id,
                state=ProjectState.CREATED,
                clear_path=True,
                clear_error=True,
            )

        if state == ProjectState.CONTEXT_READY:
            if status.path == ProjectPath.INTERVIEW:
                assert_can("rollback_to_interview", state)
                # Re-open the interview WITHOUT discarding answers — the UI lets the
                # user edit them. Clearing the last answer here used to destroy an
                # expanded brief on a single Back click.
                interview = list(status.interview or fresh_interview())
                return repo.update(
                    project_id,
                    state=ProjectState.AWAITING_INTERVIEW,
                    path=ProjectPath.INTERVIEW,
                    interview=interview,
                    clear_error=True,
                )
            assert_can("rollback_to_docs", state)
            return repo.update(
                project_id,
                state=ProjectState.AWAITING_DOCS,
                path=ProjectPath.DOCS,
                clear_error=True,
            )

        if state == ProjectState.PLATFORM_SELECTED:
            assert_can("rollback_to_context", state)
            return repo.update(
                project_id,
                state=ProjectState.CONTEXT_READY,
                clear_platform=True,
                clear_error=True,
            )

        if state in {ProjectState.READY_FOR_REVIEW, ProjectState.EXPORTED}:
            raise AppError(
                "Cannot go back after generation — review/edit the plan or start a new project",
                status_code=400,
            )

        raise AppError(
            f"Cannot go back from state {state.value}",
            status_code=400,
        )

    async def submit_documents(
        self,
        project_id: str,
        *,
        problem_statement: str = "",
        files: list[tuple[str, bytes]] | None = None,
    ) -> ProjectStatus:
        from app.services.parser.documents import (
            ALLOWED_LABEL,
            extract_upload_text,
            guess_content_type,
        )
        from app.services.parser.match import assert_statement_matches_documents

        status = self.get(project_id)
        nxt = assert_can("submit_documents", status.state)

        statement = (problem_statement or "").strip()
        file_list = files or []
        if not statement:
            raise AppError(
                "Problem statement is required. Describe the product or problem clearly.",
                status_code=400,
            )
        if not file_list:
            raise AppError(
                f"At least one document is required ({ALLOWED_LABEL}). "
                "Multiple files are supported.",
                status_code=400,
            )

        settings = get_settings()

        async def _extract_one(filename: str, data: bytes) -> tuple[str, UploadedDocument]:
            if len(data) > settings.upload_max_bytes:
                raise AppError(f"File too large: {filename}", status_code=400)
            if not data:
                raise AppError(f"Empty file: {filename}", status_code=400)
            extracted = await extract_upload_text(filename, data)
            if not extracted.strip():
                raise AppError(
                    f"No readable content found in '{filename}'. "
                    f"Allowed: {ALLOWED_LABEL} — with actual text or rows in it.",
                    status_code=400,
                )
            record = UploadedDocument(
                filename=filename,
                size_bytes=len(data),
                content_type=guess_content_type(filename),
                chars_extracted=len(extracted),
                uploaded_at=datetime.now(timezone.utc).isoformat(),
            )
            return f"### {filename}\n{extracted}", record

        # Parallel extract (OCR / PDF / DOCX) then one fast async semantic match
        results = list(await asyncio.gather(*[_extract_one(f, d) for f, d in file_list]))
        document_text = "\n\n".join(text for text, _ in results)
        documents = [record for _, record in results]
        await assert_statement_matches_documents(statement, document_text)

        brief = status.brief or ProjectBrief()
        brief = brief.model_copy(
            update={
                "problem_statement": statement,
                "document_text": document_text,
                "documents": documents,
            }
        )
        name = status.name
        if status.name == DEFAULT_PROJECT_NAME:
            name = derive_project_title(statement, fallback=status.name)

        return repo.update(
            project_id,
            state=nxt,
            brief=brief,
            name=name,
            clear_error=True,
        )

    async def answer_interview(
        self, project_id: str, answers: dict[str, str], *, name: str | None = None
    ) -> ProjectStatus:
        status = self.get(project_id)
        if status.state != ProjectState.AWAITING_INTERVIEW:
            assert_can("complete_interview", status.state)  # raises with clear message

        questions = apply_answers(status.interview, answers)
        goal_text = next((q.answer or "" for q in questions if q.id == "goal"), "")
        structured = is_structured_expanded_brief(goal_text)

        # An explicit title from the UI always wins; otherwise derive from the goal.
        explicit_name = (name or "").strip()[:80]

        def resolved_name(problem_statement: str) -> str:
            if explicit_name:
                return explicit_name
            if status.name and status.name != DEFAULT_PROJECT_NAME:
                return status.name
            return derive_project_title(problem_statement, fallback=status.name)

        # Expanded brief on goal → auto-fill optional fields and finish interview.
        if structured and is_complete(questions):
            nxt = assert_can("complete_interview", status.state)
            brief = brief_from_interview(questions)
            return repo.update(
                project_id,
                state=nxt,
                interview=questions,
                brief=brief,
                name=resolved_name(brief.problem_statement),
                clear_error=True,
            )

        # Plain goal (no expand) → tailor a few skippable, project-specific questions.
        if needs_project_questions(questions):
            generated = await generate_project_questions(goal_text)
            if generated:
                questions = with_project_questions(questions, generated)
                return repo.update(
                    project_id,
                    interview=questions,
                    name=resolved_name(goal_text),
                    clear_error=True,
                )

        if is_complete(questions):
            nxt = assert_can("complete_interview", status.state)
            brief = brief_from_interview(questions)
            return repo.update(
                project_id,
                state=nxt,
                interview=questions,
                brief=brief,
                name=resolved_name(brief.problem_statement),
                clear_error=True,
            )

        return repo.update(
            project_id,
            interview=questions,
            name=resolved_name(goal_text),
            clear_error=True,
        )

    def set_platform(self, project_id: str, platform: Platform) -> ProjectStatus:
        status = self.get(project_id)
        nxt = assert_can("set_platform", status.state)
        return repo.update(project_id, state=nxt, platform=platform, clear_error=True)

    # ── Process blueprint (SIPOC / flow / swimlane) ──────────────────────────────
    #
    # Diagrams are an artifact on the project, not a state in the FSM. That was a deliberate
    # choice: adding a state would have changed the transition table every existing project,
    # the CLI and the test suite depend on. A project with no diagrams therefore behaves
    # exactly as it did before this feature existed.

    BLUEPRINT_PROGRESS_PREFIX = "Blueprint:"

    def _blueprint_running(self, project_id: str) -> bool:
        return (get_progress(project_id) or "").startswith(self.BLUEPRINT_PROGRESS_PREFIX)

    def _assert_blueprint_frozen(self, status: ProjectStatus) -> None:
        """Block generation while diagrams exist but are still open for changes.

        Only when they exist. Generating without a blueprint stays supported — that is the
        pre-existing flow, and every project created before this feature has no diagrams.
        """
        if status.has_diagrams and not status.diagrams_frozen:
            raise AppError(
                "Approve the process blueprint first — review the SIPOC, process flow and "
                "swimlane, then Freeze them. Generation follows the frozen process.",
                status_code=409,
            )

    def _process_context(self, project_id: str) -> str:
        """The frozen blueprint as prompt context, or empty when there is none."""
        dset = repo.get_diagrams(project_id)
        if not dset or not dset.frozen:
            return ""
        return dset.as_generation_context()

    def _diagram_view(self, project_id: str, dset: DiagramSet) -> DiagramSetView:
        # A revision is marked viewable when its views are still on record, so the UI knows
        # which history entries can show their own images instead of offering a dead link.
        viewable = dset.viewable_versions()
        return DiagramSetView(
            project_id=project_id,
            version=dset.version,
            frozen=dset.frozen,
            frozen_at=dset.frozen_at,
            llm=dset.llm,
            model=dset.model,
            sipoc=dset.sipoc,
            flow=dset.flow,
            swimlane=dset.swimlane,
            warnings=dset.warnings,
            revisions=[
                r.model_copy(update={"viewable": r.version in viewable}) for r in dset.revisions
            ],
            images=diagram_store.images_for(project_id, dset),
            updated_at=dset.updated_at,
        )

    def diagrams(self, project_id: str) -> DiagramSetView | None:
        self.get(project_id)  # 404s for an unknown project before touching the disk
        dset = repo.get_diagrams(project_id)
        return self._diagram_view(project_id, dset) if dset else None

    def diagram_png(self, project_id: str, kind: str, version: int | None = None) -> bytes:
        if kind not in DIAGRAM_KINDS:
            raise AppError(f"Unknown diagram: {kind}", status_code=404)
        self.get(project_id)
        dset = repo.get_diagrams(project_id)
        if not dset:
            raise AppError("No process blueprint for this project yet", status_code=404)
        try:
            return diagram_store.get_png(project_id, dset, kind, version=version)
        except ValueError as exc:
            # Asked for a version whose views have aged out of the history — a missing image,
            # not a broken server.
            raise AppError(str(exc), status_code=404) from exc

    def architecture_png(self, project_id: str, kind: str) -> bytes:
        """One of SDD §3.2's architectural views, drawn on demand and cached.

        No version parameter, unlike `diagram_png`: these are a rendering of the current
        design, so there is no earlier revision to ask for.
        """
        if kind not in diagram_architecture.ARCH_KINDS:
            raise AppError(f"Unknown architecture view: {kind}", status_code=404)
        status = self.get(project_id)
        if not status.plan:
            raise AppError("No plan for this project yet", status_code=404)
        plan = _plan_with_source_tree(status)
        # Deterministic backfill only. This is fetched while the file pane renders SDD.md, and
        # a Bedrock call behind an `<img>` would leave the reader looking at a spinner.
        if plan_needs_solution_design(plan):
            plan = _ensure_solution_design(status, plan, use_llm=False)
        try:
            return diagram_store.get_arch_png(
                project_id,
                plan_architecture_views(plan),
                kind,
                project_name=plan.project_name,
            )
        except (OSError, ValueError) as exc:
            raise AppError(f"Could not render {kind} view: {exc}", status_code=404) from exc

    def solution_design_document(self, project_id: str, fmt: str) -> tuple[bytes, str]:
        """`SDD.md` as a PDF or a DOCX: `(bytes, download filename)`.

        The same markdown the file pane previews, laid out for paper with §3.2's three
        architecture figures embedded. One parser feeds both emitters (see
        `app.services.export.documents`), so the download cannot drift from the preview, and
        the PDF cannot drift from the DOCX.

        Deterministic backfill only, exactly as `architecture_png` does it and for the same
        reason: this is behind a download button, and a Bedrock call there would leave the
        reader watching a spinner with no way to tell a slow model from a broken button. A
        project whose document has never been generated gets the deterministic one — which is
        the document the file pane is already showing them.
        """
        if fmt not in export_documents.DOC_FORMATS:
            raise AppError(f"Unknown document format: {fmt}", status_code=404)
        status = self.get(project_id)
        if not status.plan:
            raise AppError("No plan for this project yet", status_code=404)
        plan = _plan_with_source_tree(status)
        if plan_needs_solution_design(plan):
            plan = _ensure_solution_design(status, plan, use_llm=False)
        markdown = plan_solution_design_body(plan)
        if not markdown.strip():
            raise AppError(f"No {SDD_FILENAME} for this project yet", status_code=404)
        try:
            data = export_documents.render_document(
                markdown,
                fmt,
                title=plan.project_name or status.name,
                subtitle=f"Solution Design Document ({SDD_FILENAME})",
                images=self._document_images(project_id, plan, markdown),
            )
        except Exception as exc:  # noqa: BLE001 — layout failure is a 500, not a stack trace
            logger.exception("%s %s export failed for %s", SDD_FILENAME, fmt, project_id)
            raise AppError(
                f"Could not build the {fmt.upper()}: {exc}", status_code=500
            ) from exc
        stem = Path(SDD_FILENAME).stem
        return data, f"{slugify_project_name(status.name)}-{stem}.{fmt}"

    def _document_images(
        self, project_id: str, plan: ProjectPlan, markdown: str
    ) -> dict[str, bytes]:
        """Workspace path → PNG bytes for the figures a document actually references.

        Driven by the markdown rather than by what happens to exist, so a document is never
        charged for a picture it does not show, and a blueprint PNG is read only if the text
        points at one. Both folders are covered because both are things a generated document
        may embed: `docs/architecture/` from SDD §3.2, `docs/diagrams/` from the blueprint.

        A figure that cannot be drawn is left out and the emitters fall back to its caption —
        a download missing one picture still delivers, one that raises delivers nothing.
        """
        wanted = set(export_documents.image_paths(markdown))
        if not wanted:
            return {}

        images: dict[str, bytes] = {}
        arch = {
            path: kind
            for path, kind in diagram_architecture.ARCH_WORKSPACE_PATHS.items()
            if path in wanted
        }
        if arch:
            try:
                drawn = diagram_store.arch_png_bytes(
                    project_id,
                    plan_architecture_views(plan),
                    project_name=plan.project_name,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Architecture views unavailable for the %s download of %s",
                    SDD_FILENAME,
                    project_id,
                    exc_info=True,
                )
                drawn = {}
            images.update({p: drawn[k] for p, k in arch.items() if k in drawn})

        dset = repo.get_diagrams(project_id)
        if dset:
            for path, kind in self._blueprint_image_paths(dset).items():
                if path not in wanted:
                    continue
                try:
                    images[path] = diagram_store.get_png(project_id, dset, kind)
                except Exception:  # noqa: BLE001
                    logger.warning("Skipping %s diagram in document for %s", kind, project_id)
        return images

    def start_diagrams_background(
        self, project_id: str, *, use_demo: bool = False
    ) -> ProjectStatus:
        """Draw the first blueprint. Backgrounded because it is four LLM calls."""
        status = self.get(project_id)
        if not status.brief or not (status.brief.problem_statement or "").strip():
            raise AppError(
                "Describe the problem (or upload a document) before drawing the blueprint",
                status_code=400,
            )
        if self._blueprint_running(project_id):
            return status
        if status.diagrams_frozen:
            raise AppError(
                "The blueprint is frozen. Unfreeze it before regenerating from scratch.",
                status_code=409,
            )

        brief = status.brief
        # Clear the previous failure before queueing. The client polls the project and treats
        # `error` as "this attempt failed", so a leftover message from the last try would abort
        # the retry on its very first tick — the retry would look broken without ever running.
        repo.update(project_id, clear_error=True)
        set_progress(project_id, f"{self.BLUEPRINT_PROGRESS_PREFIX} queued…")

        async def _runner() -> None:
            try:
                async def on_progress(message: str) -> None:
                    set_progress(project_id, f"{self.BLUEPRINT_PROGRESS_PREFIX} {message}")

                if use_demo:
                    await on_progress("building demo blueprint…")
                    dset = diagram_build.demo_diagram_set(brief)
                else:
                    dset = await diagram_build.build_diagram_set(brief, on_progress=on_progress)
                await on_progress("rendering PNGs…")
                diagram_store.render_set(project_id, dset)
                repo.set_diagrams(project_id, dset)
                repo.update(project_id, clear_error=True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Blueprint generation failed project=%s", project_id)
                repo.update(
                    project_id,
                    error=(
                        "Process blueprint failed (Bedrock/LiteLLM). Check AWS credentials, "
                        f"region, and model access: {getattr(exc, 'message', None) or exc}"
                    ),
                )
            finally:
                clear_progress(project_id)

        asyncio.create_task(_runner())
        return self.get(project_id)

    async def generate_diagrams(
        self, project_id: str, *, use_demo: bool = False
    ) -> DiagramSetView:
        """Blocking build — the CLI path, where there is no UI to poll for progress."""
        status = self.get(project_id)
        if not status.brief or not (status.brief.problem_statement or "").strip():
            raise AppError(
                "Describe the problem (or upload a document) before drawing the blueprint",
                status_code=400,
            )
        if status.diagrams_frozen:
            raise AppError(
                "The blueprint is frozen. Unfreeze it before regenerating from scratch.",
                status_code=409,
            )

        async def on_progress(message: str) -> None:
            set_progress(project_id, f"{self.BLUEPRINT_PROGRESS_PREFIX} {message}")

        try:
            if use_demo:
                dset = diagram_build.demo_diagram_set(status.brief)
            else:
                dset = await diagram_build.build_diagram_set(
                    status.brief, on_progress=on_progress
                )
            diagram_store.render_set(project_id, dset)
            repo.set_diagrams(project_id, dset)
            repo.update(project_id, clear_error=True)
            return self._diagram_view(project_id, dset)
        except AppError:
            raise
        except Exception as exc:
            logger.exception("Blueprint generation failed project=%s", project_id)
            raise AppError(
                "Process blueprint failed (Bedrock/LiteLLM). Check AWS credentials, region, "
                f"and model access: {exc}",
                status_code=502,
            ) from exc
        finally:
            clear_progress(project_id)

    async def refine_diagrams(
        self, project_id: str, instruction: str, scope: DiagramScope = "all"
    ) -> DiagramSetView:
        """Blocking follow-up — the CLI path."""
        self.get(project_id)
        instruction = (instruction or "").strip()
        if not instruction:
            raise AppError("Describe the change you want", status_code=400)
        current = repo.get_diagrams(project_id)
        if not current:
            raise AppError("Generate the process blueprint first", status_code=404)
        if current.frozen:
            raise AppError(
                "The blueprint is frozen. Unfreeze it to make further changes.",
                status_code=409,
            )

        async def on_progress(message: str) -> None:
            set_progress(project_id, f"{self.BLUEPRINT_PROGRESS_PREFIX} {message}")

        try:
            revised = await diagram_build.revise_diagram_set(
                current, instruction, scope=scope, on_progress=on_progress
            )
            self._render_revision(project_id, current, revised)
            repo.set_diagrams(project_id, revised)
            repo.update(project_id, clear_error=True)
            return self._diagram_view(project_id, revised)
        except diagram_build.ViewUnchanged as exc:
            # Not a failure of the request — the model answered, it just did not change
            # anything. 422 rather than 502: nothing is broken, the instruction needs to be
            # more concrete, and the blueprint is deliberately left on its current version.
            raise AppError(str(exc), status_code=422) from exc
        except AppError:
            raise
        except Exception as exc:
            logger.exception("Blueprint follow-up failed project=%s", project_id)
            raise AppError(f"Blueprint follow-up failed: {exc}", status_code=502) from exc
        finally:
            clear_progress(project_id)

    @staticmethod
    def _scope_phrase(scope: str) -> str:
        """What a follow-up scope is doing, in words, for the progress line."""
        if scope == "all":
            return "revising the process and all three views"
        return f"re-laying out the {VIEW_NAMES.get(scope, scope)} view only"

    @staticmethod
    def _render_revision(project_id: str, before: DiagramSet, after: DiagramSet) -> None:
        """Render the new version, copying any view the revision did not actually change.

        The comparison is on the stored views themselves rather than on the requested scope: a
        single-view follow-up is the case this exists for, but a process change that happened to
        leave the SIPOC identical gets the same saving, and a scope that claimed "one view" while
        the model moved would still redraw everything. Cheaper *and* it cannot be wrong.
        """
        changed = {k for k in DIAGRAM_KINDS if getattr(before, k) != getattr(after, k)}
        if before.model != after.model:
            changed = set(DIAGRAM_KINDS)  # the header, scope line and lane notes all read it
        diagram_store.render_set(
            project_id, after, redraw=changed, reuse_from=before.version
        )

    def start_diagram_followup_background(
        self, project_id: str, instruction: str, scope: DiagramScope = "all"
    ) -> ProjectStatus:
        """Apply one plain-language change — to the process, or to one view's layout."""
        status = self.get(project_id)
        instruction = (instruction or "").strip()
        if not instruction:
            raise AppError("Describe the change you want", status_code=400)
        current = repo.get_diagrams(project_id)
        if not current:
            raise AppError("Generate the process blueprint first", status_code=404)
        if current.frozen:
            raise AppError(
                "The blueprint is frozen. Unfreeze it to make further changes — the "
                "generated agents and skills follow whatever is frozen.",
                status_code=409,
            )
        if self._blueprint_running(project_id):
            return status

        # Same reason as the first draw: a stale error would end the poll before this runs.
        repo.update(project_id, clear_error=True)
        # The scope is in the very first progress line so the spinner can say what it is doing
        # before the first LLM call reports anything — the UI shows this text verbatim, and
        # "queued…" alone leaves the user unsure which of the two things they just asked for.
        set_progress(
            project_id,
            f"{self.BLUEPRINT_PROGRESS_PREFIX} queued — {self._scope_phrase(scope)}…",
        )

        async def _runner() -> None:
            try:
                async def on_progress(message: str) -> None:
                    set_progress(project_id, f"{self.BLUEPRINT_PROGRESS_PREFIX} {message}")

                revised = await diagram_build.revise_diagram_set(
                    current, instruction, scope=scope, on_progress=on_progress
                )
                await on_progress("rendering PNGs…")
                self._render_revision(project_id, current, revised)
                repo.set_diagrams(project_id, revised)
                repo.update(project_id, clear_error=True)
            except diagram_build.ViewUnchanged as exc:
                # The model answered and changed nothing. Written without the "failed" prefix:
                # nothing broke, the blueprint is intact on its current version, and the message
                # is advice about the instruction — the wizard shows this text to the user.
                logger.info("Blueprint follow-up changed nothing project=%s", project_id)
                repo.update(project_id, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Blueprint follow-up failed project=%s", project_id)
                repo.update(
                    project_id,
                    error=f"Blueprint follow-up failed: {getattr(exc, 'message', None) or exc}",
                )
            finally:
                clear_progress(project_id)

        asyncio.create_task(_runner())
        return self.get(project_id)

    def set_diagrams_frozen(self, project_id: str, frozen: bool) -> DiagramSetView:
        self.get(project_id)
        dset = repo.get_diagrams(project_id)
        if not dset:
            raise AppError("Generate the process blueprint first", status_code=404)
        stamp = datetime.now(timezone.utc).isoformat() if frozen else None
        updated = dset.model_copy(update={"frozen": frozen, "frozen_at": stamp})
        repo.set_diagrams(project_id, updated)
        return self._diagram_view(project_id, updated)

    def delete_diagrams(self, project_id: str) -> ProjectStatus:
        """Discard the blueprint entirely — the project then generates as it always did."""
        self.get(project_id)
        diagram_store.clear(project_id)
        return repo.clear_diagrams(project_id)

    async def generate(self, project_id: str, *, use_demo: bool = False) -> ProjectStatus:
        status = self.get(project_id)
        assert_can("start_generate", status.state)
        self._assert_blueprint_frozen(status)
        if not status.platform:
            raise AppError("Platform required before generate", status_code=400)
        if not status.brief:
            raise AppError("Project brief missing", status_code=400)

        repo.update(project_id, state=ProjectState.GENERATING, clear_error=True)
        set_progress(project_id, "Connecting to Bedrock via LiteLLM…")

        async def on_progress(message: str) -> None:
            set_progress(project_id, message)

        try:
            if use_demo:
                set_progress(project_id, "Building demo plan…")
                plan = demo_plan(status.brief, status.platform)
            else:
                plan = await deduce_plan(
                    status.brief,
                    status.platform,
                    on_progress=on_progress,
                    process_context=self._process_context(project_id),
                )

            clear_progress(project_id)
            return repo.update(
                project_id,
                state=ProjectState.READY_FOR_REVIEW,
                plan=plan,
                clear_error=True,
            )
        except AppError as exc:
            clear_progress(project_id)
            repo.update(
                project_id,
                state=ProjectState.PLATFORM_SELECTED,
                error=str(exc.message),
            )
            raise
        except Exception as exc:
            clear_progress(project_id)
            logger.exception("Generation failed for project=%s", project_id)
            repo.update(
                project_id,
                state=ProjectState.PLATFORM_SELECTED,
                error=str(exc),
            )
            raise AppError(
                f"LLM generation failed (Bedrock/LiteLLM). Check AWS credentials, region, and model access: {exc}",
                status_code=502,
            ) from exc

    def start_generate_background(self, project_id: str, *, use_demo: bool = False) -> ProjectStatus:
        """Kick off generation without blocking the HTTP response (UI polls progress)."""
        import asyncio

        status = self.get(project_id)
        assert_can("start_generate", status.state)
        self._assert_blueprint_frozen(status)
        if not status.platform:
            raise AppError("Platform required before generate", status_code=400)
        if not status.brief:
            raise AppError("Project brief missing", status_code=400)

        # Read before the state flips so a failure leaves nothing half-applied.
        process_context = self._process_context(project_id)
        started = repo.update(project_id, state=ProjectState.GENERATING, clear_error=True)
        set_progress(project_id, "Queued — connecting to Bedrock…")
        brief = started.brief
        platform = started.platform
        assert brief is not None and platform is not None

        async def _runner() -> None:
            set_progress(project_id, "Connecting to Bedrock via LiteLLM…")

            async def on_progress(message: str) -> None:
                set_progress(project_id, message)

            try:
                if use_demo:
                    set_progress(project_id, "Building demo plan…")
                    plan = demo_plan(brief, platform)
                else:
                    plan = await deduce_plan(
                        brief,
                        platform,
                        on_progress=on_progress,
                        process_context=process_context,
                    )
                repo.update(
                    project_id,
                    state=ProjectState.READY_FOR_REVIEW,
                    plan=plan,
                    clear_error=True,
                )
                clear_progress(project_id)
            except Exception as exc:
                clear_progress(project_id)
                logger.exception("Background generate failed project=%s", project_id)
                repo.update(
                    project_id,
                    state=ProjectState.PLATFORM_SELECTED,
                    error=str(getattr(exc, "message", None) or exc),
                )

        asyncio.create_task(_runner())
        return self.get(project_id)

    def update_plan(self, project_id: str, plan: ProjectPlan) -> ProjectStatus:
        status = self.get(project_id)
        nxt = assert_can("edit_plan", status.state)
        if plan.source_tree:
            plan = plan.model_copy(
                update={"source_tree": ensure_package_inits(sanitize_source_tree_docs_only(plan.source_tree))}
            )
        plan = normalize_plan_agent_goals(plan)
        overrides = dict(plan.file_overrides or {})
        wb = (plan.work_breakdown or "").strip()
        if wb and "WORKBREAKDOWN.md" not in overrides:
            overrides["WORKBREAKDOWN.md"] = wb
        elif overrides.get("WORKBREAKDOWN.md"):
            plan = plan.model_copy(update={"work_breakdown": overrides["WORKBREAKDOWN.md"]})
        # Same two-way sync for SDD.md: whichever side the edit arrived on becomes the other.
        sdd = (plan.solution_design or "").strip()
        if sdd and SDD_FILENAME not in overrides:
            overrides[SDD_FILENAME] = sdd
        elif overrides.get(SDD_FILENAME):
            plan = plan.model_copy(update={"solution_design": overrides[SDD_FILENAME]})
        if overrides != (plan.file_overrides or {}):
            plan = plan.model_copy(update={"file_overrides": overrides})
        return repo.update(project_id, state=nxt, plan=plan, clear_error=True)

    def start_work_breakdown_background(self, project_id: str) -> ProjectStatus:
        """Generate WORKBREAKDOWN.md in background (UI polls progress + incremental preview)."""
        status = self.get(project_id)
        if not status.plan or not status.platform:
            raise AppError("Plan and platform required", status_code=400)
        if not plan_needs_detailed_work_breakdown(status.plan):
            return status
        prog = get_progress(project_id) or ""
        if "WORKBREAKDOWN" in prog:
            return self.get(project_id)

        set_progress(project_id, "WORKBREAKDOWN.md: queued…")

        async def _runner() -> None:
            try:
                st = self.get(project_id)
                plan = _plan_with_source_tree(st)

                async def on_progress(message: str) -> None:
                    set_progress(project_id, message)

                async def on_partial(partial_md: str) -> None:
                    cur = self.get(project_id)
                    pl = _plan_with_source_tree(cur)
                    updated = apply_work_breakdown_to_plan(
                        pl,
                        partial_md,
                        used_llm=False,
                        complete=False,
                    )
                    repo.update(project_id, plan=updated)

                await generate_detailed_work_breakdown(
                    st,
                    plan,
                    on_progress=on_progress,
                    on_partial=on_partial,
                )
            except Exception as exc:
                logger.exception("Background WORKBREAKDOWN failed project=%s", project_id)
                try:
                    st = self.get(project_id)
                    pl = _plan_with_source_tree(st)
                    fb = fallback_work_breakdown(
                        st.brief or ProjectBrief(), pl, st.platform
                    )
                    repo.update(
                        project_id,
                        plan=apply_work_breakdown_to_plan(
                            pl, fb, used_llm=False, complete=True
                        ),
                        error=str(getattr(exc, "message", None) or exc)[:500],
                    )
                except Exception:
                    logger.exception("WORKBREAKDOWN fallback save failed")
            finally:
                clear_progress(project_id)

        asyncio.create_task(_runner())
        return self.get(project_id)

    async def generate_work_breakdown_for_project(self, project_id: str) -> ProjectStatus:
        """Blocking generate (export path). Prefer start_work_breakdown_background for UI."""
        status = self.get(project_id)
        if not status.plan or not status.platform:
            raise AppError("Plan and platform required", status_code=400)
        plan = _plan_with_source_tree(status)
        await generate_detailed_work_breakdown(status, plan)
        return self.get(project_id)

    def start_solution_design_background(self, project_id: str) -> ProjectStatus:
        """Generate SDD.md in background (UI and CLI both poll `progress`).

        Kept separate from the work-breakdown runner rather than merged into it: the two
        documents are independent, and a project whose WORKBREAKDOWN.md is already complete
        must be able to backfill only its SDD.md without regenerating the plan.
        """
        status = self.get(project_id)
        if not status.plan or not status.platform:
            raise AppError("Plan and platform required", status_code=400)
        if not plan_needs_detailed_solution_design(status.plan):
            return status
        prog = get_progress(project_id) or ""
        # Never start on top of a WORKBREAKDOWN run: both write the same plan row, and the
        # loser's `file_overrides` would drop the winner's document.
        if SDD_FILENAME in prog or "WORKBREAKDOWN" in prog:
            return self.get(project_id)

        set_progress(project_id, f"{SDD_FILENAME}: queued…")

        async def _runner() -> None:
            try:
                st = self.get(project_id)
                plan = _plan_with_source_tree(st)

                async def on_progress(message: str) -> None:
                    set_progress(project_id, message)

                await generate_detailed_solution_design(st, plan, on_progress=on_progress)
            except Exception as exc:
                logger.exception("Background %s failed project=%s", SDD_FILENAME, project_id)
                try:
                    st = self.get(project_id)
                    pl = _plan_with_source_tree(st)
                    brief = st.brief or ProjectBrief()
                    fb = fallback_solution_design(brief, pl, st.platform)
                    repo.update(
                        project_id,
                        plan=apply_solution_design_to_plan(
                            pl,
                            fb,
                            used_llm=False,
                            complete=True,
                            views=fallback_solution_design_views(brief, pl),
                        ),
                        error=str(getattr(exc, "message", None) or exc)[:500],
                    )
                except Exception:
                    logger.exception("%s fallback save failed", SDD_FILENAME)
            finally:
                clear_progress(project_id)

        asyncio.create_task(_runner())
        return self.get(project_id)

    async def generate_solution_design_for_project(self, project_id: str) -> ProjectStatus:
        """Blocking generate (export path). Prefer start_solution_design_background for UI."""
        status = self.get(project_id)
        if not status.plan or not status.platform:
            raise AppError("Plan and platform required", status_code=400)
        plan = _plan_with_source_tree(status)
        await generate_detailed_solution_design(status, plan)
        return self.get(project_id)

    def _blueprint_image_paths(self, dset: DiagramSet) -> dict[str, str]:
        """Workspace path → diagram kind for the version the user approved.

        Paths only, and no rendering: the export preview is polled while WORKBREAKDOWN.md
        generates, and it needs the names in the file tree, not several megabytes of PNG.
        """
        return {
            f"docs/diagrams/{diagram_views.export_filename(v.kind, dset.version)}": v.kind
            for v in diagram_views.VIEWS
        }

    def _blueprint_export_files(self, project_id: str) -> tuple[dict[str, str], dict[str, bytes]]:
        """`docs/diagrams/` for the exported workspace: the approved version's three PNGs.

        Images and nothing else. The folder is what the user approved, so someone opening it
        finds three diagrams rather than a README and a JSON dump to look past; what each
        picture is for, and which version it is, is documented in the workspace README
        instead (`_readme_with_blueprint`).

        Empty for a project with no blueprint — those export exactly as they did before this
        step existed.
        """
        dset = repo.get_diagrams(project_id)
        if not dset:
            return {}, {}

        images: dict[str, bytes] = {}
        for path, kind in self._blueprint_image_paths(dset).items():
            try:
                images[path] = diagram_store.get_png(project_id, dset, kind)
            except Exception:  # noqa: BLE001 — a missing PNG must not fail the export
                logger.warning("Skipping %s diagram in export for %s", kind, project_id)
        return {}, images

    def _readme_with_blueprint(self, readme: str, dset: DiagramSet) -> str:
        """Document `docs/diagrams/` and each image's purpose in the workspace README.

        The exporters build the README from the plan alone and know nothing about a
        blueprint, so the section is spliced in here — above the generated-by footer, which
        has to stay last.

        This is the only place the diagrams are explained now that the folder holds nothing
        but pictures: filenames say which view, this says what the view is *for*.
        """
        state = "approved" if dset.frozen else "draft — not approved yet"
        names = [
            diagram_views.export_filename(v.kind, dset.version) for v in diagram_views.VIEWS
        ]
        lines = [
            "## Process blueprint (`docs/diagrams/`)",
            "",
            f"The process this workspace was generated from — blueprint **v{dset.version}** "
            f"({state}). The agents, skills and rules below are written to follow these exact "
            "steps, so the diagrams are the specification, not decoration.",
            "",
            "```",
            "docs/",
            "└── diagrams/",
        ]
        for i, name in enumerate(names):
            lines.append(f"    {'└──' if i == len(names) - 1 else '├──'} {name}")
        lines += ["```", ""]
        for view, name in zip(diagram_views.VIEWS, names):
            lines.append(f"- **`docs/diagrams/{name}`** — {view.label}: {view.purpose}")
        lines += [
            "",
            "All three are views of one process model: a step in the swimlane is always in "
            "the flow, and the SIPOC process column is the same steps compressed. The version "
            "is in each filename, so exporting again after a change adds the new set beside "
            "the one that was reviewed instead of overwriting it.",
        ]
        if dset.model.scope:
            lines += ["", f"**Scope.** {dset.model.scope.strip()}"]
        if dset.model.assumptions:
            lines += [
                "",
                "**Assumptions accepted with the blueprint:**",
                *(f"- {a}" for a in dset.model.assumptions),
            ]
        if dset.revisions:
            lines += ["", "**Blueprint revisions:**"]
            lines += [
                f"- v{r.version}: {r.summary}"
                + (f" (asked: {r.instruction})" if r.instruction else "")
                for r in dset.revisions
            ]
        section = "\n".join(lines).rstrip() + "\n"

        footer = "Generated for "
        body = readme.rstrip("\n")
        idx = body.rfind(f"\n{footer}")
        if idx == -1:
            return f"{body}\n\n{section}"
        # rstrip on the left half too: the footer is normally preceded by a blank line, and
        # keeping it would leave three newlines above the new heading.
        return f"{body[:idx].rstrip()}\n\n{section}\n{body[idx + 1 :]}\n"

    def _attach_blueprint(self, files: dict[str, str], project_id: str) -> dict[str, bytes]:
        """Splice the blueprint into the rendered workspace; return the PNGs to write."""
        docs, images = self._blueprint_export_files(project_id)
        files.update(docs)
        dset = repo.get_diagrams(project_id) if images else None
        if dset and files.get("README.md"):
            files["README.md"] = self._readme_with_blueprint(files["README.md"], dset)
        return images

    def _readme_with_architecture(self, readme: str, plan: ProjectPlan, paths: list[str]) -> str:
        """Document `docs/architecture/` in the workspace README, beside `docs/diagrams/`.

        The blueprint had this and the architecture views did not, so a reader opening an
        exported workspace found three PNGs described in the README and three that appeared
        from nowhere. Both folders hold generated pictures; both are explained in the one file
        anybody opens first.

        Splices in the same way and to the same place — above the `Generated for` footer,
        which stays last — so the two sections read as a pair whichever order they arrive in.

        `paths` is what was actually written, so a view that failed to draw is not advertised.
        """
        views = [
            v for v in diagram_architecture.ARCH_VIEWS
            if diagram_architecture.arch_workspace_path(v.kind) in paths
        ]
        if not views:
            return readme
        names = [diagram_architecture.arch_export_filename(v.kind) for v in views]
        lines = [
            "## Architecture (`docs/architecture/`)",
            "",
            f"The software being built, as `{SDD_FILENAME}` §3.2 describes it — three views of "
            "one design, drawn from the solution design rather than sketched by hand. "
            f"`{SDD_FILENAME}` embeds these same files, so the document and this folder can "
            "never disagree.",
            "",
            "```",
            "docs/",
            "└── architecture/",
        ]
        for i, name in enumerate(names):
            lines.append(f"    {'└──' if i == len(names) - 1 else '├──'} {name}")
        lines += ["```", ""]
        for view, name in zip(views, names):
            lines.append(
                f"- **`{diagram_architecture.ARCH_DOC_DIR}/{name}`** — {view.label} "
                f"(§{view.section}): {view.purpose}"
            )
        lines += [
            "",
            "Unversioned filenames, unlike `docs/diagrams/`: the process blueprint is approved "
            "as a numbered revision, while these are a rendering of the current design and are "
            f"replaced whenever `{SDD_FILENAME}` is. Regenerate them by exporting again — "
            "editing a PNG by hand leaves it contradicting the document.",
            "",
            f"**How to read them together.** `docs/diagrams/` is the *process* this workspace "
            f"automates; `docs/architecture/` is the *system* that automates it. A step in the "
            f"swimlane is work somebody does today; a box in the logical view is code that will "
            f"do it. `{SDD_FILENAME}` is where the two are joined up.",
        ]
        section = "\n".join(lines).rstrip() + "\n"

        footer = "Generated for "
        body = readme.rstrip("\n")
        idx = body.rfind(f"\n{footer}")
        if idx == -1:
            return f"{body}\n\n{section}"
        return f"{body[:idx].rstrip()}\n\n{section}\n{body[idx + 1 :]}\n"

    def _architecture_image_paths(self, plan: ProjectPlan) -> dict[str, str]:
        """Workspace path → architecture view kind for SDD §3.2.

        Paths only and no rendering, for the same reason as `_blueprint_image_paths`: the
        preview tree is polled while a document generates. Empty when there is no SDD.md,
        because nothing would reference the images.
        """
        if not plan_solution_design_body(plan):
            return {}
        return dict(diagram_architecture.ARCH_WORKSPACE_PATHS)

    def _architecture_export_files(
        self, project_id: str, plan: ProjectPlan
    ) -> dict[str, bytes]:
        """`docs/architecture/` — the three PNGs SDD.md references by path.

        Drawn from `plan.architecture_views`, or re-derived from the plan for a project that
        predates it. A view that cannot be drawn is left out rather than failing the export:
        a document with one broken image link still delivers, an export that raises does not.
        """
        wanted = self._architecture_image_paths(plan)
        if not wanted:
            return {}
        try:
            drawn = diagram_store.arch_png_bytes(
                project_id,
                plan_architecture_views(plan),
                project_name=plan.project_name,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Architecture views unavailable for %s", project_id, exc_info=True)
            return {}
        return {path: drawn[kind] for path, kind in wanted.items() if kind in drawn}

    def export(
        self,
        project_id: str,
        *,
        mode: str = "zip",
        output_path: str | None = None,
    ) -> tuple[ExportResult, bytes | None]:
        status = self.get(project_id)
        assert_can("export", status.state)
        if not status.plan or not status.platform:
            raise AppError("Plan and platform required for export", status_code=400)

        plan = _plan_with_source_tree(status)
        if plan_needs_detailed_work_breakdown(plan):
            plan = asyncio.run(generate_detailed_work_breakdown(status, plan))
        elif plan_needs_work_breakdown(plan):
            plan = _ensure_work_breakdown(status, plan, use_llm=False)
        # SDD.md on the same terms as WORKBREAKDOWN.md: an incomplete document is
        # regenerated with Bedrock, a missing one on an older project is backfilled
        # deterministically so an export is never blocked on a model call.
        if plan_needs_detailed_solution_design(plan):
            plan = asyncio.run(generate_detailed_solution_design(status, plan))
        elif plan_needs_solution_design(plan):
            plan = _ensure_solution_design(status, plan, use_llm=False)
        dset = repo.get_diagrams(project_id)
        blueprint_paths = self._blueprint_image_paths(dset) if dset else {}
        arch_paths = self._architecture_image_paths(plan)
        files = render_files(
            plan,
            status.platform,
            extra_workspace_files=len(blueprint_paths) + len(arch_paths),
        )
        images = self._attach_blueprint(files, project_id)
        arch_images = self._architecture_export_files(project_id, plan)
        images.update(arch_images)
        # After the blueprint splice, so the two folders are documented in the order the
        # workspace is read: the process first, then the system built to run it.
        if arch_images and files.get("README.md"):
            files["README.md"] = self._readme_with_architecture(
                files["README.md"], plan, list(arch_images)
            )
        folder = slugify_project_name(status.name)
        zip_bytes: bytes | None = None
        download_path = None
        out_path = None

        if mode == "directory":
            if not output_path:
                raise AppError("output_path required for directory mode", status_code=400)
            # Write under a folder named after the project
            target = Path(output_path)
            if target.name != folder:
                target = target / folder
            written = write_directory(files, target, binary_files=images)
            out_path = str(target)
            file_list = list(files.keys()) + list(images.keys())
            _ = written
        else:
            zip_bytes = build_zip_bytes(files, root_folder=folder, binary_files=images)
            exports = Path("exports")
            exports.mkdir(parents=True, exist_ok=True)
            download_path = str(exports / f"{project_id}-{status.platform.value}.zip")
            Path(download_path).write_bytes(zip_bytes)
            file_list = list(files.keys()) + list(images.keys())

        repo.update(project_id, state=ProjectState.EXPORTED, clear_error=True)
        result = ExportResult(
            mode=mode,
            platform=status.platform,
            files=file_list,
            download_path=download_path,
            output_path=out_path,
        )
        return result, zip_bytes

    def preview_files(self, project_id: str) -> tuple[dict[str, str], list[str]]:
        """`(text files, binary paths)` for the file tree.

        The blueprint PNGs are listed but not read: nothing here renders or ships their
        bytes, because this is polled while WORKBREAKDOWN.md generates and the images are
        megabytes. The tree shows them, and clicking one fetches that single PNG from the
        diagrams endpoint — the same bytes the zip gets.
        """
        status = self.get(project_id)
        if not status.plan or not status.platform:
            raise AppError("Plan and platform required", status_code=400)
        plan = _plan_with_source_tree(status)
        # Fast path: never block preview on Bedrock (existing projects load instantly).
        plan = _ensure_work_breakdown(status, plan, use_llm=False)
        plan = _ensure_solution_design(status, plan, use_llm=False)
        dset = repo.get_diagrams(project_id)
        image_paths = list(self._blueprint_image_paths(dset)) if dset else []
        arch_paths = list(self._architecture_image_paths(plan))
        image_paths += arch_paths
        files = render_files(plan, status.platform, extra_workspace_files=len(image_paths))
        if dset and files.get("README.md"):
            files["README.md"] = self._readme_with_blueprint(files["README.md"], dset)
        # The preview is what the reader actually opens, so it documents both folders the same
        # way the zip does. Paths rather than rendered bytes: no PNG is drawn here.
        if arch_paths and files.get("README.md"):
            files["README.md"] = self._readme_with_architecture(
                files["README.md"], plan, arch_paths
            )
        return files, image_paths


project_service = ProjectService()
