"""
Render the three diagram views to PNG.

Deterministic drawing, not image generation. A SIPOC or a swimlane carries its meaning
entirely in its labels and in which arrow crosses which lane; a diffusion model produces
something that *looks* like a diagram with unreadable text and arrows joining the wrong
boxes. So the LLM decides content and layout order, and this module does the geometry.

matplotlib with the Agg backend is the renderer: it installs as a pip wheel, bundles the
DejaVu fonts it draws with, measures text exactly, and needs no display — which matters
because this runs headless on EC2 as well as on a Windows dev box. Graphviz would need the
`dot` binary on PATH and Mermaid would need Node plus headless Chrome; neither is worth a
deployment dependency for three diagrams.

`Figure` is used directly rather than `pyplot` — pyplot keeps a global figure registry that
is not safe to share between request handlers.
"""

from __future__ import annotations

import io
import math
from typing import Callable, Iterable

import matplotlib

matplotlib.use("Agg")  # must precede any figure creation

from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle  # noqa: E402

from app.models.diagrams import (
    FlowDiagram,
    ProcessEdge,
    ProcessModel,
    SipocDiagram,
    SwimlaneDiagram,
)

DPI = 100
FONT = "DejaVu Sans"

#: Device pixels per layout pixel in the saved PNG.
#:
#: Every coordinate below is a *layout* pixel at `DPI`, which is also the size the diagram is
#: meant to be read at on screen. Saving at `DPI * RENDER_SCALE` keeps that layout identical
#: and just puts more pixels behind it, so the text stays sharp when the viewer zooms past
#: 100% and when a browser downscales a wide swimlane to fit its pane. The alternative —
#: growing the fonts — would make every diagram physically larger without making any of it
#: more legible once it is scaled to fit.
RENDER_SCALE = 3

#: Bumped whenever this module's output changes. It is part of the cached PNG filename, so a
#: renderer change re-draws existing projects instead of serving a stale image (the PNGs are
#: a cache of the stored model, never a source of truth).
#: 8 — nothing on a diagram is cut off any more. The header scope, a SIPOC cell note and the
#: Measures / Records bands were each bounded by a line count chosen when the text was short,
#: and a real brief overran all three; the bounds are now high enough that only prose nobody
#: would call a scope reaches them, and the canvas grows to fit whatever is drawn.
#: 9 — no text can be drawn outside the shape that owns it. Word wrapping had nowhere to break
#: `GET /projects/{id}/plan/architecture/{kind}.png`, so an endpoint or a table name wider than
#: its box was drawn straight through both edges of it; a token that does not fit is now broken
#: at a seam (`/`, `.`, `-`) instead, which keeps all of it inside the box.
#: 10 — the last string the audit found being cut was the *title*, on the narrow SIPOC canvas:
#: a project named after its own one-line summary lost the end of it to an ellipsis. The title
#: wraps to a second line now, the band and the body are measured for it, and the badge keeps
#: the first line's right-hand end to itself.
#: 11 — the line budgets were sized against a demo project, and a real one overran nearly all of
#: them: every step's detail on the flow lost the clause naming the requirement it satisfies, a
#: step's systems caption lost the second of the two services it touches, a branch label lost its
#: condition, and a lane's responsibilities were dropped whenever the lane happened to be short.
#: Each budget is now set from the widest text a real plan produces, edge labels are wrapped by
#: one shared helper that allocates lines across their rows instead of starving the first, and a
#: lane grows to hold its own header. The last of them was not a budget at all: `room` for the
#: detail came back as 5.999999999999999 lines for the six the height pass had just paid for, so
#: floor division cut the sentence that had decided the height of every box. Zero cuts on all six
#: views for the project that reported the bug.
#: With nothing cut, what was left was text made unreadable by something drawn over it, which
#: costs the reader the same words. An edge label sits on an opaque plate, so two of them in one
#: place is not one slightly obscured but both lost, and a plate in the middle of a box's sentence
#: reads exactly like a truncated one. So every string drawn is recorded, not just the labels, and
#: a label looks for a spot in this order: clear of the shapes, clear of all text, then further
#: out. Distance is bounded too — a label 269px off its own arrow on an 896px page belongs to no
#: arrow at all — and past that bound the attachment is drawn as a dotted leader instead of being
#: left to proximity. Zero labels over any text, zero label-on-label, three leaders in six views.
RENDER_REVISION = 11

INK = "#12141f"
MUTED = "#5b6070"
ARROW = "#5d6377"
#: A handoff that skips a stage or crosses lanes. Lighter than the spine on purpose: the
#: step-to-next-step path should be the line the eye follows first.
ARROW_SOFT = "#9298ab"
LOOP = "#b06a2c"
PAGE = "#ffffff"
PANEL = "#f7f8fc"
HAIR = "#d7dae6"

#: Offsets, in layout pixels, for stacking parallel connectors inside one channel. Never 0 for
#: the horizontal set: a route drawn exactly on a lane divider reads as part of the divider.
_V_OFFSETS: tuple[float, ...] = (0.0, 9.0, -9.0, 13.0, -13.0, 5.0, -5.0)
_H_OFFSETS: tuple[float, ...] = (8.0, -8.0, 14.0, -14.0, 11.0, -11.0)

#: Drawn under the flow and swimlane views. The shapes and the line styles both carry meaning,
#: and a reader who has to infer "dashed orange means it goes backwards" has already stopped
#: reading the process.
LEGEND_SHAPES: tuple[tuple[str, str], ...] = (
    ("start", "Start"),
    ("task", "Step"),
    ("decision", "Decision"),
    ("subprocess", "Sub-process"),
    ("end", "End"),
)

#: Fill / border per step kind. Kept consistent across the flow and swimlane views so the
#: same step is the same colour in both — that is what lets a reader match them up.
SHAPE_STYLE: dict[str, tuple[str, str]] = {
    "start": ("#e4f7ec", "#1f9d55"),
    "task": ("#eef1fe", "#4a57cf"),
    "decision": ("#fff3e0", "#c9781c"),
    "subprocess": ("#f1eaff", "#7245e0"),
    "end": ("#fde9e9", "#c23b3b"),
}

#: SIPOC column tints, left to right.
SIPOC_COLORS: list[tuple[str, str]] = [
    ("#e8effc", "#3a63b8"),
    ("#e6f6f1", "#1f8f72"),
    ("#eeeafd", "#6b4bd0"),
    ("#fdf1e4", "#b9762a"),
    ("#fbe9ef", "#b93a63"),
]

LANE_TINT = {"human": "#f5f7fd", "system": "#f2f8f6", "external": "#fdf5f1"}


# ── Text metrics ────────────────────────────────────────────────────────────────
#
# Wrapping decides the box heights, which decides the canvas size, which has to be known
# before the real figure exists. So a throwaway figure at the same DPI does the measuring;
# text width in pixels is DPI-dependent but identical between two figures that share one.

_scratch: Figure | None = None
_widths: dict[tuple[str, float, str], float] = {}


def _measure(text: str, size: float, weight: str = "normal") -> float:
    global _scratch
    if not text:
        return 0.0
    key = (text, size, weight)
    hit = _widths.get(key)
    if hit is not None:
        return hit
    if _scratch is None:
        _scratch = Figure(figsize=(4, 4), dpi=DPI)
        FigureCanvasAgg(_scratch)
    artist = _scratch.text(0, 0, text, fontsize=size, fontweight=weight, fontfamily=FONT)
    width = artist.get_window_extent(renderer=_scratch.canvas.get_renderer()).width
    artist.remove()
    _widths[key] = width
    return width


def _ellipsize(text: str) -> str:
    """Mark a cut, once. A string that arrives already shortened keeps its single "…".

    The label may have been bounded twice on its way here — `build.py`'s `_cap` shortens what
    goes into the stored model, then wrapping shortens again to fit a box — and "current……"
    reads as a rendering bug rather than as a sentence that continues.
    """
    text = text.rstrip().rstrip("…").rstrip(" ,;:.")
    return f"{text}…" if text else "…"


#: Characters a long unbroken token may be broken *after*. A path, an endpoint or a dotted
#: package name has natural seams, and breaking at one reads as a continuation where breaking
#: mid-word reads as a rendering fault.
_BREAK_AFTER = "/\\·:;,-_.)]}>"


def _split_wide(text: str, max_px: float, size: float, weight: str = "normal") -> list[str]:
    """Break one token that is wider than the box it has to fit in.

    Greedy word wrapping cannot help here: `GET /projects/{id}/plan/architecture/{kind}.png`
    holds no space after "GET", so the whole endpoint arrived as a single word and was simply
    drawn past both edges of its box. Splitting after a `/` keeps every character of it on the
    picture — the alternative, clipping, throws away exactly the part of a contract a reader
    came to check.
    """
    if _measure(text, size, weight) <= max_px:
        return [text]
    out: list[str] = []
    rest = text
    while rest and _measure(rest, size, weight) > max_px:
        # The longest prefix that fits, by binary search over the measured width.
        lo, hi = 1, len(rest)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _measure(rest[:mid], size, weight) <= max_px:
                lo = mid
            else:
                hi = mid - 1
        cut = max(1, lo)
        # Pull the break back to a seam, but only a seam in the second half of the line: a token
        # like `/a/very-long-single-segment` would otherwise break after its first character.
        seam = max((rest.rfind(ch, 0, cut) for ch in _BREAK_AFTER), default=-1)
        if seam >= cut * 0.5:
            out.append(rest[: seam + 1])
            rest = rest[seam + 1 :]
            continue
        # No seam to break at, so the break lands inside a word — and `format/readabilit` above
        # `y?` reads as a typo, not as one word carried over. A hyphen is the ordinary way to say
        # "this continues"; it takes width of its own, so the prefix is pulled back until the
        # hyphen fits too. Only between two word characters: a hyphen after a bracket or a
        # comma would be inventing punctuation.
        hyphen = rest[cut - 1].isalnum() and rest[cut:cut + 1].isalnum()
        if hyphen:
            while cut > 1 and _measure(f"{rest[:cut]}-", size, weight) > max_px:
                cut -= 1
            hyphen = rest[cut - 1].isalnum() and rest[cut:cut + 1].isalnum()
        out.append(f"{rest[:cut]}-" if hyphen else rest[:cut])
        rest = rest[cut:]
    if rest:
        out.append(rest)
    return out


def _wrap(text: str, max_px: float, size: float, weight: str = "normal", max_lines: int = 4) -> list[str]:
    """Greedy wrap to a pixel width, truncating with an ellipsis rather than overflowing."""
    text = (text or "").strip()
    if not text:
        return []
    rows: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if _measure(candidate, size, weight) <= max_px or not current:
            current = candidate
        else:
            rows.append(current)
            current = word
    if current:
        rows.append(current)
    # Nothing may be wider than the space it was given, so a row still over the width after word
    # wrapping is broken again at a seam. Done for every row rather than only the last, and
    # before the line budget is applied, so the budget is spent on lines that are really drawn.
    lines: list[str] = []
    for row in rows:
        lines.extend(_split_wide(row, max_px, size, weight))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines and _measure(_ellipsize(lines[-1]), size, weight) > max_px and len(lines[-1]) > 1:
            lines[-1] = lines[-1][:-1]
        lines[-1] = _ellipsize(lines[-1])
    return lines


def _line_h(size: float) -> float:
    return size * 1.45 * DPI / 72.0


def _clip(text: str, max_px: float, size: float, weight: str = "normal") -> str:
    """One line, cut to a *width* with an ellipsis — never to a character count.

    `label[:24]` is a guess about how wide 24 characters are, and it is wrong in both
    directions: it broke "Compliance Officer (second line)" mid-word into something that gave
    the reader no sign anything was missing, while letting a short but wide all-caps label
    overflow the space it had. Measuring is the only way to know, and the "…" is what turns a
    cut from a mistake into a statement that there is more.
    """
    text = (text or "").strip()
    if not text or _measure(text, size, weight) <= max_px:
        return text
    while text and _measure(_ellipsize(text), size, weight) > max_px:
        text = text[:-1]
    return _ellipsize(text) if text else ""


# ── Header ──────────────────────────────────────────────────────────────────────
#
# The title, and under it the process scope. The scope is a sentence or two of prose, so it
# is the one piece of text on the page that has to be laid out to the canvas rather than to a
# box — and it is what used to be cut off mid-word: it was drawn as one unwrapped line of
# `scope[:150]`, which the right edge of a narrow canvas then clipped again. Now it wraps, the
# body starts below however many lines it took, and only a genuinely long scope is shortened —
# with an ellipsis, so the cut is visible.

HEADER_PAD = 28.0
HEADER_TITLE_Y = 30.0
HEADER_SUB_Y = 54.0
HEADER_SUB_SIZE = 9.0
HEADER_TITLE_SIZE = 15.0
HEADER_BADGE_SIZE = 8.2
#: The title wraps rather than being clipped, to at most two lines. It used to be a single
#: `_clip`, which on the narrow SIPOC canvas turned a project called *"A claims intake portal
#: for a mid-size general insurer"* into *"A claims intake portal for a mid-size g…"* — and the
#: title is the one line on the page that says which project the reader is looking at, so an
#: ellipsis there is worse than anywhere else. Two lines because a third would be a paragraph
#: pretending to be a heading; a title longer than that is still ellipsised, visibly.
HEADER_TITLE_MAX_LINES = 2
#: Clear space between the last scope line and the top of the body.
HEADER_GAP = 34.0
#: Eight lines. Four was picked for "starts when … ends when … out of scope: …" and a real one
#: overran it — the SIPOC scope lost its whole out-of-scope clause to an "…", and the
#: architecture views, whose scope is a paragraph explaining the layering rule, lost the
#: sentence that made the picture worth reading.
#:
#: Raising it costs nothing but header height: `_header_height` measures these lines and pushes
#: the body down, so the canvas grows rather than the text being drawn over the first row. The
#: bound is kept only so a payload that puts a whole document in `description` cannot produce a
#: picture that is all header — which is why it is 8 and not unbounded.
HEADER_SUB_MAX_LINES = 8
#: Where the body starts when there is no scope at all — unchanged from before.
HEADER_MIN_H = 96.0


def _header_lines(subtitle: str, width: float) -> list[str]:
    """The scope, wrapped to the canvas it will be drawn on."""
    return _wrap(
        subtitle,
        max(200.0, width - 2 * HEADER_PAD),
        HEADER_SUB_SIZE,
        max_lines=HEADER_SUB_MAX_LINES,
    )


def _title_lines(title: str, width: float, *, badge: str = "") -> tuple[list[str], float]:
    """`(the title's lines, the width the badge keeps)` — the one measurement of the title line.

    Only the *first* line has to leave room for the badge, which sits on it; a second line has
    the whole canvas. `_wrap` takes one width for every line, so the first line is filled here
    against the narrower bound and the remainder is wrapped against the full one.

    The badge still yields to the title, as it always did — it only names which of the three
    views this is, which the shapes and the accent colour say already, so if dropping it is what
    keeps the title on one line, it is dropped. Called by `header` to draw and by
    `_header_band` / `_header_height` to measure, so the three cannot disagree.
    """
    full_px = max(120.0, width - 2 * HEADER_PAD)
    badge_w = _measure(badge.upper(), HEADER_BADGE_SIZE, "bold") + 22.0 if badge else 0.0
    text = (title or "Process").strip()
    if _measure(text, HEADER_TITLE_SIZE, "bold") <= full_px - badge_w:
        return [text], badge_w
    if badge_w and _measure(text, HEADER_TITLE_SIZE, "bold") <= full_px:
        return [text], 0.0

    first = ""
    words = text.split()
    while words and _measure(
        f"{first} {words[0]}".strip(), HEADER_TITLE_SIZE, "bold"
    ) <= full_px - badge_w:
        first = f"{first} {words.pop(0)}".strip()
    if not first:
        # A single word wider than the badge leaves room for. The badge goes rather than the
        # title being broken mid-word on the very first line.
        badge_w = 0.0
        while words and _measure(
            f"{first} {words[0]}".strip(), HEADER_TITLE_SIZE, "bold"
        ) <= full_px:
            first = f"{first} {words.pop(0)}".strip()
    if not first:
        # Still nothing fits: one unbreakable token wider than the canvas. Let `_wrap` seam it.
        return _wrap(
            text, full_px, HEADER_TITLE_SIZE, "bold", max_lines=HEADER_TITLE_MAX_LINES
        ), 0.0
    # An architecture title is `<project name> — <view>`, so the greedy fill above regularly
    # ended the first line on the lone em dash and started the second on "Development view". A
    # dash hanging off the end of a heading reads as a typesetting fault, so it travels with the
    # words it joins — never leaving the first line empty, which would be worse.
    first_words = first.split()
    if len(first_words) > 1 and first_words[-1] in {"—", "–", "-", "·", "|", ":"}:
        words.insert(0, first_words.pop())
        first = " ".join(first_words)
    rest = _wrap(
        " ".join(words),
        full_px,
        HEADER_TITLE_SIZE,
        "bold",
        max_lines=HEADER_TITLE_MAX_LINES - 1,
    )
    return [first, *rest], badge_w


def _title_extra(title: str, width: float, *, badge: str = "") -> float:
    """How far a wrapped title pushes everything under it down. Zero for a one-line title."""
    lines, _ = _title_lines(title, width, badge=badge)
    return (len(lines) - 1) * _line_h(HEADER_TITLE_SIZE)


def _header_band(subtitle: str, width: float, *, title: str = "", badge: str = "") -> float:
    """Bottom edge of the tinted header band — the rule that separates prose from drawing.

    Clear space alone was not enough: a paragraph of grey text sitting on the same white as
    the boxes reads as part of the picture, so a long scope looked like a caption that had
    collided with the first step. A band with its own tint and a hairline under it says "this
    is the header" before anyone reads a word of it.

    Measured from the text, not from the body's `top`: the flow view leaves extra room under
    the rule for its step captions, and the rule must not move because of it.

    `title` / `badge` are optional so a caller that only has a scope still measures correctly for
    a one-line title, which is every existing caller's case — passing them is what makes the band
    grow for a title that took two lines instead of the second line landing on the first box.
    """
    extra = _title_extra(title, width, badge=badge)
    lines = _header_lines(subtitle, width)
    if not lines:
        return HEADER_TITLE_Y + extra + 18.0
    return HEADER_SUB_Y + extra + (len(lines) - 1) * _line_h(HEADER_SUB_SIZE) + 16.0


def _header_height(
    subtitle: str,
    width: float,
    *,
    gap: float = HEADER_GAP,
    title: str = "",
    badge: str = "",
) -> float:
    """Top of the body, given a scope that may need more than one line.

    Called by each renderer *after* it knows its width and *before* it computes its height,
    because a two-line scope has to push the whole diagram down rather than be drawn over it.

    `gap` is clear space measured from the *baseline* of the last scope line, so a view that
    hangs anything above its first row has to ask for more of it — see `FLOW_CAPTION_BAND`.
    """
    extra = _title_extra(title, width, badge=badge)
    lines = _header_lines(subtitle, width)
    if not lines:
        return max(HEADER_MIN_H, HEADER_MIN_H + extra)
    last_baseline = HEADER_SUB_Y + extra + (len(lines) - 1) * _line_h(HEADER_SUB_SIZE)
    return max(HEADER_MIN_H + extra, last_baseline + gap)


#: The flow view prints each step's id and actor *above* its box, so its first row needs a
#: caption's worth of extra clearance under the scope. SIPOC and the swimlane start their
#: bodies exactly at `top` and need none.
FLOW_CAPTION_BAND = 20.0


#: One accent colour per view, drawn as a bar down the left of the header band and used for
#: the badge that names the view. Three pictures of the same process are easy to mix up in a
#: folder of PNGs; a colour is recognisable before any text is legible.
VIEW_ACCENT = {"sipoc": "#3a63b8", "flow": "#2f9e6f", "swimlane": "#7a4fc4"}


#: Widest an edge label may be. These sit on an opaque plate in the empty gap between two
#: steps, so the limit is about that gap, not about any box: wider, and "no — needs more
#: information" starts covering the step it points away from.
EDGE_LABEL_PX = 132.0
#: Lines an edge label may take. The width above is deliberately narrow, and for a long time
#: nothing wrapped into it — a caption was `_clip`ped at one line, so twenty-five-character
#: branch labels on a real project came out as "authentication succe…", "further document act…",
#: "format invalid or unr…". The plate is measured and placed per line (`_label_half`,
#: `_label_point`), so extra lines grow downward into the same gap and still avoid every shape.
#: Three, because a branch label is a short clause and three lines of this width hold about
#: seventy characters — past that the plate is taller than the gap it sits in.
EDGE_LABEL_MAX_LINES = 3


def _edge_caption(
    text: str,
    *,
    width: float = EDGE_LABEL_PX,
    size: float = 7.6,
    max_lines: int = EDGE_LABEL_MAX_LINES,
) -> str:
    """An edge label as the drawn string: wrapped into the plate, not clipped to one line.

    A caption may arrive with its own line break — a deployment hop is written as `HTTPS · 443`
    over `Signed-in user requests` — and those rows are kept as rows. Each is wrapped into what
    is left of the budget minus one line held back for every row still to come, so a row taking
    a second line cannot leave the row after it with none.

    The defaults are the blueprint's; `architecture.py` passes its own, because its arrows run in
    a wider corridor. Both go through here so the shape of the rule is written once.
    """
    rows = [row for row in (text or "").split("\n") if row.strip()]
    if not rows:
        return ""
    out: list[str] = []
    for i, row in enumerate(rows):
        room = max_lines - len(out) - (len(rows) - 1 - i)
        if room <= 0:
            break
        out.extend(_wrap(row, width, size, max_lines=room))
    return "\n".join(out[:max_lines])


# ── Canvas ──────────────────────────────────────────────────────────────────────


class _Canvas:
    """A pixel-coordinate drawing surface with y increasing downward.

    Inverting the y axis means every layout calculation below reads top-to-bottom the same
    way the diagram does, which removes a whole class of sign mistakes.
    """

    def __init__(self, width: float, height: float) -> None:
        self.w = max(360.0, width)
        self.h = max(240.0, height)
        self.fig = Figure(figsize=(self.w / DPI, self.h / DPI), dpi=DPI, facecolor=PAGE)
        FigureCanvasAgg(self.fig)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, self.w)
        self.ax.set_ylim(self.h, 0)
        self.ax.set_axis_off()
        # Shapes an edge label must not be placed on top of, filled in by the layout once it
        # knows where the boxes are. A label's plate hides whatever is under it, so a label
        # landing mid-box costs the box's own name — the one thing that must stay readable.
        self.obstacles: list[tuple[float, float, float, float]] = []
        # The subset of `obstacles` that is itself text. When a label can find nowhere clear of
        # everything, these are the ones it must still avoid: a plate over part of a box costs
        # some of that box's own words, but a plate over another label costs *both* labels
        # entirely, and there is no reading of the picture that recovers them.
        self.text_obstacles: list[tuple[float, float, float, float]] = []

    # -- primitives --

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        fill: str,
        edge: str = "none",
        lw: float = 1.0,
        radius: float = 0.0,
        z: int = 2,
    ) -> None:
        if radius > 0:
            patch = FancyBboxPatch(
                (x + radius, y + radius),
                max(1.0, w - 2 * radius),
                max(1.0, h - 2 * radius),
                boxstyle=f"round,pad={radius}",
                facecolor=fill,
                edgecolor=edge,
                linewidth=lw,
                zorder=z,
                mutation_aspect=1,
            )
        else:
            patch = Rectangle(
                (x, y), w, h, facecolor=fill, edgecolor=edge, linewidth=lw, zorder=z
            )
        self.ax.add_patch(patch)

    def diamond(self, cx: float, cy: float, w: float, h: float, *, fill: str, edge: str) -> None:
        pts = [(cx, cy - h / 2), (cx + w / 2, cy), (cx, cy + h / 2), (cx - w / 2, cy)]
        self.ax.add_patch(
            Polygon(pts, closed=True, facecolor=fill, edgecolor=edge, linewidth=1.2, zorder=2)
        )

    def text(
        self,
        x: float,
        y: float,
        content: str,
        *,
        size: float = 9.5,
        color: str = INK,
        weight: str = "normal",
        ha: str = "center",
        va: str = "center",
        style: str = "normal",
        z: int = 4,
        halo: bool = False,
        halo_color: str = PAGE,
    ) -> None:
        self.ax.text(
            x,
            y,
            content,
            fontsize=size,
            color=color,
            fontweight=weight,
            fontstyle=style,
            ha=ha,
            va=va,
            fontfamily=FONT,
            zorder=z,
            # A halo is a plate behind the glyphs, in the colour of whatever the text sits on.
            # Used for captions in the gap between two layers, and for a band's own heading,
            # where an arrow drawn earlier would otherwise run straight through the letters.
            bbox={"facecolor": halo_color, "edgecolor": "none", "pad": 1.2, "alpha": 0.92}
            if halo
            else None,
        )
        # Every string drawn on the page is recorded, not just the edge labels. An edge label sits
        # on an opaque plate, so "the label found a spot clear of the other labels" is no comfort
        # if the spot was the middle of a box's own sentence — the plate blanks the words out, and
        # a half-covered line of prose reads as broken text, which is the same defect from the
        # reader's side as a truncated one. Shapes are already in `obstacles`; this is the ink
        # *inside* them, so `_label_point` can prefer a box's padding over its words.
        if content.strip():
            w = max(_measure(row, size, weight) for row in content.split("\n"))
            h = _line_h(size) * len(content.split("\n"))
            left = x if ha == "left" else x - w if ha == "right" else x - w / 2
            bottom = y if va == "bottom" else y - h if va == "top" else y - h / 2
            self.text_obstacles.append((left, bottom, w, h))

    def lines(
        self,
        cx: float,
        cy: float,
        rows: list[str],
        *,
        size: float = 9.5,
        color: str = INK,
        weight: str = "normal",
        style: str = "normal",
        z: int = 4,
        halo: bool = False,
    ) -> None:
        """Vertically centre a wrapped block on (cx, cy).

        `halo` and `z` are here so a block drawn in open space — the systems caption under a
        diamond, which arrows pass through — gets the same backing a single `text` would.
        """
        if not rows:
            return
        lh = _line_h(size)
        top = cy - (len(rows) - 1) * lh / 2
        for i, row in enumerate(rows):
            self.text(
                cx,
                top + i * lh,
                row,
                size=size,
                color=color,
                weight=weight,
                style=style,
                z=z,
                halo=halo,
            )

    def _label_point(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        label: str,
        *,
        rad: float,
        label_t: float,
        rotated: bool = False,
    ) -> tuple[float, float]:
        """Where to put an edge label: on its arrow, but not over a shape if that is avoidable.

        `label_t` is the preferred position along the chord. It is only a preference: a long
        arrow crosses stages it has nothing to do with, and the label's own plate would blank
        out whatever box it lands on. So the preferred spot is tried first, then others either
        side of it, and the first one clear of every registered shape wins. If the whole chord
        is covered — a short arrow between two touching boxes — the preference stands, because
        moving the label off its own arrow is worse than a slight overlap.
        """

        def point(t: float) -> tuple[float, float]:
            px = start[0] + (end[0] - start[0]) * t
            py = start[1] + (end[1] - start[1]) * t
            if rad:
                # Offset onto the curve, perpendicular to the chord.
                dx, dy = end[0] - start[0], end[1] - start[1]
                length = math.hypot(dx, dy) or 1.0
                px += -dy / length * rad * length * 0.5
                py += dx / length * rad * length * 0.5
            return px, py

        preferred = point(label_t)
        if not self.obstacles:
            return preferred
        half_w, half_h = self._label_half(label, rotated=rotated)

        def clear(pt: tuple[float, float], among: list[tuple[float, float, float, float]]) -> bool:
            return not any(
                pt[0] + half_w > bx
                and pt[0] - half_w < bx + bw
                and pt[1] + half_h > by
                and pt[1] - half_h < by + bh
                for bx, by, bw, bh in among
            )

        if clear(preferred, self.obstacles):
            return preferred
        # Ordered by distance from the preference, so the label stays as close to where the
        # layout wanted it as the shapes allow.
        candidates = sorted(
            (round(0.08 + 0.06 * i, 2) for i in range(15)), key=lambda t: abs(t - label_t)
        )
        # On the chord first, then in bands parallel to it. The plate is opaque, so two labels in
        # the same place is not one of them slightly obscured, it is *both* of them lost — on the
        # swimlane it made "preview / delete / restore" and "further actions required" a single
        # smudge between two diamonds, and on the Logical View it drew "route dispatch after CORS
        # middleware" through two boxes. Sliding along the chord alone is not enough: a horizontal
        # arrow crossing a row of boxes has no clear point anywhere along it, and the free space is
        # a whole line above or below. So the offset and the slide are searched together, nearest
        # band and nearest slide first, and the label ends up as close to its own arrow as the rest
        # of the picture allows.
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length, dx / length
        step = max(half_w if abs(nx) > abs(ny) else half_h, 6.0) * 2 + 3.0

        def search(
            among: list[tuple[float, float, float, float]], bands: range
        ) -> tuple[float, float] | None:
            for band in bands:
                for sign in (1, -1) if band else (1,):
                    ox, oy = nx * step * band * sign, ny * step * band * sign
                    for t in candidates:
                        base = point(t)
                        pt = (base[0] + ox, base[1] + oy)
                        if clear(pt, among):
                            return pt
            return None

        # Two things can go wrong, and they pull in opposite directions. Stay close and the plate
        # lands on something; move far enough and it lands on nothing — but a label 269px off its
        # own arrow on an 896px page floats in white space beside somebody else's arrow, and the
        # reader cannot tell which edge it belongs to. So both are bounded: `near` is how far a
        # label may drift and still read as attached, about three of its own heights, and the
        # search widens past it only when the near bands offer nothing.
        #
        # Within each distance, the tie-break is what gets covered. `obstacles` is shapes and
        # labels; `text_obstacles` is every string on the page. A spot clear of the shapes is
        # ideal, and a spot merely clear of all *text* is the acceptable fallback — it means the
        # plate is over a box's padding, hiding no words. Covering words is what the last resort
        # costs, so it comes last, after even a distant clean spot has been ruled out.
        near, far = range(0, 4), range(4, 9)
        placed = (
            search(self.obstacles, near)
            or search(self.text_obstacles, near)
            or search(self.obstacles, far)
            or search(self.text_obstacles, far)
        )
        if placed is None:
            return preferred
        self._leader(placed, point, half_w, half_h)
        return placed

    def _leader(
        self,
        placed: tuple[float, float],
        point: Callable[[float], tuple[float, float]],
        half_w: float,
        half_h: float,
    ) -> None:
        """Join a displaced label back to the arrow it describes, with a hairline.

        A label that had to go into the far bands is unambiguous about what it covers and
        ambiguous about what it means: "TCP · 5432 / Read-only analytics and history queries"
        ended up alone in the middle of a band, nearer three other arrows than its own, and a
        reader has no way to recover which connection it annotates. So past the distance at which
        proximity still says it — roughly the label's own size — the attachment is drawn instead
        of implied. The line starts at the edge of the plate rather than its centre, and sits
        under it, so the plate covers no part of its own leader.
        """
        anchor = min(
            (point(i / 24) for i in range(25)),
            key=lambda p: math.hypot(p[0] - placed[0], p[1] - placed[1]),
        )
        dx, dy = anchor[0] - placed[0], anchor[1] - placed[1]
        if math.hypot(dx, dy) <= max(half_w, half_h) + 4:
            return
        exit_t = min(
            half_w / abs(dx) if dx else math.inf,
            half_h / abs(dy) if dy else math.inf,
        )
        self.ax.plot(
            [placed[0] + dx * exit_t, anchor[0]],
            [placed[1] + dy * exit_t, anchor[1]],
            color=MUTED,
            linewidth=0.7,
            alpha=0.4,
            zorder=2,
            # Dotted, and lighter than any arrow. The leader has to be followable or it is not
            # worth drawing — in `HAIR` it was invisible at reading size — but it must never be
            # mistaken for a connection in the diagram, which is what a solid grey line of this
            # weight would look like.
            linestyle=(0, (1, 2)),
            solid_capstyle="round",
        )

    def _edge_label(
        self, x: float, y: float, label: str, *, color: str, rotated: bool = False
    ) -> None:
        """Draw an edge label on its plate and claim the space it covers."""
        self.ax.text(
            x,
            y,
            label,
            fontsize=7.6,
            color=color,
            ha="center",
            va="center",
            fontfamily=FONT,
            zorder=5,
            rotation=90 if rotated else 0,
            bbox={"facecolor": PAGE, "edgecolor": "none", "pad": 1.4, "alpha": 0.94},
        )
        self.reserve(x, y, label, rotated=rotated)

    def arrow(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        label: str = "",
        rad: float = 0.0,
        color: str = ARROW,
        dashed: bool = False,
        label_t: float = 0.5,
        lw: float = 1.2,
    ) -> None:
        self.ax.add_patch(
            FancyArrowPatch(
                start,
                end,
                arrowstyle="-|>",
                mutation_scale=10 + lw * 1.6,
                connectionstyle=f"arc3,rad={rad}",
                linewidth=lw,
                linestyle=(0, (4, 3)) if dashed else "solid",
                color=color,
                shrinkA=0,
                shrinkB=0,
                zorder=1,
            )
        )
        if not label:
            return
        mx, my = self._label_point(start, end, label, rad=rad, label_t=label_t)
        self._edge_label(mx, my, label, color=color)

    def route(
        self,
        points: list[tuple[float, float]],
        *,
        label: str = "",
        color: str = ARROW,
        dashed: bool = False,
        lw: float = 1.2,
    ) -> None:
        """An orthogonal connector: straight runs and right angles, arrowhead on the last leg.

        A curved arrow between boxes three stages and four lanes apart is drawn *through*
        everything between them, and the reader cannot tell which end belongs to which box —
        thirty of them is the arrow tangle. A route travels in the empty channels between
        lanes and columns instead, so each one can be followed by eye, which is the only
        reason to draw the handoff at all.

        `points` are the corners, source anchor first, target anchor last. Duplicate corners
        are dropped so a collapsed leg cannot produce a zero-length arrow.
        """
        pts: list[tuple[float, float]] = [points[0]]
        for p in points[1:]:
            if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1.0:
                pts.append(p)
        if len(pts) < 2:
            return
        if len(pts) > 2:
            self.ax.plot(
                [p[0] for p in pts[:-1]],
                [p[1] for p in pts[:-1]],
                color=color,
                linewidth=lw,
                linestyle=(0, (4, 3)) if dashed else "solid",
                zorder=1,
                solid_capstyle="round",
                dash_capstyle="round",
                solid_joinstyle="round",
            )
        self.arrow(pts[-2], pts[-1], color=color, dashed=dashed, lw=lw)
        if not label:
            return
        # Put the label on the longest leg, preferring a horizontal one: upright text reads
        # without turning the page, and the long leg is the part of the route a reader is
        # trying to trace when they need the label.
        best: tuple[float, tuple[float, float], tuple[float, float], bool] | None = None
        for a, b in zip(pts, pts[1:]):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            horizontal = abs(b[1] - a[1]) <= abs(b[0] - a[0])
            score = length * (1.4 if horizontal else 1.0)
            if best is None or score > best[0]:
                best = (score, a, b, horizontal)
        assert best is not None
        _, a, b, horizontal = best
        lx, ly = self._label_point(a, b, label, rad=0.0, label_t=0.5, rotated=not horizontal)
        self._edge_label(lx, ly, label, color=color, rotated=not horizontal)

    def legend(
        self,
        x: float,
        y: float,
        w: float,
        *,
        shapes: Iterable[tuple[str, str]] = (),
        strokes: Iterable[tuple[str, bool, str]] = (),
        height: float = 34.0,
    ) -> None:
        """A key strip for shape colours and line styles.

        Colour and dash pattern both carry meaning here, and meaning a reader has to guess at
        is meaning they get wrong — "why is that one orange" is answered once, in the picture.
        """
        self.rect(x, y, w, height, fill=PANEL, edge=HAIR, radius=4, z=1)
        cursor = x + 14
        cy = y + height / 2
        for kind, name in shapes:
            fill, edge = _shape_of(kind)
            radius = 6.0 if kind in ("start", "end") else 3.0
            if cursor + 24 + _measure(name, 7.6) + 14 > x + w:
                return
            self.rect(cursor, cy - 6, 18, 12, fill=fill, edge=edge, lw=1.1, radius=radius, z=2)
            self.text(cursor + 24, cy, name, size=7.6, color=MUTED, ha="left")
            cursor += 24 + _measure(name, 7.6) + 16
        for color, dashed, name in strokes:
            if cursor + 30 + _measure(name, 7.6) + 14 > x + w:
                return
            self.ax.plot(
                [cursor, cursor + 22],
                [cy, cy],
                color=color,
                linewidth=1.4,
                linestyle=(0, (4, 3)) if dashed else "solid",
                zorder=2,
                solid_capstyle="round",
            )
            self.text(cursor + 28, cy, name, size=7.6, color=MUTED, ha="left")
            cursor += 28 + _measure(name, 7.6) + 16

    def _label_half(self, label: str, *, rotated: bool) -> tuple[float, float]:
        """Half the width and half the height of the plate an edge label will occupy.

        Measured per line: a label carrying a protocol on one line and its payload on the next
        is one text object with a newline in it, and measuring the joined string would claim a
        strip twice as wide as the plate really is and half as tall — exactly backwards for the
        overlap this measurement exists to prevent.
        """
        rows = label.split("\n")
        along = max(_measure(row, 7.6, "normal") for row in rows) / 2 + 2.0
        across = _line_h(7.6) * len(rows) / 2
        return (across, along) if rotated else (along, across)

    def reserve(self, x: float, y: float, label: str, *, rotated: bool = False) -> None:
        """Record a placed label as an obstacle, so the next one does not sit on top of it.

        Two edges leaving the same box run near-parallel for a while, so without this their
        labels pile up in the same spot and neither can be read — which is a worse failure
        than a label on a box, because the box at least still says what it is.
        """
        half_w, half_h = self._label_half(label, rotated=rotated)
        rect = (x - half_w, y - half_h, half_w * 2, half_h * 2)
        self.obstacles.append(rect)
        self.text_obstacles.append(rect)

    def elbow(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        via_x: float | None = None,
        via_y: float | None = None,
        label: str = "",
        color: str = ARROW,
        dashed: bool = False,
    ) -> None:
        """Right-angled three-segment route through a corridor at `via_x` or `via_y`.

        A curved arrow between non-adjacent shapes passes straight through whatever sits
        between them, and its midpoint label lands on top of another box. Routing out to a
        dedicated corridor and back keeps both the line and its label in clear space.
        """
        if via_x is not None:
            corner_a, corner_b = (via_x, start[1]), (via_x, end[1])
            lx, ly = via_x, (start[1] + end[1]) / 2
        else:
            corner_a, corner_b = (start[0], via_y or 0.0), (end[0], via_y or 0.0)
            lx, ly = (start[0] + end[0]) / 2, via_y or 0.0
        style = (0, (4, 3)) if dashed else "solid"
        self.ax.plot(
            [start[0], corner_a[0], corner_b[0]],
            [start[1], corner_a[1], corner_b[1]],
            color=color,
            linewidth=1.2,
            linestyle=style,
            zorder=1,
            solid_capstyle="round",
        )
        if math.hypot(end[0] - corner_b[0], end[1] - corner_b[1]) > 1.5:
            self.arrow(corner_b, end, color=color, dashed=dashed)
        if label:
            # The corridor is clear of *shapes*, but not of the other routes sharing it: eight
            # loop-backs down one gutter put eight labels at the midpoint of the same line, and
            # the ones underneath were unreadable. So the label slides along its own corridor
            # leg to the first free spot, the same way an arrow label does — and `_edge_label`
            # then claims it, so the next route down the gutter has to go somewhere else.
            rotated = via_x is not None
            leg = (
                ((via_x or 0.0, start[1]), (via_x or 0.0, end[1]))
                if rotated
                else ((start[0], ly), (end[0], ly))
            )
            lx, ly = self._label_point(*leg, label, rad=0.0, label_t=0.5, rotated=rotated)
            self._edge_label(lx, ly, label, color=color, rotated=rotated)

    def header(self, title: str, subtitle: str = "", *, badge: str = "", accent: str = "") -> None:
        """A tinted header band: an accent bar, the title, the view's name, then the scope.

        `_header_height` measured the same scope lines to decide where the body starts and
        `_header_band` where the rule goes, so all three agree by construction — there is no
        constant here for a layout to drift away from.
        """
        band_h = _header_band(subtitle, self.w, title=title, badge=badge)
        self.ax.add_patch(
            Rectangle((0, 0), self.w, band_h, facecolor=PANEL, edgecolor="none", zorder=0)
        )
        # The rule is what stops the last line of prose from reading as a label on the first box.
        self.ax.plot([0, self.w], [band_h, band_h], color=HAIR, linewidth=1.2, zorder=1)
        # A short accent bar in the view's own colour, so the three renders are telling apart
        # at thumbnail size rather than only by reading their titles.
        if accent:
            self.rect(0, 0, 5, band_h, fill=accent, radius=0, z=1)
        # The badge shares the title's *first* line, so that line gets whatever the badge leaves
        # and the rest of the title gets the whole canvas. Both come out of one call to
        # `_title_lines` — a long title on the narrow flow canvas ran straight through "PROCESS
        # FLOW" when the two were placed independently, and was clipped mid-word when it was
        # forced onto one line. It wraps now, and the band and the body have already been
        # measured for however many lines it took.
        title_lines, badge_w = _title_lines(title, self.w, badge=badge)
        y = HEADER_TITLE_Y
        for line in title_lines:
            self.text(
                HEADER_PAD, y, line, size=HEADER_TITLE_SIZE, weight="bold", ha="left"
            )
            y += _line_h(HEADER_TITLE_SIZE)
        if badge and badge_w:
            self.text(
                self.w - HEADER_PAD,
                HEADER_TITLE_Y,
                badge.upper(),
                size=HEADER_BADGE_SIZE,
                color=accent or MUTED,
                weight="bold",
                ha="right",
            )
        y = HEADER_SUB_Y + (len(title_lines) - 1) * _line_h(HEADER_TITLE_SIZE)
        for line in _header_lines(subtitle, self.w):
            self.text(HEADER_PAD, y, line, size=HEADER_SUB_SIZE, color=MUTED, ha="left")
            y += _line_h(HEADER_SUB_SIZE)
        self.ax.add_patch(
            Rectangle(
                (0.5, 0.5),
                self.w - 1,
                self.h - 1,
                facecolor="none",
                edgecolor=HAIR,
                linewidth=1,
                zorder=0,
            )
        )

    def to_png(self) -> tuple[bytes, int, int, int]:
        """PNG bytes, their true pixel size, and the scale used to get there.

        The scale is returned rather than assumed because it can be reduced below
        `RENDER_SCALE` here, and the viewer divides by it to find the reading size.

        Agg refuses any figure over 2^16 pixels on a side. A 60-step process is nowhere near
        that, but the extraction is model-driven and a runaway one must degrade to a coarser
        image rather than fail the whole blueprint, so the scale drops instead.
        """
        scale = RENDER_SCALE
        while scale > 1 and max(self.w, self.h) * scale > 20000:
            scale -= 1
        buf = io.BytesIO()
        self.fig.savefig(buf, format="png", dpi=DPI * scale, facecolor=PAGE)
        self.fig.clf()
        return buf.getvalue(), int(self.w * scale), int(self.h * scale), scale


def _shape_of(kind: str) -> tuple[str, str]:
    return SHAPE_STYLE.get(kind, SHAPE_STYLE["task"])


def _anchors(
    kind: str, x: float, y: float, w: float, h: float, *, pill_inset: float = 22.0
) -> dict[str, tuple[float, float]]:
    """Where an arrow should touch a shape.

    Diamonds are drawn taller than their box and pills narrower, so anchoring on the plain
    rectangle leaves a visible gap on one shape and buries the arrowhead in another.
    """
    cx, cy = x + w / 2, y + h / 2
    if kind == "decision":
        overhang = 7.0
        return {
            "top": (cx, y - overhang),
            "bottom": (cx, y + h + overhang),
            "left": (x, cy),
            "right": (x + w, cy),
        }
    if kind in ("start", "end"):
        return {
            "top": (cx, y),
            "bottom": (cx, y + h),
            "left": (x + pill_inset, cy),
            "right": (x + w - pill_inset, cy),
        }
    return {"top": (cx, y), "bottom": (cx, y + h), "left": (x, cy), "right": (x + w, cy)}


# ── SIPOC ───────────────────────────────────────────────────────────────────────


def render_sipoc(
    sipoc: SipocDiagram, *, scope: str = "", records: Iterable[str] = ()
) -> tuple[bytes, int, int, int]:
    """A five-column table. Deliberately a table: the value is in reading across the row."""
    columns = [
        ("Suppliers", sipoc.suppliers),
        ("Inputs", sipoc.inputs),
        ("Process", sipoc.process),
        ("Outputs", sipoc.outputs),
        ("Customers", sipoc.customers),
    ]
    col_w, gap, pad = 244.0, 18.0, 12.0
    left = 28.0
    head_h = 34.0
    name_size, note_size = 9.4, 8.0

    # Wrap every cell first — the tallest column sets the canvas height.
    cells: list[list[tuple[str, list[str], list[str], float]]] = []
    for ci, (_, items) in enumerate(columns):
        column: list[tuple[str, list[str], list[str], float]] = []
        for i, item in enumerate(items):
            # The process column is a sequence, not a set: numbering it is what tells the
            # reader these five cells are in order while the other four columns are not.
            num = f"{i + 1}" if ci == 2 else ""
            indent = pad + (16.0 if num else 0.0)
            name = _wrap(item.name, col_w - pad - indent, name_size, "bold", max_lines=3)
            # Four lines, not two. A note is the parenthetical that says what the row actually
            # is — "document name, upload date, uploaded-by, status, data lineage" — and two
            # lines cut half of those away, so the cell named a record without saying what is
            # in it. The cell's height is computed from `len(note)` two lines down and the
            # tallest column sets the canvas, so a longer note grows the picture, not an "…".
            note = _wrap(item.note, col_w - pad - indent, note_size, max_lines=4)
            h = pad * 2 + len(name) * _line_h(name_size) + (len(note) * _line_h(note_size) if note else 0)
            column.append((num, name, note, max(48.0, h)))
        cells.append(column)

    body_h = max((sum(c[3] + 10 for c in col) for col in cells), default=60.0)
    width = left * 2 + col_w * 5 + gap * 4
    # The scope can wrap, and so can the title, so the table starts below both rather than at a
    # fixed 96px. The same title and badge go to `header` below — measure with what you draw.
    top = _header_height(scope, width, title=sipoc.title or "SIPOC", badge="SIPOC")

    # Measures and records go under the table rather than in it: they describe the whole
    # process, so hanging them off one column would say something untrue about that column.
    bands: list[tuple[str, list[str]]] = []
    # Hang the values off the widest label rather than a guessed indent — "Measures:" is wider
    # than it looks at 8.8pt bold, and the two ran into each other.
    band_gap = max(_measure("Measures:", 8.8, "bold"), _measure("Records:", 8.8, "bold")) + 12
    band_wrap = width - 2 * left - 24 - band_gap
    # Six lines each, not two. These are `·`-joined lists — every measure the process is judged
    # by, every record it produces — so a two-line bound did not shorten a sentence, it deleted
    # list items: the SIPOC's Records band stopped at "AI-generated answer text…" with three
    # more records behind the ellipsis. `bands_h` below is computed from `len(rows)`, so the
    # canvas grows by exactly what these take.
    if sipoc.metrics:
        bands.append(("Measures", _wrap("   ·   ".join(sipoc.metrics), band_wrap, 8.8, max_lines=6)))
    kept = [r.strip() for r in records if r and r.strip()]
    if kept:
        bands.append(("Records", _wrap("   ·   ".join(kept), band_wrap, 8.8, max_lines=6)))
    bands_h = (sum(len(rows) * _line_h(8.8) + 12 for _, rows in bands) + 14) if bands else 0.0
    height = top + head_h + body_h + 26 + bands_h + 24

    c = _Canvas(width, height)
    c.header(sipoc.title or "SIPOC", scope, badge="SIPOC", accent=VIEW_ACCENT["sipoc"])

    for idx, (heading, _) in enumerate(columns):
        x = left + idx * (col_w + gap)
        tint, accent = SIPOC_COLORS[idx]
        c.rect(x, top, col_w, head_h, fill=accent, radius=4, z=2)
        c.text(x + col_w / 2, top + head_h / 2, heading.upper(), size=10, color="#ffffff", weight="bold")
        # Column body panel, so an empty tail still reads as part of the table.
        c.rect(x, top + head_h + 6, col_w, body_h, fill=tint, edge=HAIR, radius=4, z=1)

        y = top + head_h + 16
        for num, name, note, h in cells[idx]:
            c.rect(x + 8, y, col_w - 16, h - 8, fill=PAGE, edge=HAIR, radius=3, z=2)
            block_h = len(name) * _line_h(name_size) + (len(note) * _line_h(note_size) if note else 0)
            cursor = y + (h - 8 - block_h) / 2 + _line_h(name_size) / 2
            centre = x + col_w / 2 + (8.0 if num else 0.0)
            if num:
                c.rect(x + 14, y + (h - 8) / 2 - 8, 17, 16, fill=tint, edge=accent, radius=3, z=3)
                c.text(x + 22.5, y + (h - 8) / 2, num, size=7.6, color=accent, weight="bold", z=4)
            for row in name:
                c.text(centre, cursor, row, size=name_size, weight="bold")
                cursor += _line_h(name_size)
            for row in note:
                c.text(centre, cursor, row, size=note_size, color=MUTED, style="italic")
                cursor += _line_h(note_size)
            y += h + 2

        if idx < 4:
            c.text(x + col_w + gap / 2, top + head_h / 2, "▶", size=10, color=HAIR)

    if bands:
        by = top + head_h + body_h + 20
        c.rect(left, by, width - 2 * left, bands_h - 6, fill=PANEL, edge=HAIR, radius=4, z=1)
        cursor = by + 15
        for name, rows in bands:
            c.text(left + 14, cursor, f"{name}:", size=8.8, color=INK, weight="bold", ha="left")
            for row in rows:
                c.text(left + 14 + band_gap, cursor, row, size=8.8, color=MUTED, ha="left")
                cursor += _line_h(8.8)
            cursor += 12

    return c.to_png()


# ── Process flow ────────────────────────────────────────────────────────────────


def _stack(
    c: _Canvas,
    cx: float,
    cy: float,
    blocks: list[tuple[list[str], float, str, str, str]],
) -> None:
    """Draw several wrapped blocks as one vertically centred stack.

    A step box holds a name, a line of detail and the system that runs it. Centring each of
    them independently on the box is how they end up on top of each other; the whole stack
    has to be centred as one thing.
    """
    rows_total = sum(len(rows) * _line_h(size) for rows, size, _, _, _ in blocks)
    cursor = cy - rows_total / 2
    for rows, size, weight, color, style in blocks:
        for row in rows:
            c.text(cx, cursor + _line_h(size) / 2, row, size=size, color=color, weight=weight, style=style)
            cursor += _line_h(size)


def _layers(node_ids: list[str], edges: Iterable[ProcessEdge]) -> dict[str, int]:
    """Longest-path depth using only forward edges, so a loop-back cannot hang this."""
    rank = {nid: i for i, nid in enumerate(node_ids)}
    depth = {nid: 0 for nid in node_ids}
    forward: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in edges:
        if e.source in rank and e.target in rank and rank[e.target] > rank[e.source]:
            forward[e.source].append(e.target)
    for nid in node_ids:  # `node_ids` order is topological for forward edges by definition
        for nxt in forward[nid]:
            depth[nxt] = max(depth[nxt], depth[nid] + 1)
    return depth


def render_flow(flow: FlowDiagram, *, scope: str = "") -> tuple[bytes, int, int, int]:
    """Top-down layered flowchart with labelled branches and side-routed loop-backs."""
    # The vertical gap carries both the branch labels and the next layer's actor captions, so
    # it is wider than the horizontal one — the two used to land on top of each other.
    #
    # 108 rather than 84, because there are now *three* things stacked in it: a decision's
    # systems caption under the diamond, the branch label on the arrow, and the next row's step
    # id and actor. At 84 they shared 32px, a two-line branch label needs 31 of those, and the
    # corridor left over was half a pixel short of clear — so "action = upload new document" was
    # drawn straight through "Document Management Service" and neither could be read. The page
    # is a little taller; both labels are legible, which is the whole point of drawing them.
    node_w, gap_x, gap_y = 248.0, 44.0, 108.0
    left = 28.0
    label_size, detail_size = 9.4, 7.4
    #: Lines of `node.detail` a step may carry. Three, because a rule worth drawing is usually
    #: "status moves to Active · audit entry written · retried twice", and two lines cut the last
    #: clause off the very thing that makes the step checkable.
    #: Six, once that was measured against a real project rather than an example: the details the
    #: prompt actually produces run to 180–240 characters ("Immutable audit entry written:
    #: action=Document Uploaded, user identity, document name, timestamp, document status.
    #: Satisfies BR-05 and…"), which is five to six lines at this width — so three lines cut every
    #: step on the flow, and always in the sentence naming the requirement it satisfies. The box
    #: height is measured from the content, so the cost is a taller page and nothing else.
    #: Eight after measuring every step of a real plan rather than the two longest: the widest
    #: detail needs seven lines at this width, and six still cut it — "Response target: 3–5…"
    #: dropped the unit off a number, which is worse than no number. Eight is that measurement
    #: plus one line of headroom, so a slightly wordier sentence does not reopen the bug.
    detail_max_lines = 8
    #: Lines of a step's own label. Four, because a decision is phrased as a question and
    #: `Execute lifecycle action (preview/delete/restore)?` needs four lines inside a diamond,
    #: which is 56% of the node width — at three it was drawn as "(preview/delete/…".
    label_max_lines = 4
    #: Lines of the systems caption under or inside a step. Three: it is a *list* of services, so
    #: a cut here loses a service name outright — at one line `Search History Service, Audit Log
    #: Service` became `Search History Service, Audit Log…`, and at two `Document Management
    #: Service, NLP / OCR Provider` still lost the provider. Three holds both names of every pair
    #: this layout draws, at the narrowest of the two widths the caption is wrapped to.
    caption_max_lines = 3
    caption_size = 7.6

    ids = [n.id for n in flow.nodes]
    depth = _layers(ids, flow.edges)
    rows: dict[int, list[str]] = {}
    for n in flow.nodes:
        rows.setdefault(depth[n.id], []).append(n.id)

    order = sorted(rows)
    widest = max((len(rows[r]) for r in order), default=1)
    span = widest * node_w + (widest - 1) * gap_x

    # One box height for the whole picture, measured from the tallest step rather than fixed at
    # 96px. The fixed height was what forced the detail line to be squeezed into "however many
    # lines happen to be left", so on a step with a three-line label the rule under it was
    # dropped entirely and on a two-line label it was cut to one. Now the content decides the
    # height, every row still lines up as a grid, and `lines_fit` below can always be satisfied.
    node_h = 96.0
    for n in flow.nodes:
        if n.kind == "decision":
            inner = node_w * 0.56
        elif n.kind in ("start", "end"):
            inner = node_w - 74
        else:
            inner = node_w - 26
        need = 14.0 + len(
            _wrap(n.label, inner, label_size, "bold", max_lines=label_max_lines)
        ) * _line_h(label_size)
        if n.detail and n.kind in ("task", "subprocess"):
            need += len(_wrap(n.detail, inner, detail_size, max_lines=detail_max_lines)) * _line_h(
                detail_size
            )
        if n.systems:
            # Measured, not a flat 14: the caption may take `caption_max_lines`, and a box sized
            # for one of them squeezed `lines_fit` below into dropping a line of the detail.
            need += len(
                _wrap(
                    ", ".join(n.systems[:2]), inner, caption_size, max_lines=caption_max_lines
                )
            ) * _line_h(caption_size)
        node_h = max(node_h, need)

    # Corridors down each side carry the edges that cannot go straight: skips forward on the
    # left, loop-backs on the right. Sized to the number of each so two never overlap.
    present = {n.id for n in flow.nodes}
    skips = [
        e
        for e in flow.edges
        if e.source in present and e.target in present and depth[e.target] - depth[e.source] > 1
    ]
    loops = [
        e
        for e in flow.edges
        if e.source in present and e.target in present and depth[e.target] <= depth[e.source]
    ]
    # A loop-back leaves on whichever side its source sits nearer, so it does not have to
    # travel behind the sibling steps sharing its layer. Position within the layer is known
    # before any coordinate is, which is why this is decided here.
    slot: dict[str, tuple[int, int]] = {}
    for layer, members in rows.items():
        for i, nid in enumerate(members):
            slot[nid] = (i, len(members))

    def _exits_left(edge: ProcessEdge) -> bool:
        index, count = slot[edge.source]
        return count > 1 and index < count / 2

    left_loops = [e for e in loops if _exits_left(e)]
    right_loops = [e for e in loops if not _exits_left(e)]

    lane_left = (20.0 + 14.0 * (len(skips) + len(left_loops))) if (skips or left_loops) else 0.0
    lane_right = (20.0 + 14.0 * len(right_loops)) if right_loops else 0.0

    legend_h = 34.0
    width = left * 2 + span + lane_left + lane_right
    top = _header_height(
        scope,
        width,
        gap=HEADER_GAP + FLOW_CAPTION_BAND,
        title=flow.title or "Process flow",
        badge="Process flow",
    )
    height = top + len(order) * (node_h + gap_y) + legend_h + 30

    c = _Canvas(width, height)
    c.header(flow.title or "Process flow", scope, badge="Process flow", accent=VIEW_ACCENT["flow"])

    grid_left = left + lane_left
    box: dict[str, tuple[float, float, float, float]] = {}
    by_id = {n.id: n for n in flow.nodes}
    for li, layer in enumerate(order):
        members = rows[layer]
        layer_span = len(members) * node_w + (len(members) - 1) * gap_x
        x0 = grid_left + (span - layer_span) / 2
        y = top + li * (node_h + gap_y)
        for mi, nid in enumerate(members):
            x = x0 + mi * (node_w + gap_x)
            box[nid] = (x, y, node_w, node_h)
            node = by_id[nid]
            fill, edge = _shape_of(node.kind)
            cx, cy = x + node_w / 2, y + node_h / 2

            shape_h = node_h + 14
            if node.kind == "decision":
                inner = node_w * 0.56
                # A diamond's usable width shrinks toward its points, so at the fixed height a
                # third line of text runs out through the lower edge. Give it the room instead
                # of truncating the question — the branch labels only make sense against it.
                lines = len(_wrap(node.label, inner, label_size, "bold", max_lines=label_max_lines))
                shape_h += 18 * max(0, lines - 2)
                c.diamond(cx, cy, node_w, shape_h, fill=fill, edge=edge)
            elif node.kind in ("start", "end"):
                c.rect(x + 22, y, node_w - 44, node_h, fill=fill, edge=edge, lw=1.4, radius=node_h / 2, z=2)
                inner = node_w - 74
            else:
                lw = 2.0 if node.kind == "subprocess" else 1.2
                c.rect(x, y, node_w, node_h, fill=fill, edge=edge, lw=lw, radius=6, z=2)
                inner = node_w - 26

            caption = ", ".join(node.systems[:2])
            # A diamond narrows toward its point, so its caption goes underneath rather than
            # inside — in the box it landed on the outline and read as a smudge on the border.
            below = node.kind == "decision"
            # Wrapped against the width it is drawn *at*: inside the box when it goes inside,
            # against the whole node when it goes under a diamond, where there is more room.
            cap_rows = (
                _wrap(
                    caption,
                    node_w if below else inner,
                    caption_size,
                    max_lines=caption_max_lines,
                )
                if caption
                else []
            )
            rows_txt = _wrap(node.label, inner, label_size, "bold", max_lines=label_max_lines)
            blocks: list[tuple[list[str], float, str, str, str]] = [
                (rows_txt, label_size, "bold", INK, "normal")
            ]
            # The rule the step applies, inside the box. A flowchart of twenty-five verb
            # phrases is a picture of a process nobody can check; the threshold, the field or
            # the status transition is the part a reviewer can say "no, wrong" to. Only the
            # rectangles get it: a diamond has no room and a pill has nothing to say.
            if node.detail and node.kind in ("task", "subprocess"):
                room = (
                    node_h
                    - 14
                    - len(rows_txt) * _line_h(label_size)
                    - (0 if below else len(cap_rows) * _line_h(caption_size))
                )
                # A hairline tolerance, not a fudge. `room` is a float sum of float line
                # heights, so the very node whose detail *set* `node_h` came back with room
                # for 5.999999999999999 lines of the six it had been measured to need — and
                # floor division then dropped the sixth, cutting the sentence that decided
                # the height of every box on the page. This only ever recovers a line the
                # height pass has already paid for.
                lines_fit = int(room / _line_h(detail_size) + 1e-6)
                if lines_fit >= 1:
                    detail_rows = _wrap(
                        node.detail, inner, detail_size, max_lines=min(detail_max_lines, lines_fit)
                    )
                    blocks.append((detail_rows, detail_size, "normal", MUTED, "normal"))
            if cap_rows and not below:
                blocks.append((cap_rows, caption_size, "normal", MUTED, "italic"))
            _stack(c, cx, cy, blocks)
            if cap_rows and below:
                # `lines` centres the block on the y it is given, so the centre of a caption
                # that starts at `cap_y` is half a line lower for every row past the first.
                # Both calls take that same centre: reserving `cap_y` instead left the last
                # half-line of a two-row caption outside its own reservation, which is exactly
                # where the branch label then went — "Provider" was drawn through "format
                # valid and".
                cap_y = cy + shape_h / 2 + 9 + (len(cap_rows) - 1) * _line_h(caption_size) / 2
                c.lines(cx, cap_y, cap_rows, size=caption_size, color=MUTED, halo=True, z=6)
                # Under a diamond is exactly where its branch labels want to go, so claim
                # the space now — an edge label placed there would blank out the systems.
                c.reserve(cx, cap_y, "\n".join(cap_rows))
            # The step id and its actor sit above the box — the id so a reader can carry a step
            # from here to the swimlane and back without counting boxes.
            #
            # They are split to opposite corners rather than joined into one caption, because
            # the incoming arrow lands on the middle of the top edge and a caption's plate is
            # opaque: centred it hid the whole arrowhead, and left-aligned it still clipped the
            # tip once the actor name was long. The corners are the only space that is always
            # free, and the gap between them is the arrow's corridor.
            cap_y = y - 16
            if nid:
                c.text(x + 4, cap_y, nid, size=7.6, color=MUTED, ha="left", halo=True, z=6)
                c.reserve(x + 4 + _measure(nid, 7.6) / 2, cap_y, nid)
            # Right-aligned in whatever the step id leaves of the top edge.
            actor = _clip(
                node.actor, max(70.0, node_w - 16 - (_measure(nid, 7.6) if nid else 0.0)), 7.6
            )
            if actor:
                c.text(
                    x + node_w - 4, cap_y, actor, size=7.6, color=MUTED, ha="right", halo=True, z=6
                )
                c.reserve(x + node_w - 4 - _measure(actor, 7.6) / 2, cap_y, actor)

    # Every shape is placed, so labels can now be kept off them. Extend rather than assign:
    # the captions drawn above have already claimed their own space.
    c.obstacles.extend(box.values())

    skip_i = loop_i = 0
    for e in flow.edges:
        if e.source not in box or e.target not in box:
            continue
        src = _anchors(by_id[e.source].kind, *box[e.source])
        dst = _anchors(by_id[e.target].kind, *box[e.target])
        gap = depth[e.target] - depth[e.source]
        label = _edge_caption(e.label)
        if gap == 1:
            start, end = src["bottom"], (dst["top"][0], dst["top"][1] - 4)
            rad = 0.0 if abs(start[0] - end[0]) < 6 else 0.12
            # The step-to-next-step path is drawn heaviest: it is the spine of the process and
            # should be the line the eye follows before it notices any exception.
            c.arrow((start[0], start[1] + 2), end, label=label, rad=rad, lw=1.5)
        elif gap > 1:
            # Skips a layer: down the left corridor rather than over the boxes in between.
            c.elbow(
                src["left"],
                (dst["left"][0] - 4, dst["left"][1]),
                via_x=grid_left - 8 - 14 * skip_i,
                label=label,
                color=ARROW_SOFT,
            )
            skip_i += 1
        elif _exits_left(e):
            c.elbow(
                src["left"],
                (dst["left"][0] - 4, dst["left"][1]),
                via_x=grid_left - 8 - 14 * skip_i,
                label=label,
                color=LOOP,
                dashed=True,
            )
            skip_i += 1
        else:
            # Loop-back: out a side corridor, so it never crosses a shape it does not touch.
            c.elbow(
                src["right"],
                (dst["right"][0] + 4, dst["right"][1]),
                via_x=grid_left + span + 8 + 14 * loop_i,
                label=label,
                color=LOOP,
                dashed=True,
            )
            loop_i += 1

    strokes: list[tuple[str, bool, str]] = [(ARROW, False, "Next step")]
    if skips:
        strokes.append((ARROW_SOFT, False, "Skips a step"))
    if loops:
        strokes.append((LOOP, True, "Loop back / rework"))
    c.legend(
        left,
        height - legend_h - 12,
        width - 2 * left,
        shapes=[(k, n) for k, n in LEGEND_SHAPES if any(nd.kind == k for nd in flow.nodes)],
        strokes=strokes,
        height=legend_h,
    )
    return c.to_png()


# ── Swimlane ────────────────────────────────────────────────────────────────────


def swimlane_route(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    channel_x1: float,
    channel_x2: float,
    channel_y: float | None,
) -> list[tuple[float, float]]:
    """The corners of one swimlane handoff, given the channels it has been assigned.

    Split out from the drawing so the geometry can be asserted against the box rectangles in
    a test: "no route crosses a box it does not touch" is the whole point of the routing, and
    it is not something you can see in a PNG assertion.

    Both stubs are horizontal — out of the source's side into the gutter beside it, and into
    the target's side from the gutter beside that — so the arrowhead always lands square on
    the box. When the two gutters are the same one, the route collapses to a Z rather than
    doubling back through it.
    """
    if channel_y is None or abs(channel_x2 - channel_x1) < 1.0:
        return [start, (channel_x1, start[1]), (channel_x1, end[1]), end]
    return [
        start,
        (channel_x1, start[1]),
        (channel_x1, channel_y),
        (channel_x2, channel_y),
        (channel_x2, end[1]),
        end,
    ]


def render_swimlane(
    swim: SwimlaneDiagram,
    *,
    actor_kinds: dict[str, str] | None = None,
    actor_notes: dict[str, str] | None = None,
    scope: str = "",
) -> tuple[bytes, int, int, int]:
    """Lanes down the left, stages across the top, handoffs routed through the empty channels.

    The handoffs are the reason this view exists, and drawing thirty of them as curves between
    box centres produces a picture where every arrow crosses six others and none can be
    followed. So the layout leaves deliberate empty space — a gutter down each column boundary
    and padding above and below each row of boxes — and every non-adjacent handoff travels in
    it at right angles. Nothing is hidden and nothing overlaps a box it does not connect.
    """
    actor_kinds = actor_kinds or {}
    actor_notes = actor_notes or {}
    lane_w, col_w = 206.0, 254.0
    # The two numbers the routing depends on: `gutter` is the empty width on each column
    # boundary that vertical runs travel down, `box_pad` the empty height above and below each
    # row of boxes that horizontal runs travel across.
    gutter, box_pad = 36.0, 32.0
    left = 28.0
    legend_h = 34.0
    label_size = 9.2
    #: Lines a step label may take. Four rather than three: these are the same labels the flow
    #: view draws, but in a column 60px narrower, so the view that had least room was the one
    #: cutting them.
    label_max_lines = 4
    #: Lines of an actor's responsibilities in the lane header. Six rather than three: what a
    #: lane is accountable for is written as a list ("Uploads, previews, deletes, and restores
    #: documents; manages user roles; reviews system-wide search history, analytics, and audit
    #: logs; controls document searchability"), which runs to 110–170 characters and needs five
    #: lines in this column — so three cut every lane, and the lane header is the one caption on
    #: the page that says what the row of boxes beside it is *for*. The lane grows to hold it.
    note_max_lines = 6

    lanes = swim.lanes or sorted({s.lane for s in swim.steps})
    columns = sorted({s.column for s in swim.steps}) or [0]
    col_index = {col: i for i, col in enumerate(columns)}
    lane_row = {lane: i for i, lane in enumerate(lanes)}

    # Box height and stage-band height both measured from the text, not fixed. A step label is
    # written for the process, not for a 74px box, and "Validate document format, readability
    # and run OCR if needed" needs four lines in a 218px column — at the old fixed height it
    # arrived as three lines and an "…", in the one view whose whole job is showing who does
    # what. Every box still shares one height, so the lanes stay a grid.
    step_inner = {"decision": (col_w - gutter) * 0.58, "start": col_w - gutter - 58,
                  "end": col_w - gutter - 58}
    box_h = 74.0
    for s in swim.steps:
        inner = step_inner.get(s.kind, col_w - gutter - 30)
        rows_needed = len(_wrap(s.label, inner, label_size, "bold", max_lines=label_max_lines))
        box_h = max(box_h, 22.0 + rows_needed * _line_h(label_size))

    # Two lines for a stage name, and the band grows to hold them: a phase is often called
    # "Document ingestion & validation", which does not fit one line of a 248px column.
    stage_rows = [
        _wrap(swim.stages[i] if i < len(swim.stages) else "", col_w - 22, 8.8, "bold", max_lines=2)
        for i in range(len(columns))
    ]
    band_h = 22.0 + max((len(r) for r in stage_rows), default=1) * _line_h(8.8) + 8.0

    # Steps sharing a lane and a column stack, so that lane has to grow.
    grouped: dict[tuple[str, int], list] = {}
    for s in swim.steps:
        grouped.setdefault((s.lane, s.column), []).append(s)
    # The lane header's own stack — name, kind, responsibilities — measured here, so the lane is
    # tall enough for it. It used to be drawn only `if stack_h + 16 <= h`, which meant a lane
    # holding one box silently lost its responsibilities altogether; growing the lane is the
    # honest version of that check, and a lane is a horizontal band so the cost is page height.
    lane_name_rows: dict[str, list[str]] = {}
    lane_note_rows: dict[str, list[str]] = {}
    lane_head_h: dict[str, float] = {}
    for lane in lanes:
        note = actor_notes.get(lane, "")
        lane_name_rows[lane] = _wrap(lane, lane_w - 34, 9.6, "bold", max_lines=3)
        lane_note_rows[lane] = (
            _wrap(note, lane_w - 34, 7.2, max_lines=note_max_lines) if note else []
        )
        lane_head_h[lane] = (
            len(lane_name_rows[lane]) * _line_h(9.6)
            + (_line_h(7.4) if actor_kinds.get(lane) else 0.0)
            + len(lane_note_rows[lane]) * _line_h(7.2)
            + 16.0
        )
    lane_h: dict[str, float] = {}
    for lane in lanes:
        stack = max((len(v) for (ln, _), v in grouped.items() if ln == lane), default=1)
        lane_h[lane] = max(
            max(1, stack) * (box_h + box_pad) + box_pad, lane_head_h[lane] + box_pad
        )

    width = left * 2 + lane_w + len(columns) * col_w
    top = _header_height(scope, width, title=swim.title or "Swimlane", badge="Swimlane")
    height = top + band_h + sum(lane_h.values()) + legend_h + 34

    c = _Canvas(width, height)
    c.header(swim.title or "Swimlane", scope, badge="Swimlane", accent=VIEW_ACCENT["swimlane"])

    grid_x = left + lane_w
    for i, col in enumerate(columns):
        x = grid_x + i * col_w
        band_w = col_w - 6
        c.rect(x, top, band_w, band_h, fill=PANEL, edge=HAIR, radius=4, z=1)
        c.text(x + band_w / 2, top + 13, f"STAGE {i + 1}", size=7.0, color=MUTED, weight="bold")
        # The phase name, not just the number: "Stage 3" is what the reader can already see,
        # "Adjudication" is what that column of boxes is for. Wrapped above, where the band's
        # own height was measured from it, so a two-line name has the room it needs.
        cursor = top + 28
        for row in stage_rows[i]:
            c.text(x + band_w / 2, cursor, row, size=8.8, weight="bold")
            cursor += _line_h(8.8)

    lane_y: dict[str, float] = {}
    y = top + band_h
    for lane in lanes:
        h = lane_h[lane]
        lane_y[lane] = y
        tint = LANE_TINT.get(actor_kinds.get(lane, "human"), LANE_TINT["human"])
        c.rect(left, y, lane_w - 6, h - 4, fill=tint, edge=HAIR, radius=4, z=1)
        c.rect(grid_x, y, width - left - grid_x, h - 4, fill=tint, edge=HAIR, radius=0, z=0)
        # Name, kind and duties as one centred stack: who this lane is, and what they are
        # accountable for, which is the question a reader asks the moment they see the name.
        blocks: list[tuple[list[str], float, str, str, str]] = [
            (lane_name_rows[lane], 9.6, "bold", INK, "normal")
        ]
        kind = actor_kinds.get(lane)
        if kind:
            blocks.append(([kind], 7.4, "normal", MUTED, "normal"))
        # Wrapped and measured above, where `lane_h` was set from it: the lane is sized to hold
        # this stack, so there is no longer a case where the responsibilities are dropped.
        if lane_note_rows[lane]:
            blocks.append((lane_note_rows[lane], 7.2, "normal", MUTED, "italic"))
        _stack(c, left + (lane_w - 6) / 2, y + (h - 4) / 2, blocks)
        y += h

    box: dict[str, tuple[float, float, float, float]] = {}
    for (lane, col), stack in grouped.items():
        if lane not in lane_y or col not in col_index:
            continue
        x = grid_x + col_index[col] * col_w + gutter / 2
        w = col_w - gutter
        y0 = lane_y[lane] + (lane_h[lane] - len(stack) * (box_h + box_pad) + box_pad) / 2
        for si, step in enumerate(stack):
            sy = y0 + si * (box_h + box_pad)
            box[step.id] = (x, sy, w, box_h)
            fill, edge = _shape_of(step.kind)
            cx, cy = x + w / 2, sy + box_h / 2
            if step.kind == "decision":
                inner = w * 0.58
                # Same as the flow view: a three-line question needs a taller diamond, or its
                # last line crosses the lower edge. The lane's padding absorbs the extra.
                lines = len(_wrap(step.label, inner, label_size, "bold", max_lines=label_max_lines))
                c.diamond(cx, cy, w, box_h + 10 + 16 * max(0, lines - 2), fill=fill, edge=edge)
            elif step.kind in ("start", "end"):
                c.rect(x + 14, sy, w - 28, box_h, fill=fill, edge=edge, lw=1.4, radius=box_h / 2, z=2)
                inner = w - 58
            else:
                lw = 2.0 if step.kind == "subprocess" else 1.2
                c.rect(x, sy, w, box_h, fill=fill, edge=edge, lw=lw, radius=6, z=2)
                inner = w - 30
            c.lines(
                cx,
                cy,
                _wrap(step.label, inner, label_size, "bold", max_lines=label_max_lines),
                size=label_size,
                weight="bold",
            )
            # The step id, so a box here can be matched with the same box in the flow view.
            c.text(
                x + 5,
                sy + 10,
                step.id,
                size=6.8,
                color=MUTED,
                ha="left",
                halo=step.kind not in ("task", "subprocess"),
                z=5,
            )

    grid_bottom = top + band_h + sum(lane_h.values())
    # A handoff label must not land on a box it has nothing to do with; keep them off.
    # Registered after the boxes and before the edges, when all of both are known.
    c.obstacles.extend(box.values())
    step_by_id = {s.id: s for s in swim.steps}

    # Which slot in each channel is next. Parallel runs in one channel would otherwise be
    # drawn on top of each other, and two handoffs sharing a line is one unreadable handoff.
    v_used: dict[int, int] = {}
    h_used: dict[tuple[int, bool], int] = {}

    def channel_x(gap: int) -> float:
        """A vertical channel in the gutter on the right-hand boundary of column `gap`."""
        n = v_used.get(gap, 0)
        v_used[gap] = n + 1
        return grid_x + (gap + 1) * col_w + _V_OFFSETS[n % len(_V_OFFSETS)]

    def channel_y(row: int, above: bool) -> float:
        """A horizontal channel just inside a lane boundary, in the boxes' vertical padding."""
        key = (row, above)
        n = h_used.get(key, 0)
        h_used[key] = n + 1
        lane = lanes[row]
        base = lane_y[lane] if above else lane_y[lane] + lane_h[lane]
        return min(max(base + _H_OFFSETS[n % len(_H_OFFSETS)], top + band_h + 8), grid_bottom - 8)

    skipped = looped = False
    for e in swim.edges:
        if e.source not in box or e.target not in box:
            continue
        s, t = step_by_id[e.source], step_by_id[e.target]
        src = _anchors(s.kind, *box[e.source], pill_inset=14)
        dst = _anchors(t.kind, *box[e.target], pill_inset=14)
        si, ti = col_index[s.column], col_index[t.column]
        srow, trow = lane_row.get(s.lane, 0), lane_row.get(t.lane, 0)
        label = _edge_caption(e.label)

        if si == ti:
            # Same stage, so the handoff is vertical: leave the bottom, enter the top.
            downward = src["bottom"][1] < dst["top"][1]
            start = src["bottom"] if downward else src["top"]
            end = (
                (dst["top"][0], dst["top"][1] - 4)
                if downward
                else (dst["bottom"][0], dst["bottom"][1] + 4)
            )
            blocker = next(
                (
                    True
                    for sid, (bx, by, bw, bh) in box.items()
                    if sid not in (e.source, e.target)
                    and bx - 2 <= start[0] <= bx + bw + 2
                    and by < max(start[1], end[1])
                    and by + bh > min(start[1], end[1])
                ),
                False,
            )
            if not blocker:
                c.arrow(start, end, label=label, lw=1.4)
                continue
            # A third box sits between these two in the same column, so the straight line
            # would be drawn straight through it. Out to the gutter and back instead, on
            # whichever side has one — entering the target on the same side it left from.
            right = si < len(columns) - 1
            gap = si if right else si - 1
            if gap < 0:
                c.arrow(start, end, label=label, lw=1.4)
                continue
            x1 = channel_x(gap)
            side = "right" if right else "left"
            a = src[side]
            b = (dst[side][0] + (5 if right else -5), dst[side][1])
            c.route([a, (x1, a[1]), (x1, b[1]), b], label=label, lw=1.4)
            continue

        forward = ti > si
        spine = forward and ti == si + 1
        # Weight and colour carry the hierarchy: the stage-to-next-stage path is the process,
        # a stage skip is an exception, and a backward edge is rework.
        color = ARROW if spine else (ARROW_SOFT if forward else LOOP)
        lw = 1.5 if spine else 1.2
        if forward:
            start, end = src["right"], (dst["left"][0] - 5, dst["left"][1])
            out_gap, in_gap = si, ti - 1
            skipped = skipped or not spine
        else:
            # Backwards: out of the source's left, into the target's right, so the direction
            # of travel is unmistakable even where two routes run side by side.
            start, end = src["left"], (dst["right"][0] + 5, dst["right"][1])
            out_gap, in_gap = si - 1, ti
            looped = True
        x1 = channel_x(out_gap)
        x2 = x1 if in_gap == out_gap else channel_x(in_gap)
        cy_channel: float | None = None
        if abs(x2 - x1) >= 1.0:
            # Cross the diagram along the boundary of the *target's* lane: the run then ends
            # next to the box it is entering, which is where the eye is going anyway.
            above = trow >= srow
            if len(lanes) > 1:
                # The outermost boundaries are the frame of the grid; a route drawn along one
                # reads as part of it, so use the target lane's other side instead.
                if above and trow == 0:
                    above = False
                elif not above and trow == len(lanes) - 1:
                    above = True
            cy_channel = channel_y(trow, above)
        c.route(
            swimlane_route(start, end, channel_x1=x1, channel_x2=x2, channel_y=cy_channel),
            label=label,
            color=color,
            dashed=not forward,
            lw=lw,
        )

    strokes: list[tuple[str, bool, str]] = [(ARROW, False, "Handoff to the next stage")]
    if skipped:
        strokes.append((ARROW_SOFT, False, "Skips a stage"))
    if looped:
        strokes.append((LOOP, True, "Sent back / rework"))
    c.legend(
        left,
        height - legend_h - 14,
        width - 2 * left,
        shapes=[(k, n) for k, n in LEGEND_SHAPES if any(s.kind == k for s in swim.steps)],
        strokes=strokes,
        height=legend_h,
    )
    return c.to_png()


# ── Entry point ─────────────────────────────────────────────────────────────────


def render_all(
    model: ProcessModel,
    sipoc: SipocDiagram,
    flow: FlowDiagram,
    swimlane: SwimlaneDiagram,
    *,
    only: Iterable[str] | None = None,
) -> dict[str, tuple[bytes, int, int, int]]:
    """Render the three views. Keys match `DIAGRAM_KINDS` and the PNG filename stems.

    `only` narrows the work to the named kinds. A single-view follow-up leaves the other two
    views byte-identical, and drawing a 25-step swimlane again to get the same PNG is ten
    seconds of a core for nothing — the caller copies those from the previous version instead.
    """
    wanted = set(only) if only is not None else {"sipoc", "flow", "swimlane"}
    kinds = {a.name: a.kind for a in model.actors}
    notes = {a.name: a.responsibilities for a in model.actors if a.responsibilities}
    scope = model.scope or ""
    out: dict[str, tuple[bytes, int, int, int]] = {}
    if "sipoc" in wanted:
        out["sipoc"] = render_sipoc(sipoc, scope=scope, records=model.data_objects[:8])
    if "flow" in wanted:
        out["flow"] = render_flow(flow, scope=scope)
    if "swimlane" in wanted:
        out["swimlane"] = render_swimlane(
            swimlane, actor_kinds=kinds, actor_notes=notes, scope=scope
        )
    return out
