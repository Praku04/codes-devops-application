"""
Render the SDD's three architectural views (§3.2) to PNG.

Why pictures and not Mermaid. SDD.md is read in GitHub, in an IDE preview, in the file pane of
this app, and in whatever a delivery head pastes it into. A fenced ```mermaid block is a
diagram in the first two of those and a wall of code in the rest, and the wall of code is what
the reader who most needs the diagram sees. A PNG is a picture everywhere, and it is the same
byte-for-byte file the exported zip ships — so the document, the workspace and the review deck
cannot show three different architectures.

Why deterministic geometry and not image generation: the same reason as `render.py`. An
architecture diagram carries its meaning entirely in its labels and in which box points at
which; a model that draws it produces something that looks like a diagram with unreadable text
and arrows joining the wrong components. The model decides the *content* — layers, components,
nodes, what talks to what — and this module does the geometry.

**One layout engine for all three views.** Every view is horizontal bands stacked top to
bottom, boxes flowing left to right inside each band, and arrows in the gaps between bands.
What changes per view is only what a band *means*:

| View        | A band is…            | A box is…                                          |
| ----------- | --------------------- | -------------------------------------------------- |
| Logical     | a layer               | a component — name, technology, what it does        |
| Development | a top-level directory | a package — path, file count, its key filenames     |
| Deployment  | a runtime node        | a process it hosts — name, runtime, how it is sized |

Every box carries up to three lines for that reason: a diagram of nine boxes each holding one
noun is a diagram a reviewer cannot review. The name says which part, the second line says
what it is built with, and the third says what it is responsible for.

That is deliberate: three views of one solution that are laid out three different ways cost
the reader a fresh orientation on each one, and three layout engines cost three times the
bugs. The rules that make the pictures legible — a downward arrow means "calls", a dashed
upward one means "depends back on", a box's colour names its owner — are then the same in all
three.

The drawing primitives are `render.py`'s: `_Canvas`, the text metrics and the palette are
reused rather than re-derived, which is why the architecture views and the process blueprint
look like they came out of the same tool. They are private names imported across one package.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, NamedTuple

from app.services.diagrams.render import (
    ARROW,
    ARROW_SOFT,
    HAIR,
    LOOP,
    MUTED,
    PANEL,
    _Canvas,
    _clip,
    _edge_caption,
    _header_height,
    _line_h,
    _measure,
    _wrap,
)

logger = logging.getLogger("agentcraft.diagrams")


class ArchitectureView(NamedTuple):
    kind: str
    label: str
    #: Filename stem inside `docs/architecture/` — `logical` → `logical-view.png`.
    stem: str
    #: The SDD sub-section this picture belongs to.
    section: str
    #: What the picture answers, in the words the document uses.
    purpose: str


#: The three views, in the order §3.2 presents them.
#:
#: Unversioned filenames, unlike the process blueprint's `sipoc-v2.png`. The blueprint is
#: reviewed and approved as a numbered revision, so an export after a change has to land
#: beside the version somebody signed off. The architecture views are a rendering of the
#: current SDD and nothing else — a second export replaces them, exactly as it replaces
#: SDD.md itself.
ARCH_VIEWS: tuple[ArchitectureView, ...] = (
    ArchitectureView(
        "logical",
        "Logical view",
        "logical-view",
        "3.2.1",
        "the parts of the solution grouped into layers, and which layer is allowed to call "
        "which. Read this first.",
    ),
    ArchitectureView(
        "development",
        "Development view",
        "development-view",
        "3.2.2",
        "the code a developer opens — which package owns which concern, and the direction "
        "imports are allowed to run.",
    ),
    ArchitectureView(
        "deployment",
        "Deployment view",
        "deployment-view",
        "3.2.3",
        "what runs where at runtime, with the protocol, port and payload on every hop. "
        "The view an on-call engineer reads first.",
    ),
)

ARCH_KINDS: tuple[str, ...] = tuple(v.kind for v in ARCH_VIEWS)
ARCH_VIEW_BY_KIND: dict[str, ArchitectureView] = {v.kind: v for v in ARCH_VIEWS}

#: Folder the pictures occupy in the exported workspace. Separate from `docs/diagrams/`, which
#: holds the process blueprint: one folder is "the process we are automating", the other is
#: "the software we are building", and a reader opening either should not have to sort six
#: PNGs into two groups by filename.
ARCH_DOC_DIR = "docs/architecture"

#: Bumped whenever this module's output changes. Part of the cached filename, so improving a
#: drawing re-renders existing projects instead of serving last month's picture.
#: 2 — boxes gained a technology line and a "what it does" line, and the deployment arrows
#: gained the port.
#: 3 — an edge label may be two lines, so it is clipped and measured per line instead of being
#: truncated as one, and a labelled same-band connector no longer lands on the box beside its
#: source or on the legend.
#: 4 — every box may carry a fourth line of concrete evidence (a component's contract, a
#: package's key filenames, a host's state), band subtitles say what the band is for rather
#: than only how many boxes it holds, and the arrow legend is worded per view instead of
#: calling an import a "call".
#: 5 — nothing is cut mid-word any more. The view description was capped at 400 *characters*
#: before it ever reached the renderer, so the paragraph explaining the layering rule ended
#: "…is a cross-cutting concern invoke" with no ellipsis to say so; it is now bounded on a word
#: boundary at a budget the header can actually draw. Box notes get three lines, facts wrap
#: instead of being clipped to one, and a band subtitle may take two lines.
#: 6 — three formatting faults the eyeball pass found at revision 5. A contract wider than its
#: box (`GET /projects/{id}/plan/architecture/{kind}.png`) was drawn outside it, now broken at a
#: seam by `render.py`'s wrapper; a band heading crossed by a connector is drawn on its own
#: plate; and the stack inside a box is top-aligned, so the names across a band line up in a row
#: instead of each floating in the middle of however many lines that box happens to carry.
#: 7 — text was still being cut mid-word, but *before* the renderer, so nothing marked it: `_s`
#: sliced every payload cell at a character count, which is how a box came to read "…and the error
#: shape. Deleg" and "opens no databa". `_s` now bounds on a word boundary with an ellipsis like
#: `_prose` does, the two responsibility budgets that were doing the visible damage are raised so
#: the *layout* decides how much fits, a box note may take four lines rather than three, and a
#: backtick in a payload cell is dropped instead of drawn as a stray character.
#: 8 — the shared header now wraps a long title to a second line instead of clipping it, and
#: these three titles are `<project name> — <view>`, which is the longest title the app draws.
#: The bump is here as well as in `render.py` because `_header_height` is what decides where
#: these bands start, so a two-line title moves every box on the picture.
#: 9 — the header scope reads to the end of its last sentence. Revision 5 raised the description
#: budget inside this module but left the 400-character slice in `architecture_views_payload`,
#: which is upstream of everything the renderer can see: the drawn Development View ended
#: "Environment variables are the only coupling between th" and the audit reported zero cuts,
#: because the cut had already happened before it was watching. The payload now stores the
#: paragraph whole, a description an older build stored already cut is recovered from §3.2 of the
#: same plan's SDD, and `_prose` marks one that cannot be recovered. Measured against a real
#: project rather than the short demo, every other budget that was still cutting is raised too:
#: an edge caption may take three lines and is allocated across them so the first row is not
#: starved, a box's tech line wraps instead of being clipped, notes and facts get more lines, and
#: a deployment node's scaling note keeps the clause naming the failover domain. Zero cuts on all
#: three views for the project that reported the bug.
ARCH_REVISION = 9

#: Arrow wording per view: primary, level-skipping, and backwards. The three views draw the
#: same three strokes but they do not mean the same thing — an arrow in the Development View is
#: an `import` statement, not a call at runtime — and one shared label made the picture read as
#: if it did.
_LEGEND_WORDS: dict[str, tuple[str, str, str]] = {
    "logical": ("calls / data flow", "skips a layer", "calls back up"),
    "development": ("imports", "skips a level", "circular import"),
    "deployment": ("network hop", "skips a tier", "replies upstream"),
}


def arch_export_filename(kind: str) -> str:
    """`logical-view.png`."""
    return f"{ARCH_VIEW_BY_KIND[kind].stem}.png"


def arch_workspace_path(kind: str) -> str:
    """`docs/architecture/logical-view.png` — the path SDD.md links to and the zip writes."""
    return f"{ARCH_DOC_DIR}/{arch_export_filename(kind)}"


#: Workspace path → kind, for the exporter and the file tree.
ARCH_WORKSPACE_PATHS: dict[str, str] = {arch_workspace_path(k): k for k in ARCH_KINDS}


# ---------------------------------------------------------------------------
# Layout model
# ---------------------------------------------------------------------------


@dataclass
class ArchBox:
    """One drawn box. `key` is what an edge refers to; `label` is what a reader sees."""

    key: str
    label: str
    #: Small second line — the technology, a file count, a port, a replica count.
    sub: str = ""
    #: Third line, smaller and lighter — what the thing actually does. A box that says only
    #: `Query Engine` is a box a reviewer has to ask about; one that says `Query Engine /
    #: LangChain retriever / ranks chunks, cites source` is one they can check.
    note: str = ""
    #: Concrete evidence, one short line each, drawn last and in the band's own colour.
    #:
    #: The difference from `note` is what the line is for: `note` is prose about the thing,
    #: a fact is something checkable — the filenames in a package, the endpoint a component
    #: exposes, the state a process holds. The Development View was the thinnest picture of the
    #: three precisely because it had one slot and had to choose between "what this package is
    #: for" and "which files are in it"; with both, a box names the package, counts its files,
    #: says what it owns *and* lists what to open.
    facts: list[str] = field(default_factory=list)
    #: Index into `BAND_TINTS`. Defaults to the band's own tint.
    tint: int | None = None


@dataclass
class ArchGroup:
    """One horizontal band."""

    key: str
    title: str
    subtitle: str = ""
    boxes: list[ArchBox] = field(default_factory=list)


@dataclass
class ArchEdge:
    src: str
    dst: str
    label: str = ""


@dataclass
class _Slot:
    """Where one drawn thing ended up, and the empty channels a connector may use to reach it.

    `exit_y` is clear space below it and `entry_y` clear space above — the inter-row gap when
    there is another row that way, the band gap when there is not. Deciding this while the row
    structure is in hand is what keeps the edge routing free of geometry special cases.
    """

    x: float
    y: float
    w: float
    h: float
    band: int
    exit_y: float
    entry_y: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def bottom(self) -> float:
        return self.y + self.h


# ---------------------------------------------------------------------------
# Geometry constants — layout pixels at render.py's DPI
# ---------------------------------------------------------------------------

PAD = 26.0
BAND_PAD = 15.0
#: Height of the band's own title strip, above its first row of boxes — the floor, for a
#: one-line subtitle. `_draw` raises it by a line when any band's subtitle needs two.
BAND_TITLE_H = 34.0
BAND_GAP = 54.0
BOX_GAP = 14.0
ROW_GAP = 14.0
BOX_MIN_W = 132.0
BOX_MAX_W = 236.0
BOX_MIN_H = 48.0
#: Space above the first line in a box. Every box shares one height and its stack is drawn from
#: this offset, so the names across a band sit on one baseline; the tallest box in the picture is
#: the one that pays exactly this much at the top and the same again at the bottom.
BOX_PAD_TOP = 9.0
CONTENT_MIN = 760.0
CONTENT_MAX = 1160.0
#: Width reserved outside the bands for arrows that skip a band or point back up one.
CORRIDOR_W = 54.0
LEGEND_H = 34.0

LABEL_SIZE = 9.5
SUB_SIZE = 7.6
NOTE_SIZE = 6.9
TITLE_SIZE = 10.5
BAND_SUB_SIZE = 8.0
#: Per *line* of an edge label, not per label: a deployment hop is drawn as `HTTPS · 443` over
#: `Signed-in user requests`, and clipping the pair against one budget produced the truncated
#: `HTTPS · 443 · Signed-in…` that made the picture read as high level.
EDGE_LABEL_PX = 190.0
#: Lines an edge label may take. The budget above is per line, but nothing was *wrapping* into
#: it: a caption arrived as one line and was clipped there, so `POST /query (JSON: {question,
#: session_token})` was drawn as `POST /query (JSON: {question…` and every call signature on the
#: Logical View lost its arguments. The plate is measured and placed per line already
#: (`render.py::_label_half` and `_label_point`), so a second line costs nothing but its own
#: height and cannot land on a box. Three rather than two because a deployment hop supplies two
#: rows of its own: at two, one of them had to fit on a single line, and `Parameterised queries
#: and writes` — thirty-two characters, one and a half characters over the budget — was drawn as
#: `Parameterised queries and…`.
EDGE_LABEL_MAX_LINES = 3
#: Lines a box's technology line may take. One, until `Next.js 14 App Router, React, TypeScript`
#: came back as `Next.js 14 App Router, React, TypeS…` — `_box_width` asks for the whole of this
#: line, so the only way it gets clipped is a box already at `BOX_MAX_W`, and then the fix is a
#: second line rather than a shorter stack.
SUB_MAX_LINES = 2
#: How many lines of `ArchBox.note` a box may carry. Three. Two was chosen to keep the boxes
#: square, and it cost the end of the sentence on every box whose responsibility named both what
#: it owns and what it is not allowed to do — which is exactly the phrasing the prompt asks for,
#: so the useful half was the half being cut. One box height is used for the whole picture, so
#: the price is a taller page; a taller page is legible and a truncated responsibility is not.
#: Four, since revision 7: three lines hold roughly 130 characters at this size, and the
#: responsibilities the prompt asks for run to about 160 — so three lines meant the *last* clause
#: of nearly every box was the one being dropped, and the last clause is where the constraint is.
#: Six, since revision 9: measured against a real project rather than estimated, a package
#: responsibility written the way the prompt asks ("owns X; must never Y") runs to 170–195
#: characters, which is five lines in a 220-pixel box — so four lines cut six of the eight boxes
#: on the Development View, every one of them in the "must never" clause.
NOTE_MAX_LINES = 6
#: How many *lines* of `ArchBox.facts` a box may carry, on top of the note. Lines, not facts:
#: the facts are wrapped into this budget rather than one-clipped-line-each, so a single long
#: contract ("POST /claims · GET /claims/{id} · GET /claims/{id}/events") takes two lines and is
#: read in full instead of arriving as "POST /claims · GET /claims/{id} ·…".
#: Five, since revision 9: a box carries up to two facts and one of them is a route list, so at
#: three the first fact spent a line and the list was left with two — `POST /documents, GET
#: /documents, DELETE…`, which is the failure this budget was widened to prevent in the first
#: place, one fact further along.
FACT_MAX_LINES = 5
#: Fact lines are drawn at the note size but in the band's own colour, so "what this is for"
#: and "here is the checkable detail" are told apart without a fifth type size.
FACT_SIZE = 6.9

#: Bounds. The content is model-driven, and a runaway extraction has to produce a coarse
#: picture rather than a 40-band canvas nothing can read.
MAX_BANDS = 9
MAX_BOXES_PER_BAND = 12
MAX_EDGES = 32

#: Fill / border per band, cycled. Position says "which layer", colour says "which owner" —
#: which is what lets the development view group by directory while still ordering by
#: dependency depth.
BAND_TINTS: list[tuple[str, str]] = [
    ("#eef1fe", "#4a57cf"),
    ("#e6f6f1", "#1f8f72"),
    ("#fdf1e4", "#b9762a"),
    ("#f1eaff", "#7245e0"),
    ("#fbe9ef", "#b93a63"),
    ("#e8f4fb", "#2f7fae"),
    ("#f3f6e9", "#5f8a1f"),
    ("#fdeee9", "#c2542c"),
    ("#eaf0f6", "#4a5a76"),
]

#: Accent bar in the header, per view — the same trick `render.py` uses so three PNGs in one
#: folder are told apart before any text on them is legible.
ARCH_ACCENT = {"logical": "#4a57cf", "development": "#1f8f72", "deployment": "#b9762a"}


def _tint(index: int) -> tuple[str, str]:
    return BAND_TINTS[index % len(BAND_TINTS)]


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------


def _arch_caption(text: str) -> str:
    """`render.py::_edge_caption` at this module's own budget.

    An architecture arrow travels in a wider corridor than a flowchart's, so the width and the
    line count are this module's; the rule itself — wrap into the plate, never clip a caption at
    one line, keep a payload's own rows as rows — is shared, because it is the same defect in
    both pictures when it goes wrong. It went wrong here as `answer_question(question, use…`.
    """
    return _edge_caption(
        text, width=EDGE_LABEL_PX, size=SUB_SIZE, max_lines=EDGE_LABEL_MAX_LINES
    )


def _box_width(box: ArchBox) -> float:
    need = (
        max(
            _measure(box.label, LABEL_SIZE, "bold"),
            _measure(box.sub, SUB_SIZE),
            # The note is allowed to wrap onto two lines, so it only has to fit half of itself
            # in the width — otherwise one long "what it does" phrase would set every box in
            # the picture to BOX_MAX_W and the bands would each hold two boxes.
            _measure(box.note, NOTE_SIZE) * 0.55,
            # Facts are clipped, not wrapped, so asking for three quarters of the widest one
            # is what keeps `routes.py · deps.py · schemas.py` legible without letting a
            # package that happens to hold long filenames set the width of the whole page.
            max((_measure(f, FACT_SIZE) for f in box.facts), default=0.0) * 0.75,
        )
        + 26.0
    )
    return max(BOX_MIN_W, min(BOX_MAX_W, need))


def _pack(widths: list[float], content_w: float) -> list[list[int]]:
    """Greedily flow box indices into rows no wider than the content column."""
    rows: list[list[int]] = [[]]
    used = 0.0
    for i, w in enumerate(widths):
        need = w if not rows[-1] else w + BOX_GAP
        if rows[-1] and used + need > content_w:
            rows.append([])
            used = w
        else:
            used += need
        rows[-1].append(i)
    return [r for r in rows if r]


def _draw(
    groups: list[ArchGroup],
    edges: list[ArchEdge],
    *,
    title: str,
    subtitle: str,
    kind: str,
) -> tuple[bytes, int, int, int]:
    """Lay out and draw one view. Returns `render.py`'s `(png, width, height, scale)`."""
    groups = [g for g in groups if g.boxes][:MAX_BANDS]
    if not groups:
        groups = [ArchGroup(key="solution", title="Solution", boxes=[ArchBox("app", "Application")])]
    for g in groups:
        g.boxes = g.boxes[:MAX_BOXES_PER_BAND]

    widths: dict[int, list[float]] = {
        gi: [_box_width(b) for b in g.boxes] for gi, g in enumerate(groups)
    }
    natural = max(sum(ws) + BOX_GAP * (len(ws) - 1) for ws in widths.values())
    content_w = max(CONTENT_MIN, min(CONTENT_MAX, natural))
    rows_by_group = {gi: _pack(widths[gi], content_w) for gi in widths}

    # One box height for the whole picture: rows that line up read as a grid, rows of three
    # different heights read as an accident.
    label_lines: dict[tuple[int, int], list[str]] = {}
    sub_lines: dict[tuple[int, int], list[str]] = {}
    note_lines: dict[tuple[int, int], list[str]] = {}
    fact_lines: dict[tuple[int, int], list[str]] = {}
    box_h = BOX_MIN_H
    for gi, g in enumerate(groups):
        for bi, box in enumerate(g.boxes):
            lines = _wrap(box.label, widths[gi][bi] - 20.0, LABEL_SIZE, "bold", max_lines=2)
            label_lines[(gi, bi)] = lines or [box.label]
            sub_lines[(gi, bi)] = (
                _wrap(box.sub, widths[gi][bi] - 16.0, SUB_SIZE, max_lines=SUB_MAX_LINES)
                if box.sub
                else []
            )
            notes = (
                _wrap(box.note, widths[gi][bi] - 16.0, NOTE_SIZE, max_lines=NOTE_MAX_LINES)
                if box.note
                else []
            )
            note_lines[(gi, bi)] = notes
            # Wrapped into a shared line budget, not clipped one line per fact. Clipping meant a
            # fact longer than the box was silently halved, and it also wasted the budget: a box
            # carrying one long contract used one line and dropped the rest of it, while the
            # second line it had already paid height for stayed empty.
            facts: list[str] = []
            for f in box.facts:
                if not f or len(facts) >= FACT_MAX_LINES:
                    continue
                facts.extend(
                    _wrap(f, widths[gi][bi] - 14.0, FACT_SIZE, max_lines=FACT_MAX_LINES - len(facts))
                )
            fact_lines[(gi, bi)] = facts[:FACT_MAX_LINES]
            need = (
                BOX_PAD_TOP * 2
                + len(label_lines[(gi, bi)]) * _line_h(LABEL_SIZE)
                + len(sub_lines[(gi, bi)]) * _line_h(SUB_SIZE)
                + len(notes) * _line_h(NOTE_SIZE)
                + len(fact_lines[(gi, bi)]) * _line_h(FACT_SIZE)
            )
            box_h = max(box_h, need)

    # Band titles and their subtitles, wrapped here rather than at drawing time, because the
    # title strip has to be tall enough for whatever they take before any band's height is
    # known. One strip height for every band, same reason the boxes share one height.
    band_titles: list[str] = []
    band_subs: list[list[str]] = []
    for g in groups:
        title_txt = _clip(g.title, content_w * 0.45, TITLE_SIZE, "bold")
        band_titles.append(title_txt)
        room = max(
            content_w - _measure(title_txt, TITLE_SIZE, "bold") - BAND_PAD * 3, content_w * 0.3
        )
        band_subs.append(_wrap(g.subtitle, room, BAND_SUB_SIZE, max_lines=2) if g.subtitle else [])
    band_title_h = BAND_TITLE_H + (
        max((len(rows) for rows in band_subs), default=1) - 1
    ) * _line_h(BAND_SUB_SIZE)

    band_heights = [
        band_title_h
        + len(rows_by_group[gi]) * box_h
        + (len(rows_by_group[gi]) - 1) * ROW_GAP
        + BAND_PAD
        for gi in range(len(groups))
    ]

    band_of: dict[str, int] = {}
    for gi, g in enumerate(groups):
        band_of.setdefault(g.key.lower(), gi)
        for box in g.boxes:
            band_of.setdefault(box.key.lower(), gi)

    def band_index(ref: str) -> int | None:
        return band_of.get((ref or "").lower())

    resolved: list[tuple[ArchEdge, int, int]] = []
    for e in edges:
        a, b = band_index(e.src), band_index(e.dst)
        if a is None or b is None or (a == b and e.src.lower() == e.dst.lower()):
            continue
        resolved.append((e, a, b))
        if len(resolved) >= MAX_EDGES:
            break

    needs_right = any(b > a + 1 or (a == b) for _, a, b in resolved)
    needs_left = any(b < a for _, a, b in resolved)
    left_c = CORRIDOR_W if needs_left else 0.0
    right_c = CORRIDOR_W if needs_right else 0.0

    band_x = PAD + left_c
    band_w = content_w + 2 * BAND_PAD
    canvas_w = band_x + band_w + right_c + PAD

    # A connector between two boxes in the same band is routed underneath it. Under the *last*
    # band that channel is the strip the legend sits in, so a two-line hop label — a database
    # replicating to its backups, say — was drawn across the legend. When such an edge exists
    # the picture grows to give it a lane of its own.
    # Only for a *labelled* one: an unlabelled connector has nothing to collide with, and the
    # Development View's import chain is entirely unlabelled same-band edges, which would
    # otherwise leave an empty strip under the last band on every picture.
    tail_lane = (
        _line_h(SUB_SIZE) * 2 + 16.0
        if any(a == b == len(groups) - 1 and e.label for e, a, b in resolved)
        else 0.0
    )

    header_h = _header_height(
        subtitle, canvas_w, title=title, badge=ARCH_VIEW_BY_KIND[kind].label
    )
    body_h = sum(band_heights) + BAND_GAP * (len(groups) - 1)
    canvas_h = header_h + body_h + 18.0 + tail_lane + LEGEND_H + PAD

    canvas = _Canvas(canvas_w, canvas_h)
    canvas.header(
        title,
        subtitle,
        badge=ARCH_VIEW_BY_KIND[kind].label,
        accent=ARCH_ACCENT.get(kind, MUTED),
    )

    # -- bands and boxes --
    pos: dict[str, _Slot] = {}
    y = header_h
    for gi, g in enumerate(groups):
        band_h = band_heights[gi]
        # The band itself stays PANEL — only the bar below and the boxes inside take the tint,
        # so `_fill` is named and unused rather than read as a mistake at the next line.
        _fill, border = _tint(gi)
        canvas.rect(band_x, y, band_w, band_h, fill=PANEL, edge=HAIR, lw=1.0, radius=6, z=1)
        # A short bar in the band's own colour: the boxes inside carry the same tint, so the
        # band and its contents are visibly one group even where an arrow crosses the gap.
        canvas.rect(band_x, y, 4.0, band_h, fill=border, radius=0, z=2)
        # On a plate in the band's own colour: a connector entering a box from the band above has
        # to cross this strip to reach it, and an arrow through the letters of "3. Data" is the
        # kind of collision that makes a picture look broken rather than dense.
        canvas.text(
            band_x + BAND_PAD,
            y + 18.0,
            band_titles[gi],
            size=TITLE_SIZE,
            weight="bold",
            ha="left",
            halo=True,
            halo_color=PANEL,
        )
        # Measured against the title actually drawn rather than given a fixed share of the band,
        # and allowed a second line — see where `band_subs` is built. A band title is a name
        # ("Data", "app", "1. Presentation") so a fixed split left two thirds of the row empty
        # and still put an "…" in the middle of the subtitle, which is the line carrying the
        # band's whole reason for existing.
        sub_y = y + 18.0
        for row in band_subs[gi]:
            canvas.text(
                band_x + band_w - BAND_PAD,
                sub_y,
                row,
                size=BAND_SUB_SIZE,
                color=MUTED,
                ha="right",
                halo=True,
                halo_color=PANEL,
            )
            sub_y += _line_h(BAND_SUB_SIZE)
        # The title strip is claimed, not merely haloed. A halo only decides which of two
        # overlapping strings is on top; it does not stop an edge label being placed there, and
        # "get_query_stats(period) -> QueryStats" was drawn straight across "Own all business
        # logic and transaction boundaries" — the one line saying what the band is for. Claiming
        # it sends the label somewhere else instead.
        title_h = _line_h(TITLE_SIZE)
        canvas.obstacles.append(
            (
                band_x + BAND_PAD - 2.0,
                y + 18.0 - title_h / 2,
                _measure(band_titles[gi], TITLE_SIZE, "bold") + 4.0,
                title_h,
            )
        )
        if band_subs[gi]:
            sub_w = max(_measure(row, BAND_SUB_SIZE) for row in band_subs[gi])
            canvas.obstacles.append(
                (
                    band_x + band_w - BAND_PAD - sub_w - 2.0,
                    y + 18.0 - _line_h(BAND_SUB_SIZE) / 2,
                    sub_w + 4.0,
                    len(band_subs[gi]) * _line_h(BAND_SUB_SIZE),
                )
            )
        band_rows = rows_by_group[gi]
        # A band is addressable as a whole, so a deployment connection may name the node
        # rather than one of the processes inside it. Its channels are the band gaps.
        pos.setdefault(
            g.key.lower(),
            _Slot(
                band_x,
                y,
                band_w,
                band_h,
                gi,
                exit_y=y + band_h + BAND_GAP * 0.35,
                entry_y=y - BAND_GAP * 0.35,
            ),
        )

        row_y = y + band_title_h
        for ri, row in enumerate(band_rows):
            row_w = sum(widths[gi][i] for i in row) + BOX_GAP * (len(row) - 1)
            x = band_x + BAND_PAD + max(0.0, (content_w - row_w) / 2)
            for i in row:
                box = g.boxes[i]
                bw = widths[gi][i]
                b_fill, b_edge = _tint(gi if box.tint is None else box.tint)
                canvas.rect(x, row_y, bw, box_h, fill=b_fill, edge=b_edge, lw=1.2, radius=4, z=3)
                # Name, then technology, then what it does — stacked as one centred column.
                # Pinning the name to the middle and the detail to the bottom edge (which is
                # what a two-line box could get away with) has the three overlapping as soon as
                # the third line exists.
                lines = label_lines[(gi, i)]
                subs = sub_lines[(gi, i)]
                notes = note_lines[(gi, i)]
                facts = fact_lines[(gi, i)]
                label_h = len(lines) * _line_h(LABEL_SIZE)
                sub_h = len(subs) * _line_h(SUB_SIZE)
                note_h = len(notes) * _line_h(NOTE_SIZE)
                fact_h = len(facts) * _line_h(FACT_SIZE)
                # Top-aligned, not centred. Every box in the picture shares one height, so a box
                # carrying three lines next to one carrying five had its name sitting a line lower
                # than its neighbour's; a row of names on one baseline is what makes a band read
                # as a row of peers. `BOX_PAD_TOP` is the padding the tallest box already has.
                top = row_y + BOX_PAD_TOP
                canvas.lines(
                    x + bw / 2, top + label_h / 2, lines, size=LABEL_SIZE, weight="bold"
                )
                if subs:
                    canvas.lines(
                        x + bw / 2,
                        top + label_h + sub_h / 2,
                        subs,
                        size=SUB_SIZE,
                        color=MUTED,
                    )
                if notes:
                    canvas.lines(
                        x + bw / 2,
                        top + label_h + sub_h + note_h / 2,
                        notes,
                        size=NOTE_SIZE,
                        color=MUTED,
                    )
                if facts:
                    # In the band's border colour, not MUTED: it is the last and smallest line
                    # in the box, and at this size grey on a tinted fill stops being a line a
                    # reader's eye lands on at all.
                    canvas.lines(
                        x + bw / 2,
                        top + label_h + sub_h + note_h + fact_h / 2,
                        facts,
                        size=FACT_SIZE,
                        color=b_edge,
                    )
                # The two channels a connector may travel in without crossing a box: the
                # inter-row gap when there is a row that way, and the band gap when there is
                # not. Recorded here, where the row structure is known, so edge routing below
                # never has to reason about it.
                pos[box.key.lower()] = _Slot(
                    x,
                    row_y,
                    bw,
                    box_h,
                    gi,
                    exit_y=(
                        row_y + box_h + ROW_GAP / 2
                        if ri < len(band_rows) - 1
                        # `tail_lane` is zero for every band but the last, and non-zero there
                        # only when something actually routes under it.
                        else y
                        + band_h
                        + BAND_GAP * 0.35
                        + (tail_lane / 2 if gi == len(groups) - 1 else 0.0)
                    ),
                    entry_y=(row_y - ROW_GAP / 2 if ri > 0 else y - BAND_GAP * 0.35),
                )
                canvas.obstacles.append((x, row_y, bw, box_h))
                x += bw + BOX_GAP
            row_y += box_h + ROW_GAP
        y += band_h + BAND_GAP

    # -- edges --
    #
    # Every connector that is not a single step down the stack is routed through empty space
    # rather than drawn as a straight line between two anchors. The reason is not neatness: a
    # line from the application band to the third band below it, drawn into the *side* of its
    # target, passes through whatever boxes sit between them and its arrowhead then appears to
    # belong to the box it happens to touch last. Two of these read as an entirely different
    # architecture than the one the model described. So a long connector leaves through the
    # channel below its source, travels in a corridor beside the bands, and arrives in the
    # channel above its target — three runs, none of which crosses a box.
    right_lane = 0
    left_lane = 0
    for e, a, b in resolved:
        src = pos.get(e.src.lower())
        dst = pos.get(e.dst.lower())
        if not src or not dst:
            continue
        label = _arch_caption(e.label)
        if b == a + 1:
            # One step down: a direct arrow, which is the shortest and clearest line there is.
            start = (src.cx, src.bottom)
            end = (dst.cx, dst.y)
            canvas.arrow(start, end, label=label, rad=0.0 if abs(src.cx - dst.cx) < 8 else 0.06)
        elif b > a:
            lane = band_x + band_w + 12.0 + (right_lane % 4) * 11.0
            right_lane += 1
            canvas.route(
                [
                    (src.cx, src.bottom),
                    (src.cx, src.exit_y),
                    (lane, src.exit_y),
                    (lane, dst.entry_y),
                    (dst.cx, dst.entry_y),
                    (dst.cx, dst.y),
                ],
                label=label,
                color=ARROW_SOFT,
            )
        elif b < a:
            # Points back up: out of the top, round the left, in through the bottom. Drawn
            # dashed and in its own colour because an upward dependency is the thing a
            # reviewer is looking for in a layered design.
            lane = band_x - 12.0 - (left_lane % 4) * 11.0
            left_lane += 1
            canvas.route(
                [
                    (src.cx, src.y),
                    (src.cx, src.entry_y),
                    (lane, src.entry_y),
                    (lane, dst.exit_y),
                    (dst.cx, dst.exit_y),
                    (dst.cx, dst.bottom),
                ],
                label=label,
                color=LOOP,
                dashed=True,
            )
        elif (
            src.x + src.w + 6 <= dst.x
            and abs(src.y - dst.y) < 2
            # …and the gap is wide enough to hold the label. Two touching boxes are only a few
            # pixels apart, and a label centred on that stub is drawn straight across both of
            # them: the arrow gets shorter, the boxes get unreadable, and the reader sees a
            # caption sitting on a box it does not belong to. Those go under the band instead.
            and (
                not label
                or dst.x - (src.x + src.w)
                >= max(_measure(row, SUB_SIZE) for row in label.split("\n")) + 8.0
            )
        ):
            # Same band, same row, target to the right, room for the caption: the one case a
            # straight horizontal arrow says exactly what it means.
            canvas.arrow((src.x + src.w, src.cy), (dst.x, dst.cy), label=label)
        else:
            # Same band, but wrapped onto another row or pointing back left. Under the band
            # and up into the target, for the same reason as above.
            channel = max(src.exit_y, dst.exit_y)
            canvas.route(
                [
                    (src.cx, src.bottom),
                    (src.cx, channel),
                    (dst.cx, channel),
                    (dst.cx, dst.bottom),
                ],
                label=label,
                color=ARROW_SOFT,
            )

    primary, forward, backward = _LEGEND_WORDS.get(kind, _LEGEND_WORDS["logical"])
    strokes: list[tuple[str, bool, str]] = [(ARROW, False, primary)]
    if needs_right:
        strokes.append((ARROW_SOFT, False, forward))
    if needs_left:
        strokes.append((LOOP, True, backward))
    canvas.legend(band_x, canvas_h - PAD - LEGEND_H, band_w, strokes=strokes)
    return canvas.to_png()


# ---------------------------------------------------------------------------
# Views → layout
#
# The input is the normalised `architecture_views` payload built by
# `services/deduction/solution_design.py` and stored on the plan. Its shape is the contract
# between that module and this one:
#
#   {"logical":     {"description",
#                    "layers":[{"name","responsibility",
#                               "components":[{"name","tech","detail"}]}],
#                    "flows":[{"from","to","label"}]},
#    "development": {"description", "groups":[{"path","files":int,"top","responsibility",
#                                             "key_files":[str],"depends_on":[str]}]},
#    "deployment":  {"description",
#                    "nodes":[{"name","runtime","scaling",
#                              "hosts":[{"name","tech","detail"}]}],
#                    "connections":[{"from","to","protocol","port","data"}]}}
#
# `components` and `hosts` also accept a plain list of strings, which is what projects whose
# payload was stored before the per-box detail lines existed still hold.
#
# Everything here tolerates a missing or wrong-typed key: an architecture picture that fails
# to draw would block an export, and a coarse picture is worth more than none.
# ---------------------------------------------------------------------------


def _s(value: Any, limit: int = 160) -> str:
    """A label or a short phrase, bounded the same way a paragraph is — see `_prose`.

    This used to be a hard `[:limit]` slice, and every cell that reached a box through it could
    lose its tail mid-word with nothing to say so: a package responsibility ending "…and the
    error shape. Deleg" and a host note ending "opens no databa" are both this function, not the
    renderer, which marks every cut it makes. A label is normally far shorter than its budget, so
    the bound is a safety net rather than a layout decision — but when it does fire, a visible `…`
    is the difference between a shortened sentence and a sentence the designer never finished.
    """
    return _prose(value, limit)


def _prose(value: Any, limit: int = 1100) -> str:
    """A paragraph, bounded on a word boundary and marked when it is cut.

    `_s` is right for a label — a name that is either short or wrong — but wrong for a
    description. `_s(view["description"], 400)` is what produced the header reading "The Audit
    Writer is a cross-cutting concern invoke": a hard slice at 400 characters, mid-word, with no
    ellipsis to tell the reader anything was missing. From a picture, an unmarked cut is
    indistinguishable from a sentence the designer left unfinished.

    The budget is set against what the header can draw, not guessed: `HEADER_SUB_MAX_LINES`
    lines of `HEADER_SUB_SIZE` across an architecture canvas is comfortably more than this, so
    in practice the renderer's own wrapping is what shortens a long description — and it marks
    the cut. This bound only stops a payload that put a whole section in `description` from
    setting the canvas height.
    """
    # Backticks are dropped, not drawn. A payload cell may arrive already carrying a code span
    # (`read once at startup; `.env` gitignored`), and a picture has no code formatting to spend
    # it on — the reader just gets two stray characters. The table beside the figure in SDD.md is
    # where that span belongs, and it keeps it.
    raw = str(value).replace("`", "") if isinstance(value, (str, int, float)) else ""
    text = " ".join(raw.split())
    if len(text) > limit:
        cut = text[:limit]
        head = cut.rsplit(" ", 1)[0] if " " in cut else cut
        return f"{head.rstrip(' ,;:.')}…"
    # A cut this function did not make. A payload stored before `architecture_views_payload`
    # stopped slicing descriptions at 400 characters holds a mid-word prefix, and the words that
    # follow are not in it to redraw — `solution_design.py::_restore_descriptions` recovers them
    # from the stored SDD where it can, and this is what the picture does when it cannot. The
    # test is deliberately narrow (the same one as `_looks_cut`, kept local because
    # `solution_design` imports this module, not the other way round): long, and ending on
    # nothing that reads as an ending. A dangling part-word goes with it, so the mark lands
    # after a whole word.
    if len(text) > 200 and text[-1] not in ".!?…:;)\"'":
        head = text.rsplit(" ", 1)[0] if " " in text else text
        return f"{head.rstrip(' ,;:.')}…"
    return text


def _slist(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [_s(v) for v in value]
    return [v for v in out if v][:limit]


def _dlist(value: Any, limit: int = 24) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)][:limit]


def _view(views: dict[str, Any], key: str) -> dict[str, Any]:
    got = (views or {}).get(key)
    return got if isinstance(got, dict) else {}


def _parts(value: Any, limit: int = MAX_BOXES_PER_BAND) -> list[dict[str, str]]:
    """A layer's components / a node's processes, as `{name, tech, detail, interface}`.

    Accepts both shapes on purpose. A component used to be a bare string, and projects whose
    `architecture_views` were stored before the detail lines existed still hold lists of
    strings — they must keep drawing, just with fewer lines per box.

    `interface` is the checkable one: the endpoint, table, topic or state the thing owns. It
    is what turns "Batch Progress Service — reports batch completion" into a box a reviewer can
    disagree with, and it is optional everywhere, so a payload written before it existed loses
    nothing.
    """
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            name, tech, detail, interface = _s(item), "", "", ""
        elif isinstance(item, dict):
            name = _s(item.get("name") or item.get("component") or item.get("label"))
            tech = _s(item.get("tech") or item.get("technology") or item.get("runtime"), 44)
            # 200, not 110: at 110 a responsibility phrased as "what I own and what I must not
            # do" — the phrasing the prompt asks for — lost its second half before the renderer
            # ever saw it. The layout decides how much fits (`NOTE_MAX_LINES`) and says so with
            # an ellipsis; this bound is only here to stop a payload that put a whole section in
            # one cell from setting the canvas height.
            detail = _s(
                item.get("detail") or item.get("responsibility") or item.get("purpose"), 280
            )
            # 160, not 70: an interface is a *list* — every route a router owns, every function a
            # service exposes — and 70 characters held two of them. `POST /documents, GET
            # /documents, DELETE /documents/{id}, GET /documents/{id}/preview` arrived at the
            # renderer already reading "…DELETE /documents/{id}, GET…", so the reader was told
            # there was a fourth route and not what it was. `FACT_MAX_LINES` decides how much
            # fits, and it wraps rather than cutting a path in half.
            interface = _s(
                item.get("interface")
                or item.get("contract")
                or item.get("exposes")
                or item.get("state")
                or item.get("api"),
                160,
            )
        else:
            continue
        if name:
            out.append({"name": name, "tech": tech, "detail": detail, "interface": interface})
        if len(out) >= limit:
            break
    return out


def _fact_rows(items: list[str]) -> list[str]:
    """A short list of names as up to `FACT_MAX_LINES` fact lines, balanced across them.

    Joining five filenames into one line and letting the renderer clip it wasted the second
    fact line the box already pays height for, and lost the last two names to an "…". Split at
    the midpoint instead: two shorter lines both fit where one long one did not.
    """
    if not items:
        return []
    if len(items) <= 2 or FACT_MAX_LINES < 2:
        return [" · ".join(items)]
    half = (len(items) + 1) // 2
    return [" · ".join(items[:half]), " · ".join(items[half:])]


def _logical_layout(views: dict[str, Any]) -> tuple[list[ArchGroup], list[ArchEdge], str]:
    view = _view(views, "logical")
    groups: list[ArchGroup] = []
    for i, layer in enumerate(_dlist(view.get("layers"), MAX_BANDS), start=1):
        name = _s(layer.get("name")) or f"Layer {i}"
        comps = _parts(layer.get("components"))
        boxes = [
            ArchBox(
                key=c["name"],
                label=c["name"],
                sub=c["tech"],
                note=c["detail"],
                # The contract, when the design named one. A layered picture whose boxes say
                # what each one *exposes* is one a reviewer can check an edge against; without
                # it, "these two talk" is all the arrow can be read as.
                facts=[c["interface"]] if c["interface"] else [],
            )
            for c in comps
        ] or [ArchBox(key=name, label=name)]
        # How many boxes the band holds, beside its responsibility: a layer with one component
        # and a layer with seven are different designs, and the count is the first thing a
        # reader counts by hand otherwise.
        count = f"{len(comps)} component{'s' if len(comps) != 1 else ''}" if comps else ""
        responsibility = _s(layer.get("responsibility"), 280)
        groups.append(
            ArchGroup(
                key=name,
                title=f"{i}. {name}",
                subtitle=" · ".join(x for x in (count, responsibility) if x),
                boxes=boxes,
            )
        )
    # 130, not 90: `EDGE_LABEL_MAX_LINES` lines of `EDGE_LABEL_PX` hold rather more than this, and
    # a flow label on this view is a call signature — the arguments were the half being dropped.
    edges = [
        ArchEdge(_s(f.get("from")), _s(f.get("to")), _s(f.get("label"), 130))
        for f in _dlist(view.get("flows"), MAX_EDGES)
    ]
    return groups, edges, _prose(view.get("description"))


def _development_layout(views: dict[str, Any]) -> tuple[list[ArchGroup], list[ArchEdge], str]:
    """Bands are top-level directories; boxes inside are ordered by dependency depth.

    Two encodings, one picture: the band says who owns the package, the left-to-right position
    says how deep in the import chain it sits. Ordering by depth is what keeps the arrows
    short — a route that imports a service ends up next to it rather than across the band.
    """
    view = _view(views, "development")
    packages = _dlist(view.get("groups"), 28)
    known = {_s(p.get("path")).lower() for p in packages if _s(p.get("path"))}

    edges_raw: list[tuple[str, str]] = []
    for p in packages:
        src = _s(p.get("path"))
        for dep in _slist(p.get("depends_on"), 6):
            if src and dep.lower() in known and dep.lower() != src.lower():
                edges_raw.append((src, dep))

    # Longest-path depth: a package that nothing imports sits at 0, and every dependency is
    # at least one deeper than what imports it.
    depth = {_s(p.get("path")).lower(): 0 for p in packages}
    for _ in range(len(packages)):
        moved = False
        for src, dst in edges_raw:
            if depth.get(dst.lower(), 0) < depth.get(src.lower(), 0) + 1:
                depth[dst.lower()] = depth.get(src.lower(), 0) + 1
                moved = True
        if not moved:
            break

    by_top: dict[str, list[dict[str, Any]]] = {}
    for p in packages:
        top = _s(p.get("top")) or (_s(p.get("path")).split("/")[0] or "root")
        by_top.setdefault(top, []).append(p)

    def _min_depth(top: str) -> int:
        return min(
            (depth.get(_s(p.get("path")).lower(), 0) for p in by_top[top]),
            default=0,
        )

    groups: list[ArchGroup] = []
    # Bands ordered by how shallow their shallowest package is, so the directory holding the
    # entrypoint comes first and the import arrows point down the page. Alphabetical order put
    # `main.py` last and turned the single most important arrow in the view into a dashed line
    # travelling the whole height of the canvas backwards.
    for top in sorted(by_top, key=lambda t: (_min_depth(t), t)):
        members = sorted(
            by_top[top], key=lambda p: (depth.get(_s(p.get("path")).lower(), 0), _s(p.get("path")))
        )
        boxes: list[ArchBox] = []
        for p in members[:MAX_BOXES_PER_BAND]:
            path = _s(p.get("path"))
            if not path:
                continue
            files = p.get("files")
            count = int(files) if isinstance(files, int) and files > 0 else 0
            # Both, not one or the other. This box used to show the filenames *or* the
            # responsibility, and choosing between them is what made this the thinnest of the
            # three pictures: a reader got either "what is in here" or "what it is for", never
            # the pair, and the view read as a folder listing with arrows.
            #
            # `sub` counts the files and the imports, `note` says what the package owns, and
            # the fact line names the files to open.
            key_files = _slist(p.get("key_files"), 5)
            # Counted off `edges_raw`, not off `depends_on` directly, so the numbers agree with
            # the arrows actually drawn — a dependency on something outside the tree, or on
            # itself, is dropped from both.
            imports = len([1 for a, _b in edges_raw if a.lower() == path.lower()])
            imported_by = len([1 for _a, b in edges_raw if b.lower() == path.lower()])
            sub = " · ".join(
                x
                for x in (
                    f"{count} file{'s' if count != 1 else ''}" if count else "",
                    f"imports {imports}" if imports else "",
                    f"used by {imported_by}" if imported_by else "",
                    # A package nothing imports and which imports nothing is either the
                    # entrypoint or a leaf, and saying so is what stops it reading as an
                    # omission from the dependency data.
                    "no imports either way" if not imports and not imported_by else "",
                )
                if x
            )
            boxes.append(
                ArchBox(
                    key=path,
                    label=path,
                    sub=sub,
                    note=_s(p.get("responsibility"), 280),  # see `_components`: the layout bounds it
                    facts=_fact_rows(key_files),
                )
            )
        if not boxes:
            continue
        total = sum(
            int(p.get("files") or 0) for p in members if isinstance(p.get("files"), int)
        )
        depths = [depth.get(_s(p.get("path")).lower(), 0) for p in members]
        span = (
            f"import depth {min(depths)}"
            if min(depths) == max(depths)
            else f"import depth {min(depths)}–{max(depths)}"
        )
        groups.append(
            ArchGroup(
                key=top,
                title=top,
                subtitle=" · ".join(
                    x
                    for x in (
                        f"{len(members)} package{'s' if len(members) != 1 else ''}",
                        f"{total} file{'s' if total != 1 else ''}" if total else "",
                        # Position already encodes depth; naming it is what lets a reader say
                        # *why* this band is where it is rather than inferring it.
                        span if depths else "",
                    )
                    if x
                ),
                boxes=boxes,
            )
        )

    # Import arrows carry no label on purpose: "imports" is the only relationship this view
    # draws, so a label on every arrow would repeat the legend twenty times.
    edges = [ArchEdge(a, b) for a, b in edges_raw[:MAX_EDGES]]
    return groups, edges, _prose(view.get("description"))


def _deployment_layout(views: dict[str, Any]) -> tuple[list[ArchGroup], list[ArchEdge], str]:
    view = _view(views, "deployment")
    groups: list[ArchGroup] = []
    for i, node in enumerate(_dlist(view.get("nodes"), MAX_BANDS), start=1):
        name = _s(node.get("name")) or f"Node {i}"
        runtime = _s(node.get("runtime"))
        hosts = _parts(node.get("hosts"))
        boxes = [
            ArchBox(
                key=h["name"],
                label=h["name"],
                sub=h["tech"],
                note=h["detail"],
                # Whether a process holds state is the one thing about it an operator needs
                # before anything else — it decides whether the box can be replaced, scaled
                # out, or restarted — and it was the fact this view left the reader to guess.
                facts=[h["interface"]] if h["interface"] else [],
            )
            for h in hosts
        ] or [ArchBox(key=f"{name}#self", label=name)]
        # Runtime first: on this view the band title is a place, and what kind of place it is
        # decides how everything inside it is operated.
        detail = " · ".join(
            x
            for x in (
                runtime,
                # No tighter bound than `_s`'s own. A scaling note is the sentence an operator
                # reads before deciding whether the node can be replaced — "…automatic failover"
                # instead of "…automatic failover across AZs" drops the answer — and at 90 it
                # cut every one of them a handful of characters short of the end. The band
                # subtitle wraps, so the strip grows instead of the sentence shrinking.
                _s(node.get("scaling")),
                f"{len(hosts)} process{'es' if len(hosts) != 1 else ''}" if hosts else "",
            )
            if x
        )
        groups.append(ArchGroup(key=name, title=name, subtitle=detail, boxes=boxes))
    edges = []
    for c in _dlist(view.get("connections"), MAX_EDGES):
        # Protocol and port on the same arrow, because "HTTPS" alone is not something a
        # security group can be written from — and the payload under them on a second line
        # rather than joined onto the first, which only produced a clipped one.
        hop = " · ".join(x for x in (_s(c.get("protocol"), 40), _s(c.get("port"), 24)) if x)
        carries = _s(c.get("data"), 110)
        edges.append(
            ArchEdge(_s(c.get("from")), _s(c.get("to")), "\n".join(x for x in (hop, carries) if x))
        )
    return groups, edges, _prose(view.get("description"))


_LAYOUTS = {
    "logical": _logical_layout,
    "development": _development_layout,
    "deployment": _deployment_layout,
}

_DEFAULT_SUBTITLE = {
    "logical": "Layers top to bottom. Every arrow is a call the design permits; anything not "
    "drawn here is a call that should not exist.",
    "development": "Packages grouped by the directory that owns them and ordered by import "
    "depth. Imports run one way — left to right, then down.",
    "deployment": "One band per runtime, the processes it hosts inside it, and the protocol "
    "on every hop between them.",
}


def render_view(
    views: dict[str, Any], kind: str, *, project_name: str = ""
) -> tuple[bytes, int, int, int]:
    """Draw one architectural view. Never raises for thin or malformed input."""
    if kind not in _LAYOUTS:
        raise ValueError(f"Unknown architecture view: {kind}")
    groups, edges, description = _LAYOUTS[kind](views or {})
    meta = ARCH_VIEW_BY_KIND[kind]
    title = f"{project_name} — {meta.label}" if project_name else meta.label
    return _draw(
        groups,
        edges,
        title=title,
        subtitle=description or _DEFAULT_SUBTITLE[kind],
        kind=kind,
    )


def render_architecture(
    views: dict[str, Any],
    *,
    project_name: str = "",
    only: Iterable[str] | None = None,
) -> dict[str, tuple[bytes, int, int, int]]:
    """Draw the requested views. A view that fails to draw is left out, never fatal."""
    wanted = ARCH_KINDS if only is None else tuple(k for k in ARCH_KINDS if k in set(only))
    out: dict[str, tuple[bytes, int, int, int]] = {}
    for kind in wanted:
        try:
            out[kind] = render_view(views, kind, project_name=project_name)
        except Exception:  # noqa: BLE001 — a missing picture must not fail an export
            logger.warning("Architecture view %s failed to render", kind, exc_info=True)
    return out


def views_digest(views: dict[str, Any]) -> str:
    """Content key for the PNG cache: same design, same pictures.

    Includes the renderer revision, so improving a drawing invalidates every cached file
    without anyone having to remember to clear a folder.
    """
    payload = json.dumps(views or {}, sort_keys=True, default=str, ensure_ascii=False)
    raw = f"r{ARCH_REVISION}:{payload}".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


__all__ = [
    "ARCH_ACCENT",
    "ARCH_DOC_DIR",
    "ARCH_KINDS",
    "ARCH_REVISION",
    "ARCH_VIEWS",
    "ARCH_VIEW_BY_KIND",
    "ARCH_WORKSPACE_PATHS",
    "ArchBox",
    "ArchEdge",
    "ArchGroup",
    "ArchitectureView",
    "arch_export_filename",
    "arch_workspace_path",
    "render_architecture",
    "render_view",
    "views_digest",
]
