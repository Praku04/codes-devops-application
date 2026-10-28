"""Normalize video analysis markdown formatting for consistent UI/PDF output."""

from __future__ import annotations

from typing import Optional


def _leading_markdown_hashes(line: str) -> int:
    count = 0
    for ch in line:
        if ch == "#":
            count += 1
        else:
            break
    return count


def _strip_heading_prefix(line: str) -> tuple[int, str]:
    hashes = _leading_markdown_hashes(line.lstrip())
    if hashes < 2:
        return 0, line
    stripped = line.lstrip()
    return hashes, stripped[hashes:].lstrip()


def _normalize_sop_narrative_heading(line: str) -> str:
    hashes, body = _strip_heading_prefix(line)
    if hashes < 2 or hashes > 6:
        return line
    compact = "".join(ch for ch in body.lower() if ch.isalnum())
    if compact == "sopstepbystepexecutionnarrative":
        return "## SOP Step-by-Step Execution Narrative"
    return line


def _normalize_figure_heading(line: str) -> str:
    hashes, body = _strip_heading_prefix(line)
    if hashes < 3 or hashes > 6:
        return line
    lower = body.lower()
    if not lower.startswith("figure"):
        return line
    rest = body[6:].lstrip()
    if rest.startswith(("-", ":")):
        rest = rest[1:].lstrip()
    number = ""
    for ch in rest:
        if ch.isdigit():
            number += ch
        else:
            break
    if not number:
        return line
    return f"### Figure {int(number)}"


def _normalize_step_heading(line: str) -> str:
    hashes, body = _strip_heading_prefix(line)
    if hashes < 3 or hashes > 6:
        return line
    if not body.lower().startswith("step"):
        return line
    rest = body[4:].lstrip()
    step_no = ""
    for ch in rest:
        if ch.isdigit():
            step_no += ch
        else:
            break
    if not step_no:
        return line
    title = rest[len(step_no) :].lstrip()
    if title.startswith(":"):
        title = title[1:].lstrip()
    if not title:
        return line
    return f"### Step {int(step_no):02d}: {title}"


def _normalize_label_prefix(line: str, label: str, replacement: str) -> str:
    stripped = line.lstrip()
    if stripped.lower().startswith(label.lower()):
        value = stripped[len(label) :].lstrip()
        if value:
            return f"{replacement}{value}"
    return line


def _keep_last_sop_narrative_section(md: str) -> str:
    """
    Remove duplicate SOP narrative sections and keep only the last occurrence,
    which is typically the richer, explicitly formatted version.
    """
    marker = "## SOP Step-by-Step Execution Narrative"
    lower_md = md.lower()
    marker_lower = marker.lower()
    starts: list[int] = []
    search_from = 0
    while True:
        pos = lower_md.find(marker_lower, search_from)
        if pos < 0:
            break
        starts.append(pos)
        search_from = pos + len(marker_lower)

    if len(starts) <= 1:
        return md

    ends: list[int] = []
    for start in starts:
        next_heading = md.find("\n## ", start + len(marker))
        ends.append(next_heading if next_heading >= 0 else len(md))

    keep_idx = len(starts) - 1
    rebuilt: list[str] = []
    cursor = 0
    for idx, (start, end) in enumerate(zip(starts, ends)):
        if idx == keep_idx:
            rebuilt.append(md[cursor:end])
            cursor = end
        else:
            rebuilt.append(md[cursor:start])
            cursor = end
    rebuilt.append(md[cursor:])
    return "".join(rebuilt)


def normalize_video_analysis_markdown(text: str) -> str:
    """Normalize report markdown formatting for consistent UI/PDF output."""
    cleaned = text or ""
    lines = cleaned.splitlines()
    normalized_lines = []
    for line in lines:
        updated = _normalize_sop_narrative_heading(line)
        if updated == line:
            updated = _normalize_figure_heading(line)
        if updated == line:
            updated = _normalize_step_heading(line)
        if updated == line:
            updated = _normalize_label_prefix(line, "Caption:", "**Caption:** ")
        if updated == line:
            updated = _normalize_label_prefix(line, "Timestamp:", "**Timestamp:** ")
        if updated == line:
            updated = _normalize_label_prefix(line, "Analysis:", "**Analysis:** ")
        normalized_lines.append(updated)

    cleaned = "\n".join(normalized_lines)
    cleaned = _keep_last_sop_narrative_section(cleaned)

    while "\n---\n\n---\n" in cleaned:
        cleaned = cleaned.replace("\n---\n\n---\n", "\n---\n")
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned.strip() + "\n"


def _strip_inline_markdown_light(text: str) -> str:
    """Remove common inline markdown markers without regex (CodeQL-safe)."""
    out: list[str] = []
    i = 0
    raw = text or ""
    while i < len(raw):
        ch = raw[i]
        if ch in "*_`":
            i += 1
            continue
        if ch == "[":
            close = raw.find("]", i + 1)
            if close != -1 and close + 1 < len(raw) and raw[close + 1] == "(":
                end_paren = raw.find(")", close + 2)
                if end_paren != -1:
                    out.append(raw[i + 1 : close])
                    i = end_paren + 1
                    continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    title = "".join(out)
    while "  " in title:
        title = title.replace("  ", " ")
    return title.strip(" \t-—|:;")


def extract_document_title_from_markdown(markdown_content: Optional[str]) -> Optional[str]:
    """
    Extract the same H1 title used on the PDF/DOCX cover page.
    Line scan only — no ambiguous regex (CodeQL / ReDoS safe).
    """
    if not markdown_content:
        return None
    for raw_line in markdown_content.splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue
        hashes = 0
        for ch in line:
            if ch == "#":
                hashes += 1
            else:
                break
        if hashes != 1:
            continue
        body = line[1:].lstrip()
        if not body:
            continue
        lower = body.lower()
        if lower in {"table of contents", "contents", "error"}:
            continue
        title = _strip_inline_markdown_light(body)
        if 5 <= len(title) <= 200:
            return title[:200]
        if title:
            return title[:200]
    return None


def resolve_report_pdf_title(
    stored_title: Optional[str],
    markdown_content: Optional[str] = None,
    *,
    default: str = "Meeting Documentation",
) -> str:
    """Prefer markdown H1 (PDF cover title) over separately stored AI short titles."""
    from_md = extract_document_title_from_markdown(markdown_content)
    if from_md:
        return from_md
    stored = (stored_title or "").strip()
    if stored:
        return stored[:200]
    return default

