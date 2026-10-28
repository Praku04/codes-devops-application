"""
Build the process model and its three views from a project brief.

Shape of the work: one extraction call produces the `ProcessModel`, then three calls run in
parallel to lay out the SIPOC, flow and swimlane views of it. The parallel calls each get the
whole model and are told not to re-analyse the domain, so they cost little thinking and a lot
of layout — which is what they are for.

A follow-up comes in one of two shapes. A change to the *process* revises the model and then
re-runs all three layout calls, so the views move together. A change aimed at *one view* skips
the model entirely and re-runs that one layout call — same prompt, same reconciliation, plus an
instruction that says presentation only. See `revise_diagram_set`.

Everything they return is then **reconciled against the model in code**, not trusted. Three
independent calls will occasionally drop a step, invent a lane, or point an edge at an id that
does not exist; the checks in `_reconcile_*` repair that deterministically and record what they
had to fix in `warnings`, so a thin extraction shows up in the UI instead of silently producing
a diagram that contradicts the other two.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.models.diagrams import (
    DIAGRAM_KINDS,
    VIEW_NAMES,
    DiagramRevision,
    DiagramScope,
    DiagramSet,
    FlowDiagram,
    FlowNode,
    ProcessActor,
    ProcessEdge,
    ProcessModel,
    ProcessStep,
    SipocDiagram,
    SipocItem,
    SwimlaneDiagram,
    SwimlaneStep,
)
from app.models.schemas import ProjectBrief
from app.services.llm.client import llm_client

logger = logging.getLogger("agentcraft.diagrams")

ProgressCb = Callable[[str], Awaitable[None] | None]

#: How many superseded versions keep their views so the change history can still draw them.
#: Bounded because the whole set is one JSON column: a project that has been iterated thirty
#: times should not carry thirty full models around on every read.
HISTORY_LIMIT = 8

#: Most columns a swimlane is drawn with. Past this the picture is wider than any screen and
#: every column holds one box, which is the flow diagram again rather than a view of the
#: phases. Adjacent columns are merged to fit, and steps in a merged column stack.
#: `diagram_swimlane_system.txt` asks for the same number, so the merge is the backstop for a
#: layout call that ignored it (or for the locally derived fallback) rather than the plan.
MAX_STAGES = 10

_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


def _cap(text: str, limit: int) -> str:
    """Bound a label's length, cutting between words and saying so with an ellipsis.

    These caps exist so one runaway sentence cannot become a box the size of the page; the
    renderer wraps and measures whatever it is given, so the cap is a backstop, not the layout.
    A bare `text[:44]` made it a lie: it stopped mid-word with nothing to mark the cut, so
    "Validate against the retention sched" read as the actual name of the step. Breaking on a
    space and appending "…" keeps the same bound and tells the reader there was more.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit].rstrip()
    # Only honour a word boundary that is actually near the end — a single long word would
    # otherwise collapse to almost nothing.
    space = head.rfind(" ")
    if space >= limit * 0.6:
        head = head[:space].rstrip()
    return f"{head.rstrip(',;:.')}…"


def _prompt(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


async def _emit(cb: ProgressCb | None, message: str) -> None:
    if not cb:
        return
    result = cb(message)
    if asyncio.iscoroutine(result):
        await result


def _now_iso() -> str:
    from app.db.session import _iso_ist, _now

    return _iso_ist(_now()) or ""


# ── Extraction ──────────────────────────────────────────────────────────────────


async def extract_process_model(
    brief: ProjectBrief,
    *,
    on_progress: ProgressCb | None = None,
) -> ProcessModel:
    """One call: brief in, detailed process model out."""
    await _emit(on_progress, "Reading the brief and extracting the end-to-end process…")
    context = brief.as_prompt_context()
    data = await llm_client.complete_json(
        [
            {"role": "system", "content": _prompt("process_model_system.txt")},
            {"role": "user", "content": context},
        ],
        temperature=0.2,
        max_tokens=8000,
    )
    model = ProcessModel.model_validate(_as_dict(data))
    return _repair_model(model)


async def revise_process_model(
    model: ProcessModel,
    instruction: str,
    *,
    on_progress: ProgressCb | None = None,
) -> tuple[ProcessModel, str]:
    """Apply one plain-language instruction to an existing model. Returns (model, summary)."""
    await _emit(on_progress, "Applying your change to the process…")
    data = _as_dict(
        await llm_client.complete_json(
            [
                {"role": "system", "content": _prompt("process_model_followup_system.txt")},
                {
                    "role": "user",
                    "content": (
                        "Current model:\n"
                        + json.dumps(model.model_dump(), indent=2)
                        + f"\n\nInstruction from the user:\n{instruction.strip()}"
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=8000,
        )
    )
    summary = str(data.pop("change_summary", "") or "").strip()
    revised = _repair_model(ProcessModel.model_validate(data))
    return revised, summary


#: Which system prompt lays out which view, and the reconciler that checks the answer against
#: the model. One table so the whole-set pass and the single-view pass cannot drift apart.
_VIEW_SPECS: dict[str, tuple[str, str]] = {
    "sipoc": ("diagram_sipoc_system.txt", "SIPOC"),
    "flow": ("diagram_flow_system.txt", "Process flow"),
    "swimlane": ("diagram_swimlane_system.txt", "Swimlane"),
}

#: What a view-scoped follow-up is actually allowed to rewrite, spelled out per view.
#:
#: Needed because each view's drawing prompt is written for a *first* draw and tells the model to
#: reproduce the process faithfully — "use the model's exact vocabulary", "keep its steps and its
#: topology". Handed a revision without this, the model reads those lines as covering everything
#: on the page and answers "the naming convention remains unchanged; please clarify". The point
#: is that a view's own content is not the process: a SIPOC's supplier and customer entries exist
#: nowhere in the model, so re-wording them cannot contradict the other two views.
_VIEW_EDITABLE: dict[str, str] = {
    "sipoc": (
        "Every column's entries and their notes — suppliers, inputs, the high-level process "
        "phases, outputs, customers, metrics — how many there are, what they are called, and "
        "the title. None of these are in the process model: they are this view's own content, "
        "so re-wording, re-casing, re-grouping, adding or dropping them affects nothing else."
    ),
    "flow": (
        "Every node's label, the detail line and the systems caption under it, the labels on "
        "the edges, and the title. Node ids, kinds, actors and which node leads to which "
        "belong to the process model — keep those exactly as the model has them."
    ),
    "swimlane": (
        "Every step's label, the order of the lanes, how steps are grouped into columns, the "
        "stage name over each column, the labels on the edges, and the title. Step ids, which "
        "actor owns a step, and which step leads to which belong to the process model."
    ),
}


# Every "your follow-up changed nothing" message opens with this. It is advice, not a fault:
# nothing is broken and nothing was lost, the instruction just needs to be more specific. The
# wizard and the CLI both test for this opening to show it in a neutral tone rather than as a
# failure, so keep it as the literal first words of every `ViewUnchanged` message.
NO_CHANGE_OPENING = "Nothing changed"


class ViewUnchanged(Exception):
    """A follow-up produced no change at all, so no version should be recorded for it.

    The failure this guards against is specific and was seen in the wild: asked to "change the
    naming convention for suppliers and customers", the layout call answered that the convention
    was unchanged and asked which one to use. Recording that as v2 gives the user a new version,
    an un-frozen blueprint and an identical picture — the worst of the three outcomes, because it
    looks like the change was applied. Better to keep the current version and say what happened.
    """


def _reconcile_view(
    kind: str,
    raw: dict[str, Any] | None,
    model: ProcessModel,
    warnings: list[str],
) -> SipocDiagram | FlowDiagram | SwimlaneDiagram:
    """Check one layout answer against the model. `raw=None` derives the view in code."""
    if kind == "sipoc":
        return _reconcile_sipoc(raw, model, warnings)
    if kind == "flow":
        return _reconcile_flow(raw, model, warnings)
    return _reconcile_swimlane(raw, model, warnings)


async def revise_view(
    model: ProcessModel,
    kind: str,
    current_view: SipocDiagram | FlowDiagram | SwimlaneDiagram,
    instruction: str,
    *,
    on_progress: ProgressCb | None = None,
) -> tuple[SipocDiagram | FlowDiagram | SwimlaneDiagram, str, list[str]]:
    """Re-lay out one view under an instruction, leaving the process model alone.

    The model is passed in read-only on purpose. This is the "only this diagram" path, and the
    thing that makes it safe is that the process cannot move: the layout call is told to change
    presentation only, and whatever it returns is reconciled against the *unchanged* model, so a
    call that tries to add a step has the step removed and says so in the warnings. The other two
    views are not touched and therefore cannot be contradicted.

    Returns (view, summary, warnings). Raises `ViewUnchanged` when two attempts both come back
    identical to what the user is already looking at — see that class for why.
    """
    prompt_file, label = _VIEW_SPECS[kind]
    view_name = VIEW_NAMES.get(kind, kind)
    await _emit(on_progress, f"Re-laying out the {view_name} view — the process is unchanged…")

    # The drawing prompt supplies the schema and the house style; the revision addendum tells it
    # this is an edit and that the view's own wording is fair game. Concatenated rather than sent
    # as two system turns because the client collapses to a single system prompt.
    system = f"{_prompt(prompt_file)}\n\n{_prompt('diagram_view_followup_system.txt')}"
    guidance = (
        f"Process model (authoritative — do NOT change what it says the process is):\n"
        f"{json.dumps(model.model_dump(), indent=2)}\n\n"
        f"Your current layout of this view — this is what the user is looking at:\n"
        f"{json.dumps(current_view.model_dump(), indent=2)}\n\n"
        f"Their instruction about this {view_name} view:\n{instruction.strip()}\n\n"
        f"On this view you may rewrite: {_VIEW_EDITABLE[kind]}\n\n"
        "Return the full JSON for this view — every field, including the parts you did not "
        "change — plus a `change_summary` string. Apply the instruction; do not ask about it."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": guidance},
    ]

    raw, summary = await _view_layout_call(messages, label)
    warnings: list[str] = []
    view = _reconcile_view(kind, raw, model, warnings)

    if view == current_view:
        # The instruction was understood as a request for permission, or the answer was a
        # verbatim echo. Either way the user asked for something, so ask again with their own
        # non-answer quoted back before giving up — a second attempt is cheap next to the
        # alternative, which is a version number that promises a change and delivers none.
        await _emit(
            on_progress,
            f"The {view_name} came back unchanged — asking again with a firmer instruction…",
        )
        retry = [
            *messages,
            {"role": "assistant", "content": json.dumps(view.model_dump())},
            {
                "role": "user",
                "content": (
                    "That is byte-for-byte the layout you were given, so nothing changed and "
                    f"the user's request was not carried out.{_echo(summary)} You cannot ask a "
                    "question here and no one will answer one. Apply the instruction now: "
                    "choose the most reasonable reading of it, rewrite whatever this view lets "
                    "you rewrite so the change is visible in the drawing, and put the reading "
                    "you chose in `change_summary`. Return the full JSON for this view again."
                ),
            },
        ]
        raw2, summary2 = await _view_layout_call(retry, label)
        if raw2 is not None:
            warnings2: list[str] = []
            view2 = _reconcile_view(kind, raw2, model, warnings2)
            if view2 != current_view:
                return view2, summary2 or f"Re-laid out the {view_name} view.", warnings2
        raise ViewUnchanged(
            f"{NO_CHANGE_OPENING} — the {view_name} came back exactly as it was, twice, so "
            "nothing was saved and the blueprint is still on the version you were looking at. "
            + (f"The model said: “{summary or summary2}”. " if (summary or summary2) else "")
            + "Try naming the change concretely — which entries, and what they should say "
            "instead (for example “title-case the supplier and customer names and drop the "
            "trailing colons”). If what you want is a change to the process itself rather than "
            "to this picture, apply it to all three diagrams instead."
        )

    if raw is None:
        # The call failed, so what came back is the locally derived layout — a real change to
        # the picture, just not the one that was asked for. Saying so beats a version whose
        # summary claims a change it did not make.
        summary = (
            f"The {view_name} layout call failed, so this view was derived from the process "
            "model instead. Nothing about the process changed."
        )
    return view, summary or f"Re-laid out the {view_name} view.", warnings


async def _view_layout_call(
    messages: list[dict[str, Any]], label: str
) -> tuple[dict[str, Any] | None, str]:
    """One layout call. Returns (raw, change_summary); `(None, "")` when the call failed.

    A failed call is not fatal for a view — the reconciler derives it from the model instead —
    so this swallows the exception the way `build_views` does.
    """
    try:
        raw = _as_dict(
            await llm_client.complete_json(
                messages,
                temperature=0.1,
                max_tokens=6000,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s revision failed: %s", label, exc)
        return None, ""
    return raw, str(raw.pop("change_summary", "") or "").strip()


def _echo(summary: str) -> str:
    """The model's own words, quoted back at it. Empty when it said nothing."""
    return f' You said: "{summary.strip()}".' if summary.strip() else ""


# ── The three views, in parallel ────────────────────────────────────────────────


async def build_views(
    model: ProcessModel,
    *,
    on_progress: ProgressCb | None = None,
) -> tuple[SipocDiagram, FlowDiagram, SwimlaneDiagram, list[str]]:
    """Lay out all three views concurrently, then reconcile each against the model."""
    await _emit(on_progress, "Drawing SIPOC, process flow and swimlane…")
    model_json = json.dumps(model.model_dump(), indent=2)

    async def view(prompt_file: str, label: str) -> dict[str, Any] | None:
        try:
            return _as_dict(
                await llm_client.complete_json(
                    [
                        {"role": "system", "content": _prompt(prompt_file)},
                        {"role": "user", "content": f"Process model:\n{model_json}"},
                    ],
                    temperature=0.1,
                    max_tokens=6000,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # One failed view must not lose the other two, and the model itself is intact,
            # so fall back to deriving this view in code rather than failing the request.
            logger.warning("%s view failed, deriving it locally: %s", label, exc)
            return None

    raw_sipoc, raw_flow, raw_swim = await asyncio.gather(
        *(view(*_VIEW_SPECS[kind]) for kind in DIAGRAM_KINDS)
    )

    warnings: list[str] = []
    sipoc = _reconcile_sipoc(raw_sipoc, model, warnings)
    flow = _reconcile_flow(raw_flow, model, warnings)
    swimlane = _reconcile_swimlane(raw_swim, model, warnings)
    return sipoc, flow, swimlane, warnings


async def build_diagram_set(
    brief: ProjectBrief,
    *,
    on_progress: ProgressCb | None = None,
) -> DiagramSet:
    """Full first pass: extract, then lay out the three views."""
    model = await extract_process_model(brief, on_progress=on_progress)
    sipoc, flow, swimlane, warnings = await build_views(model, on_progress=on_progress)
    now = _now_iso()
    return DiagramSet(
        version=1,
        model=model,
        sipoc=sipoc,
        flow=flow,
        swimlane=swimlane,
        warnings=warnings,
        revisions=[DiagramRevision(version=1, instruction="", summary="First draft", created_at=now)],
        created_at=now,
        updated_at=now,
    )


def _warnings_for_other_views(existing: list[str], kind: str) -> list[str]:
    """This version's warnings, minus the ones the view being re-laid-out had raised.

    Each reconciler prefixes its warnings with the view's own name, so the two other views'
    findings survive a single-view revision while the stale ones for this view are dropped and
    replaced by whatever the new layout raises.
    """
    prefix = _VIEW_SPECS[kind][1]
    return [w for w in existing if not w.startswith(prefix)]


async def revise_diagram_set(
    current: DiagramSet,
    instruction: str,
    *,
    scope: DiagramScope = "all",
    on_progress: ProgressCb | None = None,
) -> DiagramSet:
    """Apply a follow-up and bump the version.

    `scope="all"` revises the process model and re-lays out all three views — the only path that
    can change what the process *is*. A single view re-lays out that picture from the unchanged
    model, so the other two views, the model, and therefore everything generated from it are
    left exactly as they were.
    """
    if scope != "all":
        return await _revise_one_view(current, instruction, scope, on_progress=on_progress)

    model, summary = await revise_process_model(
        current.model, instruction, on_progress=on_progress
    )
    sipoc, flow, swimlane, warnings = await build_views(model, on_progress=on_progress)
    if (
        model == current.model
        and sipoc == current.sipoc
        and flow == current.flow
        and swimlane == current.swimlane
    ):
        # Same reasoning as the single-view guard: a version whose diagrams are identical to the
        # previous one looks like the change was applied and is worse than being told it was not.
        raise ViewUnchanged(
            f"{NO_CHANGE_OPENING} — the process and all three views came back exactly as they "
            "were, so no new version was saved. "
            + (f"The model said: “{summary}”. " if summary else "")
            + "Try naming the step, actor or handoff you want different, and what it should be."
        )
    version = current.version + 1
    now = _now_iso()
    return current.model_copy(
        update={
            "version": version,
            "model": model,
            "sipoc": sipoc,
            "flow": flow,
            "swimlane": swimlane,
            "warnings": warnings,
            # A revision un-freezes by construction: the frozen model is what generation
            # reads, so it must never drift from the diagrams the user is looking at.
            "frozen": False,
            "frozen_at": None,
            # Keep what is being replaced. "Show me what v1 looked like" is the question the
            # change history exists to answer, and a summary line cannot answer it.
            "history": [*current.history, current.snapshot()][-HISTORY_LIMIT:],
            "revisions": [
                *current.revisions,
                DiagramRevision(
                    version=version,
                    instruction=instruction.strip(),
                    summary=summary or "Applied the requested change",
                    scope="all",
                    created_at=now,
                ),
            ],
            "updated_at": now,
        }
    )


async def _revise_one_view(
    current: DiagramSet,
    instruction: str,
    kind: str,
    *,
    on_progress: ProgressCb | None = None,
) -> DiagramSet:
    """The "only this diagram" path — one layout call, model and other views untouched."""
    view, summary, warnings = await revise_view(
        current.model,
        kind,
        getattr(current, kind),
        instruction,
        on_progress=on_progress,
    )
    version = current.version + 1
    now = _now_iso()
    return current.model_copy(
        update={
            "version": version,
            kind: view,
            "warnings": [*_warnings_for_other_views(current.warnings, kind), *warnings],
            # Still un-freezes, even though the model did not move. The approval is of a
            # blueprint the user was looking at, and this changes what they would be looking
            # at — so it is re-approved deliberately rather than silently inherited.
            "frozen": False,
            "frozen_at": None,
            "history": [*current.history, current.snapshot()][-HISTORY_LIMIT:],
            "revisions": [
                *current.revisions,
                DiagramRevision(
                    version=version,
                    instruction=instruction.strip(),
                    summary=summary,
                    scope=kind,  # type: ignore[arg-type]
                    created_at=now,
                ),
            ],
            "updated_at": now,
        }
    )


# ── Reconciliation ──────────────────────────────────────────────────────────────


def _as_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    raise ValueError(f"Expected a JSON object, got {type(data).__name__}")


def _repair_model(model: ProcessModel) -> ProcessModel:
    """Make the model internally consistent before anything is drawn from it.

    A model that references a missing step will render as an arrow into empty space, so the
    cheapest place to fix it is here — once — rather than in each of the three renderers.
    """
    steps = [s for s in model.steps if s.id and s.name]
    ids = {s.id for s in steps}
    edges = [e for e in model.edges if e.source in ids and e.target in ids and e.source != e.target]

    # Deduplicate edges; the follow-up pass sometimes re-emits one it also kept.
    seen: set[tuple[str, str, str]] = set()
    unique: list[ProcessEdge] = []
    for e in edges:
        key = (e.source, e.target, e.label)
        if key not in seen:
            seen.add(key)
            unique.append(e)

    # Exactly one start. Extra starts become plain tasks rather than being dropped — the
    # work they describe is real even when the kind is wrong.
    starts = [s for s in steps if s.kind == "start"]
    if len(starts) > 1:
        steps = [
            s.model_copy(update={"kind": "task"}) if s.kind == "start" and s is not starts[0] else s
            for s in steps
        ]
    elif not starts and steps:
        steps = [steps[0].model_copy(update={"kind": "start"}), *steps[1:]]

    if not any(s.kind == "end" for s in steps) and len(steps) > 1:
        steps = [*steps[:-1], steps[-1].model_copy(update={"kind": "end"})]

    # Every actor named on a step must exist as an actor, or its swimlane has no home.
    known = {a.name for a in model.actors}
    invented = [s.actor for s in steps if s.actor and s.actor not in known]
    actors = list(model.actors)
    for name in dict.fromkeys(invented):
        actors.append(ProcessActor(name=name, kind="system"))

    return model.model_copy(update={"steps": steps, "edges": unique, "actors": actors})


def _reconcile_sipoc(raw: dict[str, Any] | None, model: ProcessModel, warnings: list[str]) -> SipocDiagram:
    if raw is None:
        warnings.append("SIPOC was derived from the process model locally (the layout call failed).")
        return _derive_sipoc(model)
    try:
        sipoc = SipocDiagram.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SIPOC validation failed: %s", exc)
        warnings.append("SIPOC did not match the expected shape and was derived locally.")
        return _derive_sipoc(model)

    if not sipoc.title:
        sipoc = sipoc.model_copy(update={"title": model.title})
    # An empty column draws as a blank table cell, which reads as a bug. Fill from the model.
    derived = _derive_sipoc(model)
    patch: dict[str, Any] = {}
    for column in ("suppliers", "inputs", "process", "outputs", "customers"):
        if not getattr(sipoc, column):
            patch[column] = getattr(derived, column)
            warnings.append(f"SIPOC '{column}' came back empty and was filled from the process model.")
    if not sipoc.metrics and model.kpis:
        patch["metrics"] = model.kpis[:6]
    return sipoc.model_copy(update=patch) if patch else sipoc


def _reconcile_flow(raw: dict[str, Any] | None, model: ProcessModel, warnings: list[str]) -> FlowDiagram:
    if raw is None:
        warnings.append("Process flow was derived from the process model locally (the layout call failed).")
        return _derive_flow(model)
    try:
        flow = FlowDiagram.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Flow validation failed: %s", exc)
        warnings.append("Process flow did not match the expected shape and was derived locally.")
        return _derive_flow(model)

    by_id = {n.id: n for n in flow.nodes if n.id}
    missing = [s for s in model.steps if s.id not in by_id]
    for step in missing:
        by_id[step.id] = FlowNode(
            id=step.id,
            label=_cap(step.name, 52),
            kind=step.kind,
            actor=step.actor,
            systems=step.systems[:2],
            detail=step.description.strip(),
        )
    if missing:
        warnings.append(
            f"Process flow was missing {len(missing)} step(s) from the model; they were added back."
        )
    # Drop nodes the model does not know about — they are the layout pass inventing work.
    extra = [nid for nid in by_id if not model.step_by_id(nid)]
    for nid in extra:
        by_id.pop(nid)
    if extra:
        warnings.append(f"Process flow invented {len(extra)} step(s) not in the model; they were removed.")

    # The detail line under each box comes from the model, never from this response — one
    # sentence, one owner. Whatever the layout call sent in `detail` is overwritten.
    nodes = [
        by_id[s.id].model_copy(update={"detail": s.description.strip()})
        for s in model.steps
        if s.id in by_id
    ]
    edges = _clean_edges(flow.edges or model.edges, {n.id for n in nodes})
    if not edges:
        edges = _clean_edges(model.edges, {n.id for n in nodes})
    return FlowDiagram(title=flow.title or model.title, nodes=nodes, edges=edges)


def _reconcile_swimlane(
    raw: dict[str, Any] | None, model: ProcessModel, warnings: list[str]
) -> SwimlaneDiagram:
    if raw is None:
        warnings.append("Swimlane was derived from the process model locally (the layout call failed).")
        return _derive_swimlane(model)
    try:
        swim = SwimlaneDiagram.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Swimlane validation failed: %s", exc)
        warnings.append("Swimlane did not match the expected shape and was derived locally.")
        return _derive_swimlane(model)

    lanes = [lane for lane in swim.lanes if lane.strip()]
    by_id = {s.id: s for s in swim.steps if s.id}
    derived = _derive_swimlane(model)

    missing = [s for s in model.steps if s.id not in by_id]
    if missing:
        fallback = {s.id: s for s in derived.steps}
        for step in missing:
            by_id[step.id] = fallback[step.id]
        warnings.append(f"Swimlane was missing {len(missing)} step(s) from the model; they were added back.")

    extra = [sid for sid in by_id if not model.step_by_id(sid)]
    for sid in extra:
        by_id.pop(sid)
    if extra:
        warnings.append(f"Swimlane invented {len(extra)} step(s) not in the model; they were removed.")

    # A step whose lane is not a declared lane has nowhere to be drawn. Prefer the model's
    # actor for that step, and only then fall back to adding the lane the layout asked for.
    fixed: list[SwimlaneStep] = []
    for step in by_id.values():
        lane = step.lane
        if lane not in lanes:
            actor = (model.step_by_id(step.id) or ProcessStep(id="", name="")).actor
            if actor and actor in lanes:
                lane = actor
            else:
                lane = lane or actor or "Process"
                if lane not in lanes:
                    lanes.append(lane)
        fixed.append(step.model_copy(update={"lane": lane}))

    fixed = _compact_columns(fixed)
    # Name the columns while the given names still line up with them, then merge down: a
    # bucketed column keeps the name of the first stage it swallowed.
    fixed, stages = _bucket_columns(fixed, _stage_names(swim.stages, fixed, model))
    lanes = [lane for lane in lanes if any(s.lane == lane for s in fixed)]
    edges = _clean_edges(swim.edges or model.edges, {s.id for s in fixed})
    if not edges:
        edges = _clean_edges(model.edges, {s.id for s in fixed})
    return SwimlaneDiagram(
        title=swim.title or model.title,
        lanes=lanes or derived.lanes,
        stages=stages,
        steps=sorted(fixed, key=lambda s: (s.column, s.lane)),
        edges=edges,
    )


def _stage_names(
    given: list[str], steps: list[SwimlaneStep], model: ProcessModel
) -> list[str]:
    """One phase name per column, filled in from the model when the layout call did not give one.

    Columns are numbered anyway, so this is pure gain: it tells the reader what the column of
    boxes has in common. A wrong count is ignored rather than shifted into place — a stage name
    against the wrong column is worse than no stage name.
    """
    columns = sorted({s.column for s in steps})
    if len(given) == len(columns) and all(g.strip() for g in given):
        return [_cap(g, 30) for g in given]
    names: list[str] = []
    for col in columns:
        members = [s for s in steps if s.column == col]
        # The model's own wording, not the shortened box label: this caption has more room.
        first = next((model.step_by_id(m.id) for m in members if model.step_by_id(m.id)), None)
        label = (first.name if first else members[0].label if members else "").strip()
        names.append(_cap(label, 30))
    return names


def _clean_edges(edges: list[ProcessEdge], ids: set[str]) -> list[ProcessEdge]:
    out: list[ProcessEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for e in edges:
        if e.source not in ids or e.target not in ids or e.source == e.target:
            continue
        key = (e.source, e.target, e.label)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _compact_columns(steps: list[SwimlaneStep]) -> list[SwimlaneStep]:
    """Close gaps in the column numbering so the drawing has no empty vertical bands."""
    used = sorted({s.column for s in steps})
    remap = {old: new for new, old in enumerate(used)}
    return [s.model_copy(update={"column": remap[s.column]}) for s in steps]


def _bucket_columns(
    steps: list[SwimlaneStep], stages: list[str], *, limit: int = MAX_STAGES
) -> tuple[list[SwimlaneStep], list[str]]:
    """Merge adjacent columns until there are at most `limit` of them.

    One column per step is not a stage view — it is the flow diagram stretched to five times
    the width, with every lane so sparsely filled that the handoff arrows are all anyone can
    see. Merged columns are drawn as a stack, so the steps keep their order and the diagram
    keeps a shape a screen can hold; each merged column takes the name of the first stage it
    swallowed, which is the one the reader meets first.
    """
    used = sorted({s.column for s in steps})
    if len(used) <= limit:
        return steps, stages
    bucket = {col: i * limit // len(used) for i, col in enumerate(used)}
    merged = [s.model_copy(update={"column": bucket[s.column]}) for s in steps]
    names: list[str] = []
    for b in range(limit):
        first = next(
            (stages[i] for i, col in enumerate(used) if bucket[col] == b and i < len(stages)),
            "",
        )
        names.append(first)
    return merged, (names if any(n.strip() for n in names) else [])


# ── Local derivation (no LLM) ───────────────────────────────────────────────────
#
# Used when a layout call fails and by the `demo` path, which must work with no Bedrock at
# all. These are honest projections of the model — plainer than the LLM's phrasing, but never
# inconsistent with the other views, which is the property that matters.


def _derive_sipoc(model: ProcessModel) -> SipocDiagram:
    """A SIPOC read off the model, with a note on every cell saying where it came from.

    The notes are the point. A column of bare nouns ("Claim record", "Claimant") is a table
    the reader has to take on trust; "supplies Claim record · Submit claim" can be checked
    against the flow diagram next to it. They also stop the derived SIPOC from looking thin
    next to the model's own — this runs in demo mode and whenever the layout call fails, and
    it is now the first thing anyone sees on the Blueprint step.
    """
    early = model.steps[: max(1, len(model.steps) // 3)]
    late = model.steps[-max(1, len(model.steps) // 3) :]

    # Suppliers are whoever hands work in. The actors who own the opening steps first — the
    # start step's actor is a supplier by definition, it is where work arrives — then the
    # third parties, which supply from outside wherever they appear. Internal services come
    # last and only to fill the column, because "the Validation Engine supplies the claim"
    # is true but says less than "the claimant does".
    starters = _dedupe(s.actor for s in early if s.actor)
    kinds = {a.name: a.kind for a in model.actors}
    supplier_names = _dedupe(
        [
            *(n for n in starters if kinds.get(n) != "system"),
            *(a.name for a in model.actors if a.kind == "external"),
            *starters,
        ]
    ) or ["Requester"]
    supplies: dict[str, list[str]] = {}
    for step in early:
        for item in step.inputs:
            if step.actor and item:
                supplies.setdefault(step.actor, []).append(item)
    by_actor = {a.name: a for a in model.actors}

    def supplier_note(name: str) -> str:
        given = _dedupe(supplies.get(name, []))[:2]
        if given:
            return f"supplies {', '.join(given)}"
        actor = by_actor.get(name)
        return (actor.responsibilities if actor else "") or "hands work into the process"

    # Inputs and outputs: the records the early and late steps read and write, each noted with
    # the step that touches it, then topped up from the model's data objects so the column is
    # never one lonely cell when every step reads the same record.
    def records(steps: list[ProcessStep], attr: str, verb: str) -> list[SipocItem]:
        seen: dict[str, str] = {}
        for step in steps:
            for item in getattr(step, attr):
                if item and item not in seen:
                    seen[item] = f"{verb} {step.name}"
        for obj in model.data_objects:
            if len(seen) >= 5:
                break
            if obj and obj not in seen:
                seen[obj] = "process record"
        return [
            SipocItem(name=_cap(n, 44), note=_cap(note, 60))
            for n, note in list(seen.items())[:5]
        ]

    # Customers are whoever receives an outcome: never the services doing the work.
    consumers = _dedupe(
        [
            *(s.actor for s in late if s.actor and kinds.get(s.actor) != "system"),
            *(a.name for a in model.actors if a.kind != "system"),
        ]
    ) or ["Process owner"]
    receives: dict[str, list[str]] = {}
    for step in late:
        for item in step.outputs:
            if step.actor and item:
                receives.setdefault(step.actor, []).append(item)

    def customer_note(name: str) -> str:
        got = _dedupe(receives.get(name, []))[:2]
        if got:
            return f"receives {', '.join(got)}"
        actor = by_actor.get(name)
        return (actor.responsibilities if actor else "") or "depends on the outcome"

    # Collapse to phases by taking an even spread — never the whole step list, which is what
    # makes a SIPOC unreadable. The note names who owns that phase.
    picked = _spread(model.steps, 6)

    return SipocDiagram(
        title=model.title,
        suppliers=[
            SipocItem(name=_cap(n, 44), note=_cap(supplier_note(n), 60))
            for n in supplier_names[:5]
        ],
        inputs=records(early, "inputs", "read by"),
        process=[SipocItem(name=_cap(s.name, 44), note=_cap(s.actor or "", 60)) for s in picked],
        outputs=records(late, "outputs", "written by"),
        customers=[
            SipocItem(name=_cap(n, 44), note=_cap(customer_note(n), 60))
            for n in consumers[:5]
        ],
        metrics=model.kpis[:6],
    )


def _derive_flow(model: ProcessModel) -> FlowDiagram:
    return FlowDiagram(
        title=model.title,
        nodes=[
            FlowNode(
                id=s.id,
                label=_cap(s.name, 52),
                kind=s.kind,
                actor=s.actor,
                systems=s.systems[:2],
                detail=s.description.strip(),
            )
            for s in model.steps
        ],
        edges=list(model.edges),
    )


def _derive_swimlane(model: ProcessModel) -> SwimlaneDiagram:
    """Columns by longest-path depth from the start, which is what a reader expects."""
    depth = _depths(model)
    lanes: list[str] = []
    for s in model.steps:
        lane = s.actor or "Process"
        if lane not in lanes:
            lanes.append(lane)
    steps = [
        SwimlaneStep(
            id=s.id,
            label=_cap(s.name, 40),
            lane=s.actor or "Process",
            column=depth.get(s.id, 0),
            kind=s.kind,
        )
        for s in model.steps
    ]
    compacted = _compact_columns(steps)
    compacted, stages = _bucket_columns(compacted, _stage_names([], compacted, model))
    return SwimlaneDiagram(
        title=model.title,
        lanes=lanes,
        stages=stages,
        steps=compacted,
        edges=list(model.edges),
    )


def _depths(model: ProcessModel) -> dict[str, int]:
    """Longest-path depth per step, ignoring loop-backs so a cycle cannot hang this."""
    order = [s.id for s in model.steps]
    rank = {sid: i for i, sid in enumerate(order)}
    forward: dict[str, list[str]] = {sid: [] for sid in order}
    for e in model.edges:
        if e.source in rank and e.target in rank and rank[e.target] > rank[e.source]:
            forward[e.source].append(e.target)

    depth = {sid: 0 for sid in order}
    for sid in order:  # already topological for forward edges, by construction of `rank`
        for nxt in forward[sid]:
            depth[nxt] = max(depth[nxt], depth[sid] + 1)
    return depth


def _dedupe(items) -> list[str]:
    return list(dict.fromkeys(x for x in items if x and x.strip()))


def _spread(items: list[Any], count: int) -> list[Any]:
    """Pick `count` entries spread evenly across the list, keeping first and last."""
    if len(items) <= count:
        return items
    step = (len(items) - 1) / (count - 1)
    return [items[round(i * step)] for i in range(count)]


#: Section names an expanded brief opens with. They name the section, not the project, so a
#: title taken from one of them says nothing.
_BRIEF_SECTIONS = frozenset(
    {
        "product",
        "problem",
        "problem statement",
        "overview",
        "context",
        "goal",
        "goals",
        "scope",
        "summary",
        "users",
        "tech stack",
        "constraints",
    }
)


def _demo_title(statement: str) -> str:
    """The first line of the statement that reads like a title, without markdown decoration.

    An expanded brief begins `## Product`, and line one taken verbatim put a literal
    "## Product" in the header of all three demo diagrams. So: skip the section headings, drop
    the markers, and cut on a word boundary — never mid-word, which is the whole reason the
    renderers measure instead of slicing.
    """
    for raw in (statement or "").splitlines():
        line = raw.strip().lstrip("#>-*• ").strip().strip("*`_ ")
        if not line or line.rstrip(":").lower() in _BRIEF_SECTIONS:
            continue
        if len(line) <= 60:
            return line
        # A first sentence that fits is the best title available — "A claims intake portal for a
        # mid-size general insurer." beats the same words cut after "insurer. A…".
        stop = min((i for i in (line.find(f"{p} ") for p in ".?!") if 12 <= i <= 59), default=-1)
        if stop >= 0:
            return line[:stop]
        words = line[:60].split(" ")[:-1] or [line[:60]]
        # Drop a dangling initial or article the cut left behind.
        while len(words) > 1 and len(words[-1]) <= 2:
            words.pop()
        return f"{' '.join(words).rstrip(' ,;:.')}…"
    return "Project intake"


def demo_diagram_set(brief: ProjectBrief) -> DiagramSet:
    """A deterministic set for the `demo=true` path — no Bedrock call at all."""
    title = _demo_title(brief.problem_statement)
    actors = [
        ProcessActor(name="Requester", kind="human", responsibilities="Submits the request"),
        ProcessActor(name="Intake Service", kind="system", responsibilities="Validates and records"),
        ProcessActor(name="Reviewer", kind="human", responsibilities="Approves or rejects"),
        ProcessActor(name="Notification Service", kind="system", responsibilities="Informs parties"),
    ]
    steps = [
        ProcessStep(id="s1", name="Submit request", kind="start", actor="Requester", outputs=["Request"]),
        ProcessStep(id="s2", name="Validate request", kind="task", actor="Intake Service", inputs=["Request"]),
        ProcessStep(id="s3", name="Request complete?", kind="decision", actor="Intake Service"),
        ProcessStep(id="s4", name="Return for correction", kind="task", actor="Notification Service"),
        ProcessStep(id="s5", name="Review request", kind="task", actor="Reviewer", inputs=["Request"]),
        ProcessStep(id="s6", name="Approved?", kind="decision", actor="Reviewer"),
        ProcessStep(id="s7", name="Record decision", kind="task", actor="Intake Service", outputs=["Decision"]),
        ProcessStep(id="s8", name="Notify requester", kind="task", actor="Notification Service"),
        ProcessStep(id="s9", name="Closed", kind="end", actor="Intake Service"),
    ]
    edges = [
        ProcessEdge(source="s1", target="s2"),
        ProcessEdge(source="s2", target="s3"),
        ProcessEdge(source="s3", target="s5", label="complete"),
        ProcessEdge(source="s3", target="s4", label="incomplete"),
        ProcessEdge(source="s4", target="s1", label="resubmit"),
        ProcessEdge(source="s5", target="s6"),
        ProcessEdge(source="s6", target="s7", label="approved"),
        ProcessEdge(source="s6", target="s8", label="rejected"),
        ProcessEdge(source="s7", target="s8"),
        ProcessEdge(source="s8", target="s9"),
    ]
    model = ProcessModel(
        title=title,
        scope="Demo blueprint — generated without calling Bedrock.",
        actors=actors,
        systems=["Intake Service", "Notification Service"],
        steps=steps,
        edges=edges,
        data_objects=["Request", "Decision"],
        kpis=["Time to decision", "Rework rate"],
        assumptions=["Placeholder process — run generate without demo mode for a real one."],
    )
    now = _now_iso()
    return DiagramSet(
        version=1,
        llm=False,
        model=model,
        sipoc=_derive_sipoc(model),
        flow=_derive_flow(model),
        swimlane=_derive_swimlane(model),
        warnings=["Demo mode — these diagrams are a placeholder, not an analysis of your brief."],
        revisions=[DiagramRevision(version=1, summary="Demo draft", created_at=now)],
        created_at=now,
        updated_at=now,
    )
