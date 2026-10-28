"""Normalize agent ## Goals sections into markdown bullet lists."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.schemas import ProjectPlan

_GOALS_HEADING = re.compile(r"(?im)^(#{2}\s+Goals)\s*$")
_ALT_GOALS_HEADING = re.compile(
    r"(?im)^(#{2}\s+(?:Core Responsibilities|Must block(?:\s*/\s*fix)?|Checks|Responsibilities))\s*$"
)
_NEXT_H2 = re.compile(r"(?m)^#{1,2}\s+\S")
_BULLET = re.compile(r"^[-*•]\s+")
_NUMBERED = re.compile(r"^\d+[.)]\s+")
_SUBHEAD = re.compile(r"^#{3,6}\s+")
_BOLD_LEAD = re.compile(r"^\*?\*?([^*]+?)\*?\*?\s*[–—:-]\s*(.+)$")


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split on sep when not inside parentheses."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(sep)
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
            buf.append(c)
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            buf.append(c)
            i += 1
            continue
        if depth == 0 and text.startswith(sep, i):
            chunk = "".join(buf).strip()
            if chunk:
                parts.append(chunk)
            buf = []
            i += n
            continue
        buf.append(c)
        i += 1
    chunk = "".join(buf).strip()
    if chunk:
        parts.append(chunk)
    return parts


def _split_comma_list(text: str) -> list[str]:
    """Split comma-separated goal clauses at paren depth 0."""
    return [p for p in _split_top_level(text, ", ") if len(p.strip()) > 6]


def _paragraph_to_items(para: str) -> list[str]:
    para = " ".join((para or "").split()).strip()
    if not para:
        return []

    items: list[str] = []
    for sent in _split_top_level(para, ". "):
        sent = sent.strip().rstrip(".").strip()
        if not sent:
            continue
        # Dense "Enforce A (…), B (…), C (…)" style — one bullet per clause.
        if sent.count("(") >= 2 and ", " in sent:
            chunks = _split_comma_list(sent)
            if len(chunks) >= 2:
                items.extend(c.rstrip(".").strip() for c in chunks if c.strip())
                continue
        items.append(sent)
    return items


def _line_to_item(line: str) -> str | None:
    s = line.strip()
    if not s:
        return None
    if _SUBHEAD.match(s):
        s = _SUBHEAD.sub("", s).strip()
        s = _NUMBERED.sub("", s).strip()
        return s or None
    if _BULLET.match(s):
        return _BULLET.sub("", s).strip() or None
    if _NUMBERED.match(s):
        rest = _NUMBERED.sub("", s).strip()
        m = _BOLD_LEAD.match(rest)
        if m:
            title, detail = m.group(1).strip(), m.group(2).strip()
            return f"{title} — {detail}" if detail else title
        return rest or None
    return None


def _section_to_bullets(body: str) -> list[str]:
    raw_lines = [ln.rstrip() for ln in (body or "").splitlines()]
    lines = [ln for ln in raw_lines if ln.strip()]
    if not lines:
        return []

    structured = [_line_to_item(ln) for ln in lines]
    if all(x is not None for x in structured) and any(structured):
        items = [x for x in structured if x]
        if len(items) > 20:
            items = items[:20]
        return [f"- {it}" for it in items]

    items: list[str] = []
    prose_chunks: list[str] = []
    for ln in lines:
        parsed = _line_to_item(ln)
        if parsed is not None and (
            _BULLET.match(ln.strip())
            or _NUMBERED.match(ln.strip())
            or _SUBHEAD.match(ln.strip())
        ):
            if prose_chunks:
                items.extend(_paragraph_to_items(" ".join(prose_chunks)))
                prose_chunks = []
            items.append(parsed)
        else:
            prose_chunks.append(ln.strip())
    if prose_chunks:
        items.extend(_paragraph_to_items(" ".join(prose_chunks)))

    if len(items) > 20:
        items = items[:20]

    return [f"- {it}" for it in items if it]


def _rewrite_section(text: str, heading_match: re.Match[str], *, force_title: str | None = None) -> str:
    start = heading_match.end()
    rest = text[start:]
    next_h = _NEXT_H2.search(rest)
    end = start + next_h.start() if next_h else len(text)
    section_body = text[start:end].strip("\n")

    bullets = _section_to_bullets(section_body)
    if not bullets:
        return text

    new_body = "\n".join(bullets)
    if (
        force_title is None
        and section_body.strip() == new_body.strip()
        and all(ln.strip().startswith("- ") for ln in section_body.splitlines() if ln.strip())
    ):
        return text

    title = force_title or heading_match.group(1)
    prefix = text[: heading_match.start()] + title + "\n" + new_body
    if next_h:
        suffix = "\n\n" + text[end:].lstrip("\n")
        return (prefix + suffix).rstrip() + "\n"
    return prefix.rstrip() + "\n"


def ensure_goals_bullets(markdown: str, *, prefer_vapt_alt: bool = False) -> str:
    """
    Rewrite the ## Goals section of an agent system prompt into `-` bullets.
    When prefer_vapt_alt is True and ## Goals is missing, rename common VAPT
    checklist headings (Core Responsibilities / Checks / Must block) to ## Goals.
    """
    text = (markdown or "").strip()
    if not text:
        return markdown or ""

    m = _GOALS_HEADING.search(text)
    if m:
        return _rewrite_section(text, m)

    if prefer_vapt_alt:
        alt = _ALT_GOALS_HEADING.search(text)
        if alt:
            return _rewrite_section(text, alt, force_title="## Goals")

    return text


def normalize_plan_agent_goals(plan: "ProjectPlan") -> "ProjectPlan":
    """Ensure every agent's ## Goals section uses bullet points."""
    agents = []
    changed = False
    for agent in plan.agents:
        old = agent.system_prompt or ""
        name = (agent.name or "").lower()
        is_vapt = name == "security-vapt-reviewer" or "vapt" in name or name.startswith("security")
        new_prompt = ensure_goals_bullets(old, prefer_vapt_alt=is_vapt).rstrip()
        if new_prompt:
            new_prompt += "\n"
        if new_prompt.rstrip("\n") != old.rstrip("\n"):
            changed = True
            agents.append(agent.model_copy(update={"system_prompt": new_prompt}))
        else:
            agents.append(agent)
    if not changed:
        return plan
    return plan.model_copy(update={"agents": agents})
