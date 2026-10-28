"""
`SDD.md` as a PDF or a DOCX, laid out from the same markdown the UI previews.

One parser, two emitters. `parse_markdown` turns the document into a list of neutral blocks —
heading, paragraph, list, table, code, quote, figure — and `markdown_to_pdf` / `markdown_to_docx`
each walk that same list. That is the whole design: "the same markdown format for both pdf and
docx" is then true by construction rather than by two hand-kept-in-sync converters drifting apart
on the third table nobody checked.

The parser mirrors `formatMarkdown` in `wizard.component.ts` feature for feature and in the same
order, because the preview pane is the contract the reader has already seen: frontmatter as a
fenced block, GFM pipe tables with `\\|` escapes and per-column alignment, `#{1,6}` headings with
GitHub-rule anchor slugs, `-`/`*` and `1.` lists, blockquotes, and an inline pass that lifts code
spans out first so a path inside backticks is never rewritten as a link.

Two things a screen does implicitly that paper has to be told:

* **Figures.** SDD §3.2 references its three architectural views by workspace path
  (`docs/architecture/logical-view.png`). The caller passes those bytes in `images`; a path with
  no bytes degrades to the same named placeholder the preview shows, never a broken box.
* **Colour.** The app's palette is a dark theme, and a dark theme on white paper fails contrast
  at every step. The constants below are the same hues re-stepped for paper.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Sequence

logger = logging.getLogger("agentcraft.export")

#: `fmt` values the two emitters answer to, in the order the UI offers them.
DOC_FORMATS: tuple[str, ...] = ("pdf", "docx")

_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def media_type(fmt: str) -> str:
    """The `Content-Type` for a rendered document."""
    try:
        return _MEDIA_TYPES[fmt.lower().lstrip(".")]
    except KeyError as exc:
        raise ValueError(f"Unsupported document format: {fmt}") from exc


# ---------------------------------------------------------------------------
# The block model
#
# Deliberately flat and stringly-typed rather than a class hierarchy: two emitters dispatch on
# `kind` in a single `if` chain each, and a third emitter (HTML, RTF, whatever is asked for next)
# needs to handle only the kinds it cares about.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """A stretch of inline text with the marks that apply to it.

    `image` set means the run is a figure reference rather than text — `text` is then the alt
    text and `image` the source path. Block assembly splits those out (`_text_blocks`), so no
    emitter ever has to draw a picture inside a line of prose.
    """

    text: str
    code: bool = False
    bold: bool = False
    italic: bool = False
    href: str | None = None
    image: str | None = None


@dataclass(frozen=True)
class Block:
    #: heading | para | quote | list | table | code | frontmatter | figure
    kind: str
    runs: tuple[Run, ...] = ()
    #: heading level, 1–6, as written in the markdown.
    level: int = 0
    #: heading anchor, by GitHub's rule — what `[Logical View](#321-logical-view)` needs.
    slug: str = ""
    #: `code` / `frontmatter` payload, verbatim.
    text: str = ""
    ordered: bool = False
    items: tuple[tuple[Run, ...], ...] = ()
    headers: tuple[tuple[Run, ...], ...] = ()
    #: "", "left", "center" or "right" per column, from the `| :--: |` row.
    aligns: tuple[str, ...] = ()
    rows: tuple[tuple[tuple[Run, ...], ...], ...] = ()
    #: `figure` source path and alt text.
    path: str = ""
    alt: str = ""


# ---------------------------------------------------------------------------
# Inline pass
# ---------------------------------------------------------------------------

#: The control character that stands in for a lifted code span. Same reasoning as the UI's
#: `MD_HOLD`: prose is full of " 3 ", so a guessable marker would turn a page count into
#: somebody else's code span.
_HOLD = "\x00"
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")

#: One pass, alternatives in the UI's replace order — image, link, `**`, `__`, `*`, placeholder.
#: Matching in one sweep instead of five sequential rewrites is what lets a mark nest correctly
#: (`**bold with *emphasis* inside**`) without the second pass rewriting the first pass's output.
_TOKEN_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^()\s]+)\)"
    r"|\[(?P<label>[^\]]+)\]\((?P<href>[^()\s]+)\)"
    r"|\*\*(?P<b1>.+?)\*\*"
    r"|__(?P<b2>.+?)__"
    r"|\*(?P<em>[^*\n]+)\*"
    # `_italic_`, but only when the underscores stand alone: `snake_case_name` and
    # `batch_service.py` are everywhere in these documents and none of them is emphasis.
    r"|(?<![A-Za-z0-9_])_(?P<em2>[^_\n]+)_(?![A-Za-z0-9_])"
    r"|\x00(?P<hold>\d+)\x00"
)

_HTTP_RE = re.compile(r"^https?://[^/]", re.I)
_MAILTO_RE = re.compile(r"^mailto:[^@\s]+@[^@\s]+$", re.I)
_ANCHOR_RE = re.compile(r"^#[\w-]+$", re.A)


def _doc_href(raw: str) -> str | None:
    """A URL a document viewer can follow, or None to keep the label as plain text.

    Narrower than the preview's `safeHref` on purpose. The pane also accepts a repo-relative
    path, because there the whole workspace is one click away; in a PDF or a DOCX that has left
    the machine, `docs/architecture/logical-view.png` resolves against nothing, and a link that
    goes nowhere is worse than the same words unlinked. `#anchor` stays — the SDD's table of
    contents is 26 of them, and both emitters write the matching destinations.
    """
    url = (raw or "").strip()
    if not url or url.startswith("//") or re.search(r"[\"'\s<>`\\]", url):
        return None
    if _HTTP_RE.match(url) or _MAILTO_RE.match(url) or _ANCHOR_RE.match(url):
        return url
    return None


def _runs(text: str, spans: Sequence[str], ctx: Run) -> list[Run]:
    """Tokenise `text` into runs, carrying `ctx`'s marks into everything nested inside."""
    out: list[Run] = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.append(replace(ctx, text=text[pos : m.start()]))
        pos = m.end()
        if m.group("src") is not None:
            out.append(replace(ctx, text=m.group("alt") or "", image=m.group("src")))
        elif m.group("href") is not None:
            href = _doc_href(m.group("href"))
            # A refused URL keeps its label, as the preview does — dropping the text as well
            # would silently delete a sentence, which is worse than losing the link.
            inner = replace(ctx, href=href) if href else ctx
            out.extend(_runs(m.group("label"), spans, inner))
        elif m.group("b1") is not None or m.group("b2") is not None:
            body = m.group("b1") if m.group("b1") is not None else m.group("b2")
            out.extend(_runs(body, spans, replace(ctx, bold=True)))
        elif m.group("em") is not None or m.group("em2") is not None:
            body = m.group("em") if m.group("em") is not None else m.group("em2")
            out.extend(_runs(body, spans, replace(ctx, italic=True)))
        else:
            idx = int(m.group("hold"))
            code = spans[idx] if 0 <= idx < len(spans) else ""
            out.append(replace(ctx, text=code, code=True))
    if pos < len(text):
        out.append(replace(ctx, text=text[pos:]))
    return [r for r in out if r.text or r.image]


def inline(text: str) -> tuple[Run, ...]:
    """One line of markdown → its runs. Code spans are lifted out before anything else."""
    spans: list[str] = []

    def hold(m: re.Match[str]) -> str:
        spans.append(m.group(1))
        return f"{_HOLD}{len(spans) - 1}{_HOLD}"

    return tuple(_runs(_CODE_SPAN_RE.sub(hold, text), spans, Run("")))


def plain(runs: Iterable[Run]) -> str:
    """The runs as unmarked text — for a bookmark name or a table-width measurement."""
    return "".join(r.text for r in runs if not r.image)


# ---------------------------------------------------------------------------
# Block pass
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_FENCE_RE = re.compile(r"^\s*```")
#: A thematic break. SDD.md separates its front matter table from §1 with one, and left as a
#: paragraph it reaches the reader as three literal hyphens in the middle of the page. The marks
#: may be spaced out (`* * *`), which is why this is not simply `-{3,}` -- and it is why the rule
#: is tested before the bullet, or that spaced form parses as a one-item list of nothing.
_RULE_RE = re.compile(r"^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
_DELIMITER_RE = re.compile(r"^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$")
_SLUG_DROP_RE = re.compile(r"[^0-9a-zA-Z_\- ]")


def heading_slug(text: str) -> str:
    """A heading's anchor id by GitHub's rule — the rule the document was written against.

    Lowercased, every non-word character dropped, spaces to hyphens, *without* collapsing runs:
    `3.6 AI Guardrails & Data Security` is `36-ai-guardrails--data-security` with two hyphens
    there, because that is the anchor the backend's own table of contents links to.
    """
    return _SLUG_DROP_RE.sub("", text.strip().lower()).replace(" ", "-")


def _table_cells(line: str) -> list[str]:
    """One markdown table row → its cells, honouring `\\|` as a literal pipe.

    Hand-rolled rather than a split on `|`: `_md_table` in `solution_design.py` escapes pipes
    inside cells, and a split that ignored the escape would shift every later column by one.
    """
    row = line.strip()
    cells: list[str] = []
    cur = ""
    i = 0
    while i < len(row):
        ch = row[i]
        if ch == "\\" and i + 1 < len(row) and row[i + 1] == "|":
            cur += "|"
            i += 2
            continue
        if ch == "|":
            cells.append(cur)
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    cells.append(cur)
    # Leading and trailing border pipes produce one empty cell at each end.
    if row.startswith("|"):
        cells.pop(0)
    if row.endswith("|") and cells:
        cells.pop()
    return [c.strip() for c in cells]


def _is_delimiter_row(line: str | None) -> bool:
    """A `| --- | :--: |` separator, which is what makes the line above it a header row."""
    return bool(line) and bool(_DELIMITER_RE.match(line or "")) and "-" in (line or "")


def _align_of(cell: str) -> str:
    if re.fullmatch(r":-+:", cell):
        return "center"
    if re.fullmatch(r"-+:", cell):
        return "right"
    if re.fullmatch(r":-+", cell):
        return "left"
    return ""


def _text_blocks(kind: str, runs: tuple[Run, ...], **extra) -> list[Block]:
    """A text block, split around any figure it contains.

    An image reference is block-level in the preview too (`.md-image` is `display:block`), and
    SDD §3.2 puts each one on its own line, so in practice this yields exactly one figure per
    call — but a caption written on the same line as the image still keeps its words.
    """
    out: list[Block] = []
    pending: list[Run] = []

    def flush() -> None:
        if any(r.text.strip() for r in pending):
            out.append(Block(kind, runs=tuple(pending), **extra))
        pending.clear()

    for run in runs:
        if run.image:
            flush()
            out.append(Block("figure", path=run.image, alt=run.text or run.image))
        else:
            pending.append(run)
    flush()
    return out


def parse_markdown(src: str) -> list[Block]:
    """Markdown → blocks, mirroring `formatMarkdown` in `wizard.component.ts`."""
    raw = (src or "").replace("\r\n", "\n")
    blocks: list[Block] = []

    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            blocks.append(Block("frontmatter", text=raw[3:end].strip()))
            raw = re.sub(r"^\n+", "", raw[end + 4 :])

    lines = raw.split("\n")
    pending_items: list[tuple[Run, ...]] = []
    pending_ordered = False
    in_code = False
    code_buf: list[str] = []

    def flush_list() -> None:
        nonlocal pending_items
        if pending_items:
            blocks.append(Block("list", ordered=pending_ordered, items=tuple(pending_items)))
            pending_items = []

    def flush_code() -> None:
        nonlocal in_code, code_buf
        if in_code:
            blocks.append(Block("code", text="\n".join(code_buf)))
            code_buf = []
            in_code = False

    li = 0
    while li < len(lines):
        line = lines[li]
        if _FENCE_RE.match(line):
            if in_code:
                flush_code()
            else:
                flush_list()
                in_code = True
                code_buf = []
            li += 1
            continue
        if in_code:
            code_buf.append(line)
            li += 1
            continue

        # GFM pipe table — a row followed by a `| --- |` separator. The separator is required,
        # so a lone line that happens to contain pipes stays a paragraph.
        if line.lstrip().startswith("|") and _is_delimiter_row(
            lines[li + 1] if li + 1 < len(lines) else None
        ):
            flush_list()
            headers = tuple(inline(c) for c in _table_cells(line))
            aligns = tuple(_align_of(c) for c in _table_cells(lines[li + 1]))
            rows: list[tuple[tuple[Run, ...], ...]] = []
            li += 2
            while li < len(lines) and lines[li].lstrip().startswith("|"):
                cells = _table_cells(lines[li])
                # Header count wins, as GFM does: a ragged row keeps the columns lined up
                # instead of pushing one cell of one row out past the header.
                rows.append(
                    tuple(
                        inline(cells[i] if i < len(cells) else "") for i in range(len(headers))
                    )
                )
                li += 1
            blocks.append(
                Block("table", headers=headers, aligns=aligns, rows=tuple(rows))
            )
            continue

        # Before the bullet test, or `* * *` is read as a one-item list.
        if _RULE_RE.match(line):
            flush_list()
            blocks.append(Block("rule"))
            li += 1
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            if pending_ordered:
                flush_list()
            pending_ordered = False
            pending_items.append(inline(bullet.group(1)))
            li += 1
            continue
        numbered = _NUMBERED_RE.match(line)
        if numbered:
            if pending_items and not pending_ordered:
                flush_list()
            pending_ordered = True
            pending_items.append(inline(numbered.group(1)))
            li += 1
            continue

        flush_list()
        pending_ordered = False
        if not line.strip():
            li += 1  # blank lines carry no meaning of their own once blocks are separate
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            body = heading.group(2)
            blocks.extend(
                _text_blocks(
                    "heading",
                    inline(body),
                    level=len(heading.group(1)),
                    slug=heading_slug(body),
                )
            )
            li += 1
            continue
        quote = _QUOTE_RE.match(line)
        if quote:
            blocks.extend(_text_blocks("quote", inline(quote.group(1))))
            li += 1
            continue
        blocks.extend(_text_blocks("para", inline(line)))
        li += 1

    flush_list()
    flush_code()
    return blocks


def normalize_image_path(path: str) -> str:
    """`./docs/x.png#frag` → `docs/x.png`, so a reference matches the `images` key."""
    clean = (path or "").split("#")[0].strip().replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    return clean.lstrip("/")


def image_paths(src: str) -> list[str]:
    """Every distinct figure path a document references, in document order.

    The caller uses this to render only the pictures this document actually embeds — drawing
    all six when the SDD names three would cost three matplotlib layouts for nothing.
    """
    seen: dict[str, None] = {}
    for block in parse_markdown(src):
        if block.kind == "figure":
            seen.setdefault(normalize_image_path(block.path), None)
    return list(seen)


# ---------------------------------------------------------------------------
# Shared layout constants
#
# The app's palette re-stepped for paper: same hues, dark enough on white to read. A dark-theme
# ink (`--ink: #e8f0ec`) on a white page is invisible, so this is a translation and not a copy.
# ---------------------------------------------------------------------------

INK = "#12211c"
MUTED = "#5a6d64"
ACCENT = "#177a58"
LINE = "#cfdad6"
CODE_BG = "#f1f5f3"
HEAD_BG = "#e6efeb"

#: Heading point sizes by markdown level. `#` is the document title, and §3.2.1 is a `####`
#: that still has to look like a heading rather than bold body text — hence the shallow taper.
_PDF_HEADING_PT: dict[int, float] = {1: 19.0, 2: 14.5, 3: 12.0, 4: 10.6, 5: 10.0, 6: 9.6}
_DOCX_HEADING_PT: dict[int, float] = {1: 20.0, 2: 15.0, 3: 12.5, 4: 11.0, 5: 10.5, 6: 10.0}

_BODY_PT = 9.6
_TABLE_PT = 8.4
_CODE_PT = 8.2


# ---------------------------------------------------------------------------
# PDF (reportlab / platypus)
# ---------------------------------------------------------------------------


class _FontSet(NamedTuple):
    body: str
    bold: str
    italic: str
    bold_italic: str
    mono: str
    mono_bold: str


_FONT_SET: _FontSet | None = None

#: Registered under our own names so a second registration of the same DejaVu file by
#: matplotlib (or by reportlab's own TTF cache) cannot collide with these.
_DEJAVU = (
    ("ACSans", "DejaVuSans.ttf"),
    ("ACSans-Bold", "DejaVuSans-Bold.ttf"),
    ("ACSans-Italic", "DejaVuSans-Oblique.ttf"),
    ("ACSans-BoldItalic", "DejaVuSans-BoldOblique.ttf"),
    ("ACMono", "DejaVuSansMono.ttf"),
    ("ACMono-Bold", "DejaVuSansMono-Bold.ttf"),
)


def _fonts() -> _FontSet:
    """DejaVu, from the copy matplotlib already ships, falling back to the built-in Type 1s.

    Two reasons for DejaVu over Helvetica: it covers the characters these documents are full of
    (`§`, `—`, `·`, `≥`, `→`), where a Type 1 base font is limited to WinAnsi and drops the rest;
    and it is the face the diagrams are drawn in, so a figure's labels match the prose around it.
    """
    global _FONT_SET
    if _FONT_SET is not None:
        return _FONT_SET
    try:
        import matplotlib
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        for name, filename in _DEJAVU:
            pdfmetrics.registerFont(TTFont(name, str(ttf / filename)))
        # `<b>`/`<i>` inside paragraph markup resolve through this mapping, not through the
        # style's font name — without it a bold run silently renders regular.
        addMapping("ACSans", 0, 0, "ACSans")
        addMapping("ACSans", 1, 0, "ACSans-Bold")
        addMapping("ACSans", 0, 1, "ACSans-Italic")
        addMapping("ACSans", 1, 1, "ACSans-BoldItalic")
        addMapping("ACMono", 0, 0, "ACMono")
        addMapping("ACMono", 1, 0, "ACMono-Bold")
        addMapping("ACMono", 0, 1, "ACMono")
        addMapping("ACMono", 1, 1, "ACMono-Bold")
        _FONT_SET = _FontSet(
            "ACSans", "ACSans-Bold", "ACSans-Italic", "ACSans-BoldItalic", "ACMono", "ACMono-Bold"
        )
    except Exception:  # noqa: BLE001 — a download must not fail over a missing font file
        logger.warning("DejaVu unavailable for PDF export; falling back to Helvetica", exc_info=True)
        _FONT_SET = _FontSet(
            "Helvetica",
            "Helvetica-Bold",
            "Helvetica-Oblique",
            "Helvetica-BoldOblique",
            "Courier",
            "Courier-Bold",
        )
    return _FONT_SET


def _xml(text: str) -> str:
    """Escape for reportlab's paragraph markup, which is XML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _pdf_markup(runs: Iterable[Run], fonts: _FontSet, *, size: float) -> str:
    """Runs → one reportlab paragraph string.

    Nothing here decides *whether* a mark applies; the parser already did. This only spells each
    mark the way `paraparser` expects, code spans included: a monospace face one point smaller
    (DejaVu Mono runs wide) on the same plate the preview gives them.
    """
    out: list[str] = []
    for run in runs:
        body = _xml(run.text)
        if not body:
            continue
        if run.code:
            body = (
                f'<font face="{fonts.mono}" size="{size - 0.9:.1f}" backColor="{CODE_BG}">'
                f"{body}</font>"
            )
        if run.bold:
            body = f"<b>{body}</b>"
        if run.italic:
            body = f"<i>{body}</i>"
        if run.href:
            body = (
                f'<link href="{_xml(run.href)}" color="{ACCENT}" '
                f'underline="1">{body}</link>'
            )
        out.append(body)
    return "".join(out) or "&nbsp;"


def _wrap_code(text: str, width: float, font: str, size: float) -> str:
    """Hard-wrap code so no line runs off the page.

    `Preformatted` does not wrap — it draws each line as written and lets it overflow the frame.
    SDD.md's fenced blocks are folder trees and command lines, and a tree whose right half is
    off the page is exactly the "broken" the reader complains about. Breaking is by character
    because a code line has no words to break at, preferring a seam (`/`, `.`, `-`) when one is
    close enough to the edge to be worth using.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    seams = "/\\.-_,:;)]}>= "
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.replace("\t", "    ")
        while stringWidth(line, font, size) > width and len(line) > 1:
            lo, hi = 1, len(line)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if stringWidth(line[:mid], font, size) <= width:
                    lo = mid
                else:
                    hi = mid - 1
            cut = max(1, lo)
            seam = max((line.rfind(ch, 0, cut) for ch in seams), default=-1)
            if seam >= cut * 0.6:
                cut = seam + 1
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return "\n".join(out)


def _pdf_image(data: bytes, avail_w: float, avail_h: float):
    """A figure scaled to the text column, and never taller than the page it has to fit on.

    An oversized `Image` is not clipped by platypus — it raises, and the whole download fails
    over one picture. The three architecture views are drawn at scale 3, so every one of them
    lands here needing to be scaled down.
    """
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image

    reader = ImageReader(io.BytesIO(data))
    px_w, px_h = reader.getSize()
    if px_w <= 0 or px_h <= 0:
        raise ValueError("image has no size")
    ratio = px_h / px_w
    width = avail_w
    height = width * ratio
    if height > avail_h:
        height = avail_h
        width = height / ratio
    return Image(io.BytesIO(data), width=width, height=height, kind="direct")


def markdown_to_pdf(
    src: str,
    *,
    title: str = "",
    subtitle: str = "",
    images: Mapping[str, bytes] | None = None,
) -> bytes:
    """`SDD.md` → PDF bytes, laid out block for block from `parse_markdown`."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        ListFlowable,
        ListItem,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    fonts = _fonts()
    pics = {normalize_image_path(k): v for k, v in (images or {}).items()}
    ink, muted, accent, line_c = HexColor(INK), HexColor(MUTED), HexColor(ACCENT), HexColor(LINE)

    margin = 18 * mm
    page_w, page_h = A4
    avail_w = page_w - 2 * margin
    avail_h = page_h - 2 * margin - 14 * mm  # less the footer band

    body = ParagraphStyle(
        "body",
        fontName=fonts.body,
        fontSize=_BODY_PT,
        leading=_BODY_PT * 1.45,
        textColor=ink,
        spaceAfter=5.0,
        # Words break at a seam rather than running into the margin — the same rule the diagram
        # renderer applies inside a box, for the same reason.
        splitLongWords=1,
    )
    quote = ParagraphStyle(
        "quote", parent=body, textColor=muted, fontName=fonts.italic, leftIndent=6.0
    )
    caption = ParagraphStyle(
        "caption", parent=body, fontSize=_BODY_PT - 0.8, textColor=muted, alignment=TA_CENTER
    )
    code = ParagraphStyle(
        "code",
        fontName=fonts.mono,
        fontSize=_CODE_PT,
        leading=_CODE_PT * 1.35,
        textColor=ink,
        spaceAfter=0,
    )
    cell_styles = {
        "": ParagraphStyle("cell", parent=body, fontSize=_TABLE_PT,
                           leading=_TABLE_PT * 1.35, spaceAfter=0, alignment=TA_LEFT),
        "left": None,
        "center": None,
        "right": None,
    }
    cell_styles["left"] = cell_styles[""]
    cell_styles["center"] = ParagraphStyle(
        "cell-c", parent=cell_styles[""], alignment=TA_CENTER
    )
    cell_styles["right"] = ParagraphStyle("cell-r", parent=cell_styles[""], alignment=TA_RIGHT)
    head_styles = {
        key: ParagraphStyle(f"head-{key or 'l'}", parent=style, fontName=fonts.bold)
        for key, style in cell_styles.items()
    }
    heading_styles = {
        level: ParagraphStyle(
            f"h{level}",
            parent=body,
            fontName=fonts.bold,
            fontSize=pt,
            leading=pt * 1.28,
            textColor=ink if level > 1 else HexColor("#0d1a15"),
            spaceBefore=13.0 if level <= 2 else 9.0,
            spaceAfter=4.0,
            # A heading alone at the foot of a page, with its section overleaf, is the other
            # thing readers call broken. platypus moves it forward with whatever follows.
            keepWithNext=1,
        )
        for level, pt in _PDF_HEADING_PT.items()
    }

    story: list = []
    for block in parse_markdown(src):
        if block.kind == "heading":
            level = max(1, min(6, block.level))
            style = heading_styles[level]
            markup = _pdf_markup(block.runs, fonts, size=style.fontSize)
            # `<a name>` is what makes the document's own table of contents work — 26 links in
            # SDD.md point at these anchors, and without a destination every one is a no-op.
            if block.slug:
                markup = f'<a name="{_xml(block.slug)}"/>{markup}'
            para = Paragraph(markup, style)
            para._sdd_outline = (plain(block.runs), block.slug, level - 1)  # type: ignore[attr-defined]
            story.append(para)
            if level <= 2:
                story.append(
                    Table(
                        [[""]],
                        colWidths=[avail_w],
                        rowHeights=[1.0],
                        style=TableStyle(
                            [("LINEBELOW", (0, 0), (-1, -1), 0.6, accent if level == 1 else line_c)]
                        ),
                    )
                )
                story.append(Spacer(1, 5.0))
        elif block.kind == "para":
            story.append(Paragraph(_pdf_markup(block.runs, fonts, size=_BODY_PT), body))
        elif block.kind == "rule":
            story.append(Spacer(1, 3.0))
            story.append(
                Table(
                    [[""]],
                    colWidths=[avail_w],
                    rowHeights=[1.0],
                    style=TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.6, line_c)]),
                )
            )
            story.append(Spacer(1, 7.0))
        elif block.kind == "quote":
            inner = Paragraph(_pdf_markup(block.runs, fonts, size=_BODY_PT), quote)
            story.append(
                Table(
                    [[inner]],
                    colWidths=[avail_w],
                    style=TableStyle(
                        [
                            ("LINEBEFORE", (0, 0), (0, -1), 2.0, accent),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                            ("TOPPADDING", (0, 0), (-1, -1), 3),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ]
                    ),
                )
            )
            story.append(Spacer(1, 5.0))
        elif block.kind == "list":
            items = [
                ListItem(Paragraph(_pdf_markup(runs, fonts, size=_BODY_PT), body), leftIndent=16)
                for runs in block.items
            ]
            # `start` as a string in both cases: with `bulletType="1"` an integer reaches
            # `canvas.drawString` unformatted and raises.
            extra = {"start": "1"} if block.ordered else {"start": "•"}
            story.append(
                ListFlowable(
                    items,
                    bulletType="1" if block.ordered else "bullet",
                    bulletFontName=fonts.body,
                    bulletFontSize=_BODY_PT,
                    bulletColor=ink if block.ordered else accent,
                    leftIndent=16,
                    bulletDedent=11,
                    **extra,
                )
            )
            story.append(Spacer(1, 4.0))
        elif block.kind in ("code", "frontmatter"):
            wrapped = _wrap_code(block.text, avail_w - 16, fonts.mono, _CODE_PT)
            story.append(
                Table(
                    [[Preformatted(wrapped, code)]],
                    colWidths=[avail_w],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), HexColor(CODE_BG)),
                            ("BOX", (0, 0), (-1, -1), 0.5, line_c),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                            ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ]
                    ),
                )
            )
            story.append(Spacer(1, 6.0))
        elif block.kind == "table":
            story.extend(
                _pdf_table(
                    block,
                    fonts=fonts,
                    avail_w=avail_w,
                    cell_styles=cell_styles,
                    head_styles=head_styles,
                    line_c=line_c,
                )
            )
        elif block.kind == "figure":
            data = pics.get(normalize_image_path(block.path))
            drawn = None
            if data:
                try:
                    drawn = _pdf_image(data, avail_w, avail_h * 0.82)
                except Exception:  # noqa: BLE001 — a bad PNG becomes a placeholder, not a 500
                    logger.warning("Figure %s could not be placed in the PDF", block.path)
            if drawn is not None:
                # The figure and the `*Figure 3.2.1 — …*` line under it are one unit; a page
                # break between a picture and its caption is the classic export defect.
                story.append(Spacer(1, 3.0))
                story.append(drawn)
                story.append(Spacer(1, 3.0))
            else:
                story.append(
                    Paragraph(
                        f"{_xml(block.alt)} "
                        f'<font color="{MUTED}">({_xml(block.path)})</font>',
                        caption,
                    )
                )
    if not story:
        story.append(Paragraph("This document is empty.", body))

    footer_left = " · ".join(p for p in (title.strip(), subtitle.strip()) if p)

    def _footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(line_c)
        canvas.setLineWidth(0.5)
        y = margin - 6 * mm
        canvas.line(margin, y + 4 * mm, page_w - margin, y + 4 * mm)
        canvas.setFont(fonts.body, 7.6)
        canvas.setFillColor(muted)
        canvas.drawString(margin, y, footer_left[:110])
        canvas.drawRightString(page_w - margin, y, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    class _Doc(SimpleDocTemplate):
        """Adds a PDF outline entry per heading, so the sidebar mirrors the document."""

        def afterFlowable(self, flowable) -> None:  # noqa: N802 — reportlab's spelling
            info = getattr(flowable, "_sdd_outline", None)
            if not info:
                return
            text, key, level = info
            if not text:
                return
            if key:
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(text[:120], key, level=min(level, 5), closed=level > 1)

    buf = io.BytesIO()
    doc = _Doc(
        buf,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin + 8 * mm,
        title=title or "Solution Design Document",
        author="AgentCraft Studio",
        subject=subtitle or "Solution Design Document",
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _pdf_table(block: Block, *, fonts: _FontSet, avail_w: float, cell_styles, head_styles, line_c):
    """One markdown table as a platypus `Table`, sized to the text column.

    Column widths come from the natural width of each column's content, then are scaled to fit
    exactly `avail_w` — a table wider than the page is silently clipped by platypus, which is
    how a 7-column risk register loses its last two columns. Every cell is a `Paragraph`, so a
    long cell wraps instead of forcing the column wider.
    """
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    cols = len(block.headers)
    if not cols:
        return []
    aligns = [
        block.aligns[i] if i < len(block.aligns) and block.aligns[i] in cell_styles else ""
        for i in range(cols)
    ]

    def metrics(runs, *, header: bool = False) -> tuple[float, float]:
        """(width of the whole cell on one line, width of its widest single word).

        `header` measures in bold whether or not the markdown says so, because the header style
        draws it bold regardless — measuring `Version` in the regular face is what left it one
        point too narrow for the face it was drawn in, and broke it as `Versio / n`.
        """
        total = widest = 0.0
        for run in runs:
            if run.image:
                continue
            bold = run.bold or header
            font = fonts.mono if run.code else (fonts.bold if bold else fonts.body)
            size = _TABLE_PT - 0.9 if run.code else _TABLE_PT
            total += stringWidth(run.text, font, size)
            for word in run.text.split():
                widest = max(widest, stringWidth(word, font, size))
        return total, widest

    pad = 11.0
    #: No column narrower than this, or a two-word header comes out one letter per line.
    floor_w = 34.0
    needs: list[float] = []  # the widest single word — below this, words break mid-word
    wants: list[float] = []  # the whole cell on one line — what the column would like
    for i in range(cols):
        measured = [metrics(block.headers[i], header=True)]
        measured += [metrics(row[i]) for row in block.rows]
        # A column is never squeezed below its longest *word*. That is the whole fix for
        # `Performanc / e` and `backend/cor / e/`: reportlab breaks a word that cannot fit its
        # column, so guaranteeing room for the longest word is what guarantees no mid-word break.
        # Capped so one monster token — a 60-character endpoint — cannot starve five columns.
        needs.append(min(max((m[1] for m in measured), default=0.0) + pad, avail_w * 0.34))
        wants.append(min(max((m[0] for m in measured), default=0.0) + pad, avail_w * 0.52))

    widths = [max(floor_w, n) for n in needs]
    if sum(widths) > avail_w:
        # More longest-words than the page is wide: scale them together, so what breaking there
        # has to be is spread evenly instead of falling entirely on the last column.
        scale = avail_w / sum(widths)
        widths = [w * scale for w in widths]
    else:
        # Hand the leftover out in proportion to what each column still wants, so a prose column
        # grows and an `ID` column stays narrow.
        spare = avail_w - sum(widths)
        hunger = [max(0.0, wants[i] - widths[i]) for i in range(cols)]
        if sum(hunger) > 0:
            share = min(1.0, spare / sum(hunger))
            widths = [widths[i] + hunger[i] * share for i in range(cols)]
            spare = avail_w - sum(widths)
        if spare > 0.5:  # every column already fits on one line — fill the width evenly
            widths = [w + spare / cols for w in widths]

    data = [[Paragraph(_pdf_markup(c, fonts, size=_TABLE_PT), head_styles[aligns[i]])
             for i, c in enumerate(block.headers)]]
    for row in block.rows:
        data.append(
            [Paragraph(_pdf_markup(c, fonts, size=_TABLE_PT), cell_styles[aligns[i]])
             for i, c in enumerate(row)]
        )
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, line_c),
                ("BACKGROUND", (0, 0), (-1, 0), HexColor(HEAD_BG)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return [table, Spacer(1, 7.0)]


# ---------------------------------------------------------------------------
# DOCX (python-docx)
# ---------------------------------------------------------------------------


def _docx_hyperlink(paragraph, url: str) -> object:
    """A `w:hyperlink` element ready for runs, external or in-document.

    python-docx has no hyperlink API, so this is the documented XML: an external URL becomes a
    relationship on the part, and a `#anchor` becomes a `w:anchor` pointing at the bookmark the
    matching heading writes.
    """
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement

    link = OxmlElement("w:hyperlink")
    if url.startswith("#"):
        link.set(qn("w:anchor"), url[1:])
    else:
        rid = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
        link.set(qn("r:id"), rid)
    paragraph._p.append(link)
    return link


def _docx_bookmark(paragraph, name: str, bid: int) -> None:
    """A `w:bookmarkStart/End` pair around a heading, so `#anchor` links land on it."""
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement

    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bid))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bid))
    paragraph._p.insert(0, start)
    paragraph._p.append(end)


def _docx_rule(paragraph) -> None:
    """A thematic break — an empty paragraph wearing a bottom border, as Word itself writes one."""
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement
    from docx.shared import Pt

    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), LINE.lstrip("#"))
    borders.append(bottom)
    paragraph._p.get_or_add_pPr().append(borders)
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(8)


def _docx_shade(element, fill: str) -> None:
    """Solid background on a paragraph or a table cell."""
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement

    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill.lstrip("#"))
    element.append(shd)


def _docx_runs(paragraph, runs: Iterable[Run], *, size: float, mono: str, body_font: str) -> None:
    """Add `runs` to a paragraph, one Word run per mark combination.

    Links are containers rather than a run property, so a linked stretch is added inside the
    `w:hyperlink` element — which is also the only way its bold or code marks survive.
    """
    from docx.shared import Pt, RGBColor

    for run in runs:
        if not run.text:
            continue
        parent = _docx_hyperlink(paragraph, run.href) if run.href else None
        r = paragraph.add_run(run.text)
        if parent is not None:
            parent.append(r._r)  # move the run inside the hyperlink element
        r.font.name = mono if run.code else body_font
        r.font.size = Pt(size - 0.8 if run.code else size)
        r.bold = True if run.bold else None
        r.italic = True if run.italic else None
        if run.href:
            r.font.color.rgb = RGBColor.from_string(ACCENT.lstrip("#").upper())
            r.font.underline = True
        elif run.code:
            r.font.color.rgb = RGBColor.from_string("0F4E3A")


def markdown_to_docx(
    src: str,
    *,
    title: str = "",
    subtitle: str = "",
    images: Mapping[str, bytes] | None = None,
) -> bytes:
    """`SDD.md` → DOCX bytes, from the same blocks the PDF is built from."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Emu, Inches, Pt, RGBColor

    fonts_body = "DejaVu Sans"
    fonts_mono = "DejaVu Sans Mono"
    pics = {normalize_image_path(k): v for k, v in (images or {}).items()}

    doc = Document()
    doc.core_properties.title = title or "Solution Design Document"
    doc.core_properties.subject = subtitle or "Solution Design Document"
    doc.core_properties.author = "AgentCraft Studio"

    section = doc.sections[0]
    section.left_margin = section.right_margin = Inches(0.85)
    section.top_margin = section.bottom_margin = Inches(0.8)
    avail_w = section.page_width - section.left_margin - section.right_margin

    normal = doc.styles["Normal"]
    normal.font.name = fonts_body
    normal.font.size = Pt(_BODY_PT + 0.4)
    normal.font.color.rgb = RGBColor.from_string(INK.lstrip("#").upper())
    # East-Asian font name too, or Word substitutes its own face for anything non-Latin.
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), fonts_body)
    normal.paragraph_format.space_after = Pt(5)
    normal.paragraph_format.line_spacing = 1.15
    for level, pt in _DOCX_HEADING_PT.items():
        try:
            style = doc.styles[f"Heading {level}"]
        except KeyError:  # pragma: no cover — present in every default template
            continue
        style.font.name = fonts_body
        style.font.size = Pt(pt)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(INK.lstrip("#").upper())
        style.paragraph_format.space_before = Pt(12 if level <= 2 else 8)
        style.paragraph_format.space_after = Pt(4)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_text = " · ".join(p for p in (title.strip(), subtitle.strip()) if p)
    if footer_text:
        run = footer.add_run(footer_text)
        run.font.size = Pt(7.5)
        run.font.name = fonts_body
        run.font.color.rgb = RGBColor.from_string(MUTED.lstrip("#").upper())

    bookmark_id = 1
    for block in parse_markdown(src):
        if block.kind == "heading":
            level = max(1, min(6, block.level))
            para = doc.add_paragraph(style=f"Heading {level}")
            _docx_runs(
                para,
                block.runs,
                size=_DOCX_HEADING_PT[level],
                mono=fonts_mono,
                body_font=fonts_body,
            )
            if block.slug:
                _docx_bookmark(para, block.slug, bookmark_id)
                bookmark_id += 1
        elif block.kind == "para":
            para = doc.add_paragraph()
            _docx_runs(para, block.runs, size=_BODY_PT + 0.4, mono=fonts_mono, body_font=fonts_body)
        elif block.kind == "rule":
            _docx_rule(doc.add_paragraph())
        elif block.kind == "quote":
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = Inches(0.25)
            _docx_runs(para, block.runs, size=_BODY_PT + 0.4, mono=fonts_mono, body_font=fonts_body)
            for run in para.runs:
                run.italic = True
                run.font.color.rgb = RGBColor.from_string(MUTED.lstrip("#").upper())
        elif block.kind == "list":
            style = "List Number" if block.ordered else "List Bullet"
            for runs in block.items:
                try:
                    para = doc.add_paragraph(style=style)
                except KeyError:  # pragma: no cover — template without list styles
                    para = doc.add_paragraph()
                    para.paragraph_format.left_indent = Inches(0.3)
                _docx_runs(
                    para, runs, size=_BODY_PT + 0.4, mono=fonts_mono, body_font=fonts_body
                )
        elif block.kind in ("code", "frontmatter"):
            # One paragraph per line, not one paragraph with line breaks: Word then keeps the
            # block's shading tight to the text and lets a long fence break across pages.
            lines = block.text.split("\n") or [""]
            for text in lines:
                para = doc.add_paragraph()
                pf = para.paragraph_format
                pf.space_after = Pt(0)
                pf.space_before = Pt(0)
                pf.line_spacing = 1.0
                pf.left_indent = Inches(0.12)
                _docx_shade(para._p.get_or_add_pPr(), CODE_BG)
                run = para.add_run(text.replace("\t", "    ") or " ")
                run.font.name = fonts_mono
                run.font.size = Pt(_CODE_PT + 0.4)
            doc.add_paragraph().paragraph_format.space_after = Pt(2)
        elif block.kind == "table":
            _docx_table(doc, block, avail_w=avail_w, mono=fonts_mono, body_font=fonts_body)
        elif block.kind == "figure":
            data = pics.get(normalize_image_path(block.path))
            placed = False
            if data:
                try:
                    para = doc.add_paragraph()
                    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    para.add_run().add_picture(io.BytesIO(data), width=Emu(int(avail_w)))
                    placed = True
                except Exception:  # noqa: BLE001 — a bad PNG becomes a placeholder, not a 500
                    logger.warning("Figure %s could not be placed in the DOCX", block.path)
            if not placed:
                para = doc.add_paragraph()
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = para.add_run(f"{block.alt} ({block.path})")
                run.italic = True
                run.font.size = Pt(_BODY_PT - 0.4)
                run.font.color.rgb = RGBColor.from_string(MUTED.lstrip("#").upper())

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _docx_table(doc, block: Block, *, avail_w: int, mono: str, body_font: str) -> None:
    """One markdown table as a Word table, header repeated on every page.

    Widths are set on every cell as well as on the column, because Word honours the cell width
    and ignores `w:gridCol` alone when autofit is on — the reason a "fixed" table still comes
    out with one column squeezed to nothing.
    """
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Emu, Pt

    cols = len(block.headers)
    if not cols:
        return
    aligns = [
        block.aligns[i] if i < len(block.aligns) else "" for i in range(cols)
    ]
    para_align = {
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }

    table = doc.add_table(rows=1 + len(block.rows), cols=cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False

    # Natural width per column from its longest cell, then scaled to the text column — the same
    # rule the PDF uses, so the two documents break their columns in the same places.
    def measure(runs) -> int:
        return max(1, len(plain(runs)))

    wants = [
        min(
            max(measure(block.headers[i]), *(measure(r[i]) for r in block.rows), 8)
            if block.rows
            else max(measure(block.headers[i]), 8),
            48,
        )
        for i in range(cols)
    ]
    total = sum(wants) or 1
    widths = [int(avail_w * w / total) for w in wants]

    header_cells = table.rows[0].cells
    for i, runs in enumerate(block.headers):
        cell = header_cells[i]
        cell.width = Emu(widths[i])
        _docx_shade(cell._tc.get_or_add_tcPr(), HEAD_BG)
        para = cell.paragraphs[0]
        para.alignment = para_align.get(aligns[i], WD_ALIGN_PARAGRAPH.LEFT)
        para.paragraph_format.space_after = Pt(1)
        _docx_runs(para, runs, size=_TABLE_PT + 0.6, mono=mono, body_font=body_font)
        for run in para.runs:
            run.bold = True
    # `w:tblHeader` is what repeats the header when a table crosses a page.
    trPr = table.rows[0]._tr.get_or_add_trPr()
    repeat = trPr.makeelement(qn("w:tblHeader"), {})
    trPr.append(repeat)

    for r, row in enumerate(block.rows, start=1):
        cells = table.rows[r].cells
        for i, runs in enumerate(row):
            cell = cells[i]
            cell.width = Emu(widths[i])
            para = cell.paragraphs[0]
            para.alignment = para_align.get(aligns[i], WD_ALIGN_PARAGRAPH.LEFT)
            para.paragraph_format.space_after = Pt(1)
            _docx_runs(para, runs, size=_TABLE_PT + 0.6, mono=mono, body_font=body_font)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_document(
    src: str,
    fmt: str,
    *,
    title: str = "",
    subtitle: str = "",
    images: Mapping[str, bytes] | None = None,
) -> bytes:
    """`src` markdown as `fmt` (`pdf` or `docx`) bytes."""
    key = (fmt or "").lower().lstrip(".")
    if key == "pdf":
        return markdown_to_pdf(src, title=title, subtitle=subtitle, images=images)
    if key == "docx":
        return markdown_to_docx(src, title=title, subtitle=subtitle, images=images)
    raise ValueError(f"Unsupported document format: {fmt}")


__all__ = [
    "Block",
    "DOC_FORMATS",
    "Run",
    "heading_slug",
    "image_paths",
    "inline",
    "markdown_to_docx",
    "markdown_to_pdf",
    "media_type",
    "normalize_image_path",
    "parse_markdown",
    "plain",
    "render_document",
]
