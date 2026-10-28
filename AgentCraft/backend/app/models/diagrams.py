"""
Process diagrams shown for approval before any agent, skill, or rule is generated.

Three diagrams — SIPOC, process flow, and swimlane — are **views of one `ProcessModel`**.
Generating them from three independent prompts is how you get a swimlane whose steps do not
appear in the SIPOC's process column: each answer is locally plausible and they disagree.
One shared model makes that impossible, and a follow-up edit aimed at the *process* lands on
the model so all three views move together.

A follow-up can also be aimed at one view (`DiagramScope`), and then it deliberately does not
touch the model: it re-lays out that picture only. The distinction is the whole reason the
choice can be offered — "only this diagram" means "change how this one is drawn", never "let
this one disagree with the others about what the process is".

The model is also what the follow-up loop rewrites. The PNG is a *render* of it, never the
thing being edited — asking an image model to redraw a picture "but with one step changed"
does not work, and nothing downstream could read the result anyway. The frozen model is fed
to `deduce_plan`, so the agents and skills follow the process the user actually approved.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: The three renderable views. Used as URL segments and as PNG filename stems.
DiagramKind = Literal["sipoc", "flow", "swimlane"]
DIAGRAM_KINDS: tuple[str, ...] = ("sipoc", "flow", "swimlane")

#: What a follow-up is allowed to touch.
#:
#: `all` revises the shared `ProcessModel` and re-lays out all three views — the only way to
#: change the process itself, and the reason the views never disagree. A single kind revises
#: **that view's layout only** and leaves the model untouched: how this picture presents the
#: process, not what the process is. Keeping the two apart is what makes "only this diagram"
#: an honest offer instead of a way to end up with three views of three different processes.
DiagramScope = Literal["all", "sipoc", "flow", "swimlane"]

#: Human wording for a scope, used in progress lines, the CLI and the admin panel.
SCOPE_LABELS: dict[str, str] = {
    "all": "All three views",
    "sipoc": "SIPOC only",
    "flow": "Process flow only",
    "swimlane": "Swimlane only",
}

#: The name of a view as it is spoken about, for progress messages and prompts.
VIEW_NAMES: dict[str, str] = {
    "sipoc": "SIPOC",
    "flow": "process flow",
    "swimlane": "swimlane",
}


def scope_label(scope: str | None) -> str:
    """`"All three views"` / `"SIPOC only"`. Unknown or missing reads as all — the scope field
    postdates the first revisions, and every one of those did redraw all three."""
    return SCOPE_LABELS.get((scope or "all").strip().lower(), SCOPE_LABELS["all"])

#: Step shapes, in the vocabulary the renderer draws: rounded ends, rectangles for work,
#: diamonds for branches, a thicker box for anything that expands into its own flow.
StepKind = Literal["start", "task", "decision", "subprocess", "end"]


class ProcessActor(BaseModel):
    """Whoever performs work — a person, a service, or a third party.

    Actors become swimlanes, so `kind` matters visually: system lanes are tinted
    differently from human ones, which is what makes a handoff readable at a glance.
    """

    name: str
    kind: Literal["human", "system", "external"] = "human"
    responsibilities: str = ""


class ProcessStep(BaseModel):
    id: str = Field(description="Short stable id (s1, s2…) referenced by edges")
    name: str
    kind: StepKind = "task"
    actor: str = Field(default="", description="Must match a ProcessActor.name")
    description: str = ""
    systems: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)


class ProcessEdge(BaseModel):
    source: str
    target: str
    label: str = Field(
        default="",
        description="Branch condition when the source is a decision (e.g. 'rejected')",
    )


class ProcessModel(BaseModel):
    """The extracted process — the single source of truth for all three views."""

    title: str = ""
    scope: str = Field(default="", description="What this process covers, one or two lines")
    actors: list[ProcessActor] = Field(default_factory=list)
    systems: list[str] = Field(default_factory=list)
    steps: list[ProcessStep] = Field(default_factory=list)
    edges: list[ProcessEdge] = Field(default_factory=list)
    data_objects: list[str] = Field(
        default_factory=list,
        description="Records the process reads or writes (case file, audit log, invoice…)",
    )
    kpis: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list,
        description="Anything inferred because the source material was silent — shown to the "
        "user so a wrong guess is corrected before it reaches the generated agents",
    )

    def step_by_id(self, step_id: str) -> ProcessStep | None:
        return next((s for s in self.steps if s.id == step_id), None)


class SipocItem(BaseModel):
    name: str
    note: str = Field(default="", description="One short qualifier — a requirement or format")


class SipocDiagram(BaseModel):
    """Suppliers → Inputs → Process → Outputs → Customers, plus how it is measured."""

    title: str = ""
    suppliers: list[SipocItem] = Field(default_factory=list)
    inputs: list[SipocItem] = Field(default_factory=list)
    process: list[SipocItem] = Field(
        default_factory=list,
        description="5-7 high-level steps only — the detail belongs in the flow diagram",
    )
    outputs: list[SipocItem] = Field(default_factory=list)
    customers: list[SipocItem] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)


class FlowNode(BaseModel):
    id: str
    label: str
    kind: StepKind = "task"
    actor: str = ""
    systems: list[str] = Field(default_factory=list)
    detail: str = Field(
        default="",
        description="One line of the step's own description, drawn under the label. Filled "
        "in from the model in code rather than asked for — it is the same sentence, and a "
        "second copy in the response is a second chance to disagree with the model.",
    )


class FlowDiagram(BaseModel):
    title: str = ""
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[ProcessEdge] = Field(default_factory=list)


class SwimlaneStep(BaseModel):
    id: str
    label: str
    lane: str = Field(description="Must match one of SwimlaneDiagram.lanes")
    column: int = Field(
        default=0,
        description="Left-to-right stage. Steps sharing a column happen in parallel.",
    )
    kind: StepKind = "task"


class SwimlaneDiagram(BaseModel):
    title: str = ""
    lanes: list[str] = Field(default_factory=list)
    stages: list[str] = Field(
        default_factory=list,
        description="One phase name per column, left to right. 'Stage 3' tells the reader "
        "nothing; 'Validation' tells them what that column of boxes has in common.",
    )
    steps: list[SwimlaneStep] = Field(default_factory=list)
    edges: list[ProcessEdge] = Field(default_factory=list)


class DiagramRevision(BaseModel):
    """One entry in the follow-up history, so a user can see how the model got here."""

    version: int
    instruction: str = Field(default="", description="Empty for the first generation")
    summary: str = Field(default="", description="What the model reported changing")
    scope: DiagramScope = Field(
        default="all",
        description="What this revision was allowed to touch — all three views (a change to "
        "the process) or one view's layout. Defaults to `all` so revisions recorded before "
        "the choice existed read correctly: they all redrew everything.",
    )
    created_at: str | None = None
    viewable: bool = Field(
        default=False,
        description="A snapshot of this version is kept, so its three PNGs can still be "
        "drawn. Set on the API response, not stored — it is derived from the history.",
    )


class DiagramSnapshot(BaseModel):
    """A superseded version, kept so its diagrams can be looked at again.

    The *views* are kept, not the PNGs. Re-rendering from the stored views means an old
    version is drawn by the current renderer, so history does not freeze at whatever the
    drawing code looked like on the day — and it costs a few KB of JSON instead of megabytes
    of images per revision.
    """

    version: int
    model: ProcessModel
    sipoc: SipocDiagram
    flow: FlowDiagram
    swimlane: SwimlaneDiagram
    created_at: str | None = None


class DiagramSet(BaseModel):
    """Everything persisted for a project's blueprint step, as `projects.diagrams_json`."""

    version: int = 1
    frozen: bool = False
    frozen_at: str | None = None
    llm: bool = Field(
        default=True,
        description="False when the deterministic fallback drew these (no Bedrock)",
    )
    model: ProcessModel = Field(default_factory=ProcessModel)
    sipoc: SipocDiagram = Field(default_factory=SipocDiagram)
    flow: FlowDiagram = Field(default_factory=FlowDiagram)
    swimlane: SwimlaneDiagram = Field(default_factory=SwimlaneDiagram)
    warnings: list[str] = Field(
        default_factory=list,
        description="Reconciliation repairs — e.g. an edge pointing at a step that was "
        "dropped. Surfaced rather than swallowed so a thin extraction is visible.",
    )
    revisions: list[DiagramRevision] = Field(default_factory=list)
    history: list[DiagramSnapshot] = Field(
        default_factory=list,
        description="Superseded versions, oldest first, capped by HISTORY_LIMIT. Lets the "
        "change history show what each earlier version actually looked like.",
    )
    created_at: str | None = None
    updated_at: str | None = None

    def snapshot(self) -> DiagramSnapshot:
        """This version's views, for keeping once a follow-up supersedes it."""
        return DiagramSnapshot(
            version=self.version,
            model=self.model,
            sipoc=self.sipoc,
            flow=self.flow,
            swimlane=self.swimlane,
            created_at=self.updated_at or self.created_at,
        )

    def views_for(self, version: int) -> DiagramSnapshot | None:
        """The views for any version still on record — current or archived."""
        if version == self.version:
            return self.snapshot()
        return next((s for s in self.history if s.version == version), None)

    def viewable_versions(self) -> set[int]:
        return {self.version, *(s.version for s in self.history)}

    def as_generation_context(self) -> str:
        """Render the frozen model as prompt context for `deduce_plan`.

        Deliberately prose-with-structure rather than raw JSON: this is appended to a brief
        that is already prose, and the blueprint prompt reads it as requirements, not as a
        schema to echo back.
        """
        lines: list[str] = ["## Approved process blueprint (frozen by the user — authoritative)"]
        if self.model.title:
            lines.append(f"Process: {self.model.title}")
        if self.model.scope:
            lines.append(f"Scope: {self.model.scope}")
        if self.model.actors:
            lines.append("\n### Actors")
            for a in self.model.actors:
                suffix = f" — {a.responsibilities}" if a.responsibilities else ""
                lines.append(f"- {a.name} ({a.kind}){suffix}")
        if self.model.steps:
            lines.append("\n### Process steps")
            for s in self.model.steps:
                bits = [f"{s.id}. {s.name} [{s.kind}]"]
                if s.actor:
                    bits.append(f"actor: {s.actor}")
                if s.systems:
                    bits.append(f"systems: {', '.join(s.systems)}")
                if s.description:
                    bits.append(s.description)
                lines.append("- " + " · ".join(bits))
        if self.model.edges:
            lines.append("\n### Transitions")
            for e in self.model.edges:
                label = f" [{e.label}]" if e.label else ""
                lines.append(f"- {e.source} -> {e.target}{label}")
        if self.model.data_objects:
            lines.append(f"\n### Data objects\n{', '.join(self.model.data_objects)}")
        if self.model.kpis:
            lines.append("\n### Success measures")
            lines.extend(f"- {k}" for k in self.model.kpis)
        if self.model.assumptions:
            lines.append("\n### Assumptions the user accepted")
            lines.extend(f"- {a}" for a in self.model.assumptions)
        lines.append(
            "\nThe agents, skills and rules you produce must cover every step above. "
            "Each actor with substantial work should map to an agent or a named skill."
        )
        return "\n".join(lines)


class DiagramImage(BaseModel):
    """Size of a rendered PNG, for the UI and CLI to fetch or write out."""

    kind: str
    width: int = 0
    height: int = 0
    bytes: int = 0
    scale: int = Field(
        default=1,
        description="Device pixels per layout pixel. The viewer divides width/height by this "
        "to get the size the diagram was laid out to be read at, so 100% zoom is legible "
        "rather than twice the intended size.",
    )


class DiagramSetView(BaseModel):
    """API response — the set plus render metadata. PNG bytes come from their own endpoint."""

    project_id: str
    version: int
    frozen: bool
    frozen_at: str | None = None
    llm: bool = True
    model: ProcessModel
    sipoc: SipocDiagram
    flow: FlowDiagram
    swimlane: SwimlaneDiagram
    warnings: list[str] = Field(default_factory=list)
    revisions: list[DiagramRevision] = Field(default_factory=list)
    images: list[DiagramImage] = Field(default_factory=list)
    updated_at: str | None = None


class DiagramFollowupRequest(BaseModel):
    instruction: str = Field(
        description="Plain-language change, e.g. 'split approval into maker and checker'"
    )
    scope: DiagramScope = Field(
        default="all",
        description="`all` revises the process and redraws all three views. A single view "
        "(`sipoc`, `flow`, `swimlane`) re-lays out just that picture and leaves the process "
        "model — and therefore the other two views and the generated agents — untouched.",
    )
