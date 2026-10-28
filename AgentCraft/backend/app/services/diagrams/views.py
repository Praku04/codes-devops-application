"""The three blueprint views: what each is called, what it is for, and how it is filed.

One place for the label, the exported filename stem and the plain-language purpose, because
the same three sentences appear on the UI's Blueprint step, in the CLI's legend and in the
exported workspace README — and three hand-kept copies of a sentence drift apart.

(The CLI ships as its own package and cannot import this module, so it keeps a copy in
`cli/agentcraft/main.py` — `DIAGRAM_PURPOSE` for the sentences and `_diagram_filename` for the
names. The UI's copies are `blueprintTabs` and `diagramFileRef` in `wizard.component.ts`. All
three must be changed together.)
"""

from __future__ import annotations

from typing import NamedTuple


class DiagramView(NamedTuple):
    kind: str
    label: str
    #: Filename without the version or extension — `sipoc` → `sipoc-v2.png`.
    stem: str
    #: What question the view answers, in the words shown to the end user.
    purpose: str


VIEWS: tuple[DiagramView, ...] = (
    DiagramView(
        "sipoc",
        "SIPOC",
        "sipoc",
        "scope on one page — who hands work in, what the process turns it into, who "
        "receives it, and how it is measured. Read this first.",
    ),
    DiagramView(
        "flow",
        "Process flow",
        "process-flow",
        "the order of work — what happens after what, where a decision splits the path, "
        "and where a rejection sends work back for rework.",
    ),
    DiagramView(
        "swimlane",
        "Swimlane",
        "swimlane",
        "who owns what — the same steps in each team or system lane, so every handoff "
        "between them, and every wait it causes, is visible.",
    ),
)

VIEW_BY_KIND: dict[str, DiagramView] = {v.kind: v for v in VIEWS}


def export_filename(kind: str, version: int) -> str:
    """`sipoc-v2.png`.

    The version is in the name because the export directory is written to more than once: a
    workspace exported again after a revision must drop `sipoc-v3.png` beside the
    `sipoc-v2.png` somebody already reviewed rather than replace it in place.
    """
    return f"{VIEW_BY_KIND[kind].stem}-v{int(version)}.png"
