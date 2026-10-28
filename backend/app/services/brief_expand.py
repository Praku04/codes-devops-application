"""Expand a short problem seed into a detailed AgentCraft brief (markdown).

Sections are generated asynchronously in parallel (asyncio.gather) for speed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.services.llm.client import llm_client

logger = logging.getLogger("agentcraft.brief_expand")

_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "expand_brief_system.txt"

_SECTION_ORDER = (
    "product",
    "problem",
    "goals",
    "stack",
    "domains",
    "constraints",
    "integrations",
    "success",
)

# Public alias for API / UI
SECTION_ORDER = _SECTION_ORDER

_SECTION_HINTS: dict[str, str] = {
    "product": "What is being built, who it is for, and the core value proposition (2–4 short paragraphs or bullets).",
    "problem": "The pain / problem space this product solves (concrete, not vague).",
    "goals": "Measurable product and delivery goals (bullet list).",
    "stack": "Likely tech stack (languages, frameworks, data stores, cloud) as bullets.",
    "domains": "Business / technical domains involved (short bullet list).",
    "constraints": "Compliance, security, performance, timeline, or operational constraints.",
    "integrations": "External systems, APIs, vendors to integrate with (bullets).",
    "success": "How we know it worked — acceptance / success criteria (bullets).",
}

SectionCallback = Callable[[str, str], Awaitable[None] | None]


def is_short_brief(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if len(t) < 280:
        return True
    return len(t.splitlines()) <= 3


def seed_for_expansion(seed: str) -> str:
    """
    Compact grounding text for LLM expand.

    If the user clicks Re-expand on an already structured brief, do not feed the
    entire previous markdown back as-is — distill Product (or plain text) so the
    model rebuilds Goals / Stack / Constraints instead of echoing.
    """
    t = (seed or "").strip()
    if not t:
        return t
    if re.search(r"^##\s+", t, flags=re.M):
        product = ""
        m = re.search(r"^##\s+Product\s*\n(.*?)(?=^##\s+|\Z)", t, flags=re.M | re.S)
        if m:
            product = m.group(1).strip()
        plain = re.sub(r"^##\s+.+$", "", t, flags=re.M)
        plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
        base = (product or plain)[:1400]
        return (
            f"User product intent:\n{base}\n\n"
            "Rebuild a FULL structured brief with NEW concrete Goals, Stack, Domains, "
            "Constraints, Integrations, and Success. Stay faithful to this product — "
            "do not invent a different product, and do not copy a prior brief verbatim."
        )
    return t[:2500]


def _looks_like_raw_json(text: str) -> bool:
    t = text.strip()
    return (t.startswith("{") and t.endswith("}")) or (t.startswith("{'") and "':" in t)


def _title(key: str) -> str:
    return key.replace("_", " ").strip().title()


def _value_to_lines(value: Any, *, indent: int = 0) -> list[str]:
    pad = "  " * indent
    if value is None:
        return []
    if isinstance(value, bool):
        return [f"{pad}- {str(value).lower()}"]
    if isinstance(value, (int, float)):
        return [f"{pad}- {value}"]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "\n" in text:
            return [f"{pad}{ln}" if ln.strip() else "" for ln in text.splitlines()]
        return [f"{pad}{text}"]
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.extend(_value_to_lines(item, indent=indent))
            else:
                lines.append(f"{pad}- {item}")
        return lines
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}**{_title(str(k))}**")
                lines.extend(_value_to_lines(v, indent=indent + 1))
            else:
                rendered = ", ".join(str(x) for x in v) if isinstance(v, list) else v
                lines.append(f"{pad}- **{_title(str(k))}:** {rendered}")
        return lines
    return [f"{pad}{value}"]


def structure_to_markdown(data: dict[str, Any]) -> str:
    """Turn a nested brief dict into readable markdown sections."""
    parts: list[str] = []
    keys = [k for k in _SECTION_ORDER if k in data] + [
        k for k in data.keys() if str(k).lower() not in _SECTION_ORDER and k != "expanded"
    ]
    for key in keys:
        val = data[key]
        parts.append(f"## {_title(str(key))}")
        parts.append("")
        lines = _value_to_lines(val)
        if lines:
            parts.extend(lines)
        else:
            parts.append("_TBD_")
        parts.append("")
    return "\n".join(parts).strip()


def normalize_expanded(payload: Any) -> str:
    """Accept string markdown, nested JSON, or {expanded: ...} and return clean markdown."""
    if payload is None:
        return ""

    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if _looks_like_raw_json(text):
            try:
                parsed = json.loads(text.replace("'", '"'))
            except Exception:
                try:
                    import ast

                    parsed = ast.literal_eval(text)
                except Exception:
                    return text
            return normalize_expanded(parsed)
        return text

    if isinstance(payload, dict):
        if "expanded" in payload:
            inner = payload["expanded"]
            if isinstance(inner, str):
                return normalize_expanded(inner)
            if isinstance(inner, dict):
                return structure_to_markdown(inner)
            return normalize_expanded(inner)
        return structure_to_markdown(payload)

    if isinstance(payload, list):
        return "\n".join(f"- {x}" for x in payload)

    return str(payload).strip()


def _section_body_from_payload(payload: Any, section: str) -> str:
    """Pull a single section body from JSON or plain text."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if _looks_like_raw_json(text):
            try:
                parsed = json.loads(text.replace("'", '"'))
            except Exception:
                try:
                    import ast

                    parsed = ast.literal_eval(text)
                except Exception:
                    return text
            return _section_body_from_payload(parsed, section)
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("#"):
            return "\n".join(lines[1:]).strip() or text
        return text
    if isinstance(payload, dict):
        for key in (section, section.title(), _title(section), "body", "content", "text"):
            if key in payload and payload[key] is not None:
                val = payload[key]
                if isinstance(val, str):
                    return val.strip()
                return "\n".join(_value_to_lines(val)).strip()
        if "expanded" in payload:
            return _section_body_from_payload(payload["expanded"], section)
        return "\n".join(_value_to_lines(payload)).strip()
    if isinstance(payload, list):
        return "\n".join(f"- {x}" for x in payload)
    return str(payload).strip()


async def _expand_one_section(
    grounded: str,
    section: str,
    sem: asyncio.Semaphore,
) -> tuple[str, str]:
    """Expand a single brief section concurrently; returns (section_key, markdown_body)."""
    hint = _SECTION_HINTS.get(section, f"Write the {_title(section)} section for this product.")
    async with sem:
        try:
            data = await llm_client.complete_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You expand ONE section of a product brief for an IDE agent studio. "
                            'Return JSON only: {"section": "<name>", "body": "<markdown without ## heading>"}. '
                            "Keep body concrete (about 4–10 lines). No frontmatter. "
                            "Never return the seed unchanged — write this section's content only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Seed idea:\n{grounded}\n\n"
                            f"Section to write: {_title(section)} ({section})\n"
                            f"Guidance: {hint}\n\n"
                            'Return JSON {"section": "'
                            + section
                            + '", "body": "..."} now.'
                        ),
                    },
                ],
                max_tokens=550,
                temperature=0.35,
                retries=0,
                timeout=90.0,
            )
            body = _section_body_from_payload(data, section)
            if not body and isinstance(data, dict):
                body = str(data.get("body") or data.get("content") or "").strip()
            body = re.sub(r"^#+\s*.*\n+", "", body or "").strip()
            if not body:
                body = f"_Could not expand {_title(section)}; refine the seed and retry._"
            return section, body
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parallel brief section %s failed: %s", section, exc)
            return section, f"_Section {_title(section)} unavailable ({exc})._"


def _assemble_brief(results: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    usable = 0
    by_key = {k: v for k, v in results}
    for section in _SECTION_ORDER:
        text = (by_key.get(section) or "").strip() or "_TBD_"
        if not text.startswith("_Section ") and not text.startswith("_Could not expand"):
            usable += 1
        parts.append(f"## {_title(section)}")
        parts.append("")
        parts.append(text)
        parts.append("")
    expanded = "\n".join(parts).strip()
    if not expanded:
        raise ValueError("Expansion returned empty text")
    if usable == 0:
        raise ValueError(
            "All brief sections failed (often expired AWS/Bedrock credentials). "
            "Refresh credentials and retry Expand."
        )
    if _looks_like_raw_json(expanded):
        expanded = normalize_expanded(expanded)
    return expanded


async def expand_problem_statement(
    seed: str,
    *,
    on_section: SectionCallback | None = None,
) -> str:
    """
    Expand a seed into a full markdown brief via parallel async LLM calls.

    All Product / Problem / Goals / Stack / Domains / Constraints /
    Integrations / Success sections run concurrently (asyncio.gather).
    Optional on_section(section, body) is awaited as each section finishes.
    """
    seed = (seed or "").strip()
    if not seed:
        raise ValueError("Seed is empty")

    grounded = seed_for_expansion(seed)
    # True parallel: every section may run at once
    sem = asyncio.Semaphore(len(_SECTION_ORDER))

    async def _run(section: str) -> tuple[str, str]:
        key, body = await _expand_one_section(grounded, section, sem)
        if on_section is not None:
            maybe = on_section(key, body)
            if asyncio.iscoroutine(maybe):
                await maybe
        return key, body

    logger.info(
        "brief_expand_parallel start sections=%s mode=async_gather",
        len(_SECTION_ORDER),
    )
    results = await asyncio.gather(*[_run(section) for section in _SECTION_ORDER])
    expanded = _assemble_brief(list(results))
    logger.info("brief_expand_parallel done chars=%s", len(expanded))
    return expanded


async def expand_problem_statement_sequential_fallback(seed: str) -> str:
    """Legacy single-call expand (kept for tests / emergency fallback)."""
    seed = (seed or "").strip()
    if not seed:
        raise ValueError("Seed is empty")
    system = (
        _PROMPT.read_text(encoding="utf-8")
        if _PROMPT.exists()
        else 'Expand the seed into markdown brief sections. Return JSON {"expanded": "markdown"}.'
    )
    data = await llm_client.complete_json(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"Seed idea:\n{seed}\n\n"
                    'Return ONE JSON object: {"expanded": "<markdown string>"}.\n'
                    "The value of expanded MUST be a markdown STRING with ## headings "
                    "(Product, Problem, Goals, Stack, Domains, Constraints, Integrations, Success). "
                    "Do NOT put nested JSON objects inside expanded."
                ),
            },
        ],
        max_tokens=3500,
        temperature=0.2,
    )
    expanded = normalize_expanded(data)
    if not expanded:
        raise ValueError("Expansion returned empty text")
    if _looks_like_raw_json(expanded):
        expanded = normalize_expanded(expanded)
    return expanded
