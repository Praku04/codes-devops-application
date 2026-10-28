"""Project API — state-enforced transitions (auth required)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.core.deps import get_current_user
from app.core.exceptions import AppError
from app.core.state_machine import Platform, ProjectPath
from app.models.diagrams import DiagramFollowupRequest, DiagramSetView
from app.models.schemas import (
    CreateProjectRequest,
    ExpandBriefRequest,
    ExpandBriefResponse,
    ExportRequest,
    ExportResult,
    InterviewAnswerRequest,
    ProjectPlan,
    ProjectStatus,
    SessionSummary,
    SetPathRequest,
    SetPlatformRequest,
    UserOut,
)
from app.services.brief_expand import (
    SECTION_ORDER as _SECTION_ORDER,
    expand_problem_statement,
    is_short_brief,
)
from app.services.export import documents as export_documents
from app.services.projects import project_service, slugify_project_name

router = APIRouter(prefix="/projects")


@router.get("/meta/platforms")
def list_platforms() -> list[dict[str, str]]:
    return [
        {
            "id": Platform.CLAUDE_CODE.value,
            "label": "Claude Code",
            "blurb": ".claude/agents + .claude/skills/SKILL.md",
        },
        {
            "id": Platform.CURSOR.value,
            "label": "Cursor",
            "blurb": ".cursor/rules/*.mdc + skills + agents + mcp.json",
        },
        {
            "id": Platform.WINDSURF.value,
            "label": "Windsurf",
            "blurb": "AGENTS.md + .windsurf/agents + .windsurf/skills + .windsurf/rules",
        },
        {
            "id": Platform.GITHUB_COPILOT.value,
            "label": "GitHub Copilot",
            "blurb": "GitHub Copilot instructions + agents + skills",
        },
    ]


@router.post("/meta/expand-brief", response_model=ExpandBriefResponse)
async def expand_brief(
    body: ExpandBriefRequest,
    user: UserOut = Depends(get_current_user),
) -> ExpandBriefResponse:
    """Always expand into ## Product / Goals / Stack / … via parallel async LLM calls."""
    _ = user
    seed = (body.seed or "").strip()
    if not seed:
        raise AppError("Provide a short problem statement to expand", status_code=400)
    was_short = is_short_brief(seed)
    try:
        expanded = await expand_problem_statement(seed)
    except Exception as exc:  # noqa: BLE001
        raise AppError(f"Could not expand brief: {exc}", status_code=502) from exc
    if not expanded or not expanded.strip():
        raise AppError("Expand returned empty brief", status_code=502)
    if "## " not in expanded:
        raise AppError(
            "Expand did not produce structured sections. Check Bedrock credentials and retry.",
            status_code=502,
        )
    return ExpandBriefResponse(expanded=expanded, was_short=was_short)


@router.post("/meta/expand-brief/stream")
async def expand_brief_stream(
    body: ExpandBriefRequest,
    user: UserOut = Depends(get_current_user),
) -> StreamingResponse:
    """
    NDJSON stream of parallel expand progress.

    Lines:
      {"event":"start","sections":[...],"mode":"async_parallel"}
      {"event":"section","section":"goals","title":"Goals","ok":true}
      {"event":"done","expanded":"...","was_short":true}
      {"event":"error","message":"..."}
    """
    _ = user
    seed = (body.seed or "").strip()
    if not seed:
        raise AppError("Provide a short problem statement to expand", status_code=400)

    async def event_gen() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def on_section(section: str, body_text: str) -> None:
            ok = not (body_text or "").startswith(("_Section ", "_Could not expand"))
            await queue.put(
                {
                    "event": "section",
                    "section": section,
                    "title": section.replace("_", " ").title(),
                    "ok": ok,
                }
            )

        async def runner() -> None:
            try:
                expanded = await expand_problem_statement(seed, on_section=on_section)
                if "## " not in (expanded or ""):
                    await queue.put(
                        {
                            "event": "error",
                            "message": "Expand did not produce structured sections. Check Bedrock credentials and retry.",
                        }
                    )
                else:
                    await queue.put(
                        {
                            "event": "done",
                            "expanded": expanded,
                            "was_short": is_short_brief(seed),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                await queue.put({"event": "error", "message": f"Could not expand brief: {exc}"})
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        yield (
            json.dumps(
                {
                    "event": "start",
                    "sections": list(_SECTION_ORDER),
                    "mode": "async_parallel",
                }
            )
            + "\n"
        )
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield json.dumps(item) + "\n"
        finally:
            await task

    return StreamingResponse(event_gen(), media_type="application/x-ndjson")


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions(user: UserOut = Depends(get_current_user)) -> list[SessionSummary]:
    return project_service.list_sessions(user_id=user.id)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, user: UserOut = Depends(get_current_user)) -> Response:
    project_service.delete(project_id, user_id=user.id)
    return Response(status_code=204)


@router.post("", response_model=ProjectStatus)
def create_project(
    body: CreateProjectRequest | None = None,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    name = body.name if body else "Untitled Project"
    return project_service.create(name=name, user_id=user.id)


@router.get("", response_model=list[ProjectStatus])
def list_projects(user: UserOut = Depends(get_current_user)) -> list[ProjectStatus]:
    return project_service.list(user_id=user.id)


@router.get("/{project_id}", response_model=ProjectStatus)
def get_project(project_id: str, user: UserOut = Depends(get_current_user)) -> ProjectStatus:
    return project_service.get(project_id, user_id=user.id)


@router.post("/{project_id}/path", response_model=ProjectStatus)
def set_path(
    project_id: str,
    body: SetPathRequest,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    project_service.get(project_id, user_id=user.id)
    return project_service.set_path(project_id, body.path)


@router.post("/{project_id}/rollback", response_model=ProjectStatus)
def rollback_project(
    project_id: str,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    """One-step wizard back (path choice, docs/interview, platform, or generate)."""
    project_service.get(project_id, user_id=user.id)
    return project_service.rollback(project_id)


@router.post("/{project_id}/documents", response_model=ProjectStatus)
async def submit_documents(
    project_id: str,
    problem_statement: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    project_service.get(project_id, user_id=user.id)
    payloads: list[tuple[str, bytes]] = []
    for f in files or []:
        data = await f.read()
        payloads.append((f.filename or "upload.bin", data))
    return await project_service.submit_documents(
        project_id,
        problem_statement=problem_statement,
        files=payloads,
    )


@router.get("/{project_id}/interview", response_model=ProjectStatus)
def get_interview(project_id: str, user: UserOut = Depends(get_current_user)) -> ProjectStatus:
    return project_service.get(project_id, user_id=user.id)


@router.post("/{project_id}/interview", response_model=ProjectStatus)
async def post_interview(
    project_id: str,
    body: InterviewAnswerRequest,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    project_service.get(project_id, user_id=user.id)
    return await project_service.answer_interview(project_id, body.answers, name=body.name)


@router.post("/{project_id}/platform", response_model=ProjectStatus)
def set_platform(
    project_id: str,
    body: SetPlatformRequest,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    project_service.get(project_id, user_id=user.id)
    return project_service.set_platform(project_id, body.platform)


# ── Process blueprint ───────────────────────────────────────────────────────────
#
# Ordered before /{project_id}/generate only for readability; FastAPI matches on the full
# path so these cannot shadow it. Every handler starts with the ownership check that the
# rest of this module uses — `project_service.get(id, user_id=...)` 404s on someone else's
# project rather than leaking its existence.


@router.post("/{project_id}/diagrams", response_model=ProjectStatus | DiagramSetView)
async def generate_diagrams(
    project_id: str,
    demo: bool = Query(False, description="Deterministic placeholder without calling Bedrock"),
    wait: bool = Query(
        False,
        description="If true, block until the blueprint is drawn (CLI). UI polls instead.",
    ),
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus | DiagramSetView:
    project_service.get(project_id, user_id=user.id)
    if wait:
        return await project_service.generate_diagrams(project_id, use_demo=demo)
    return project_service.start_diagrams_background(project_id, use_demo=demo)


@router.get("/{project_id}/diagrams", response_model=DiagramSetView | None)
def get_diagrams(
    project_id: str,
    user: UserOut = Depends(get_current_user),
) -> DiagramSetView | None:
    """Null rather than 404 when there is none: "no blueprint" is a normal project state."""
    project_service.get(project_id, user_id=user.id)
    return project_service.diagrams(project_id)


@router.get("/{project_id}/diagrams/{kind}.png")
def get_diagram_png(
    project_id: str,
    kind: str,
    version: int | None = Query(
        None, description="An earlier version still in the change history. Defaults to current."
    ),
    user: UserOut = Depends(get_current_user),
) -> Response:
    """PNG bytes. Fetched as a blob by the UI — auth is a Bearer header, not a cookie, so a
    bare URL in an `<img src>` would come back 401."""
    project_service.get(project_id, user_id=user.id)
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


@router.get("/{project_id}/plan/architecture/{kind}.png")
def get_architecture_png(
    project_id: str,
    kind: str,
    user: UserOut = Depends(get_current_user),
) -> Response:
    """One of SDD.md §3.2's architectural views — `logical`, `development` or `deployment`.

    Separate from `/diagrams/{kind}.png` because these are a different artefact: the blueprint
    is the process being automated and carries an approved version number, these are the
    software architecture as the current design describes it. Same blob-fetch reason for
    existing at all — a Bearer header means a bare URL in an `<img src>` returns 401.
    """
    project_service.get(project_id, user_id=user.id)
    data = project_service.architecture_png(project_id, kind)
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-cache",
            "Content-Disposition": f'inline; filename="{kind}-view.png"',
        },
    )


@router.get("/{project_id}/plan/solution-design.{fmt}")
def download_solution_design(
    project_id: str,
    fmt: str,
    user: UserOut = Depends(get_current_user),
) -> Response:
    """`SDD.md` as a download — `pdf` or `docx`.

    Both formats come off one parse of the same markdown the Files pane previews, with §3.2's
    three architecture views embedded as images, so what the reader hands to a delivery head
    is the document they reviewed on screen rather than a second rendering of it.

    `attachment`, not `inline`: this is a document to keep, and Chrome's PDF viewer would
    otherwise swallow the filename the disposition carries.
    """
    project_service.get(project_id, user_id=user.id)
    data, filename = project_service.solution_design_document(project_id, fmt)
    return Response(
        content=data,
        media_type=export_documents.media_type(fmt),
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/{project_id}/diagrams/followup", response_model=ProjectStatus | DiagramSetView)
async def followup_diagrams(
    project_id: str,
    body: DiagramFollowupRequest,
    wait: bool = Query(False, description="Block until redrawn (CLI)."),
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus | DiagramSetView:
    """Apply one plain-language change.

    `scope` decides what may move. `all` revises the shared process model and re-lays out all
    three views; a single view (`sipoc`, `flow`, `swimlane`) re-lays out that picture only and
    leaves the model — and so the other two views and anything generated from them — alone.
    """
    project_service.get(project_id, user_id=user.id)
    if wait:
        return await project_service.refine_diagrams(project_id, body.instruction, body.scope)
    return project_service.start_diagram_followup_background(
        project_id, body.instruction, body.scope
    )


@router.post("/{project_id}/diagrams/freeze", response_model=DiagramSetView)
def freeze_diagrams(project_id: str, user: UserOut = Depends(get_current_user)) -> DiagramSetView:
    """Approve the blueprint. Generation is blocked until this happens."""
    project_service.get(project_id, user_id=user.id)
    return project_service.set_diagrams_frozen(project_id, True)


@router.post("/{project_id}/diagrams/unfreeze", response_model=DiagramSetView)
def unfreeze_diagrams(project_id: str, user: UserOut = Depends(get_current_user)) -> DiagramSetView:
    project_service.get(project_id, user_id=user.id)
    return project_service.set_diagrams_frozen(project_id, False)


@router.delete("/{project_id}/diagrams", response_model=ProjectStatus)
def delete_diagrams(project_id: str, user: UserOut = Depends(get_current_user)) -> ProjectStatus:
    """Drop the blueprint. The project then generates straight from the brief, as before."""
    project_service.get(project_id, user_id=user.id)
    return project_service.delete_diagrams(project_id)


@router.post("/{project_id}/generate", response_model=ProjectStatus)
async def generate(
    project_id: str,
    demo: bool = Query(False, description="Use deterministic demo plan without calling Bedrock"),
    wait: bool = Query(
        False,
        description="If true, block until generation finishes (CLI). UI should use wait=false and poll.",
    ),
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    project_service.get(project_id, user_id=user.id)
    if wait:
        return await project_service.generate(project_id, use_demo=demo)
    return project_service.start_generate_background(project_id, use_demo=demo)


@router.get("/{project_id}/plan", response_model=ProjectPlan)
def get_plan(project_id: str, user: UserOut = Depends(get_current_user)) -> ProjectPlan:
    status = project_service.get(project_id, user_id=user.id)
    if not status.plan:
        raise AppError("Plan not ready", status_code=404)
    return status.plan


@router.put("/{project_id}/plan", response_model=ProjectStatus)
def put_plan(
    project_id: str,
    plan: ProjectPlan,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    project_service.get(project_id, user_id=user.id)
    return project_service.update_plan(project_id, plan)


@router.post("/{project_id}/export", response_model=ExportResult)
def export_project(
    project_id: str,
    body: ExportRequest,
    user: UserOut = Depends(get_current_user),
) -> ExportResult:
    project_service.get(project_id, user_id=user.id)
    result, _ = project_service.export(
        project_id, mode=body.mode, output_path=body.output_path
    )
    return result


@router.get("/{project_id}/export/download")
def download_export(project_id: str, user: UserOut = Depends(get_current_user)) -> Response:
    status = project_service.get(project_id, user_id=user.id)
    if not status.platform:
        raise AppError("Nothing to download", status_code=404)
    zip_name = f"{slugify_project_name(status.name)}.zip"
    path = Path("exports") / f"{project_id}-{status.platform.value}.zip"
    if not path.exists():
        result, data = project_service.export(project_id, mode="zip")
        _ = result
        if data:
            return Response(
                content=data,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
            )
        raise AppError("Export missing", status_code=404)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=zip_name,
    )


@router.post("/{project_id}/plan/work-breakdown/generate", response_model=ProjectStatus)
async def generate_work_breakdown_plan(
    project_id: str,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    """Start background Bedrock WORKBREAKDOWN.md generation (poll project progress)."""
    project_service.get(project_id, user_id=user.id)
    return project_service.start_work_breakdown_background(project_id)


@router.post("/{project_id}/plan/solution-design/generate", response_model=ProjectStatus)
async def generate_solution_design_plan(
    project_id: str,
    user: UserOut = Depends(get_current_user),
) -> ProjectStatus:
    """Start background Bedrock SDD.md generation (poll project progress).

    Same authorisation as every other project route — `get_current_user`, scoped to the
    caller's own projects — so the solution design is available to every user, and to an
    admin through the projects they can already open. Nothing here is role-gated.
    """
    project_service.get(project_id, user_id=user.id)
    return project_service.start_solution_design_background(project_id)


@router.get("/{project_id}/export/preview")
def preview_export(project_id: str, user: UserOut = Depends(get_current_user)) -> dict:
    project_service.get(project_id, user_id=user.id)
    files, images = project_service.preview_files(project_id)
    # `images` are in `files` (the tree and the zip hold them) but never in `contents`: they
    # are binary, and the client fetches each one from the diagrams endpoint when opened.
    return {
        "files": sorted([*files.keys(), *images]),
        "contents": files,
        "images": sorted(images),
    }


_ = ProjectPath
