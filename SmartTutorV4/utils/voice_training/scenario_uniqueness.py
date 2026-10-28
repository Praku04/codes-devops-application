"""Helpers to keep AI-generated voice training scenarios unique across users and runs."""

from __future__ import annotations

import re
from typing import Any

from utils.voice_training.schemas import ScenarioCategory, ScenarioScript
from utils.voice_training.fallback_scenarios import STANDARD_CATEGORIES

_WORD_RE = re.compile(r"[a-z0-9']+")
_SIMILARITY_THRESHOLD = 0.42
_MAX_AVOID_ENTRIES = 8

_VARIATION_HINTS = (
    "Use a different industry (healthcare portal, retail POS, SaaS dashboard, logistics fleet, hospitality booking).",
    "Use a different customer name and role — avoid common names already listed (e.g. John, Sarah, Michael).",
    "Use a different product or service and a different root cause (policy gap, misconfiguration, outage, duplicate charge).",
    "Use a different company size and channel (enterprise B2B, SMB, mobile app user, field technician).",
    "Use a different timeline and urgency (same-day deadline, multi-week billing cycle, weekend outage).",
)


def _tokenize(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall((text or "").lower()) if len(word) > 2}


def content_tokens(scenario: ScenarioScript | dict[str, Any]) -> set[str]:
    if isinstance(scenario, ScenarioScript):
        blob = f"{scenario.title} {scenario.description} {scenario.customer_persona}"
    else:
        blob = (
            f"{scenario.get('title', '')} {scenario.get('description', '')} "
            f"{scenario.get('customer_persona', '')}"
        )
    return _tokenize(blob)


def jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def title_exists(title: str, existing: list[ScenarioScript]) -> bool:
    normalized = normalize_title(title)
    if not normalized:
        return False
    return any(normalize_title(item.title) == normalized for item in existing)


def is_too_similar(
    candidate: ScenarioScript,
    existing: list[ScenarioScript],
    *,
    threshold: float = _SIMILARITY_THRESHOLD,
) -> bool:
    """Return True when candidate overlaps an existing scenario title or content."""
    if not existing:
        return False
    cand_title = normalize_title(candidate.title or "")
    cand_tokens = content_tokens(candidate)
    for item in existing:
        if cand_title and cand_title == normalize_title(item.title or ""):
            return True
        if jaccard_similarity(cand_tokens, content_tokens(item)) >= threshold:
            return True
    return False


def select_categories_for_count(
    count: int,
    existing: list[ScenarioScript],
) -> list[ScenarioCategory]:
    """Pick categories spread across the library; prefer categories with fewer scenarios."""
    count = max(1, min(5, count))
    counts_by_cat = {cat: 0 for cat in STANDARD_CATEGORIES}
    for scenario in existing:
        if scenario.category in counts_by_cat:
            counts_by_cat[scenario.category] += 1
    ordered = sorted(STANDARD_CATEGORIES, key=lambda cat: counts_by_cat[cat])
    selected: list[ScenarioCategory] = []
    while len(selected) < count:
        for cat in ordered:
            if len(selected) >= count:
                break
            selected.append(cat)
    return selected


def build_existing_scenarios_block(
    existing: list[ScenarioScript],
    category: ScenarioCategory,
    *,
    max_entries: int = _MAX_AVOID_ENTRIES,
) -> str:
    """Build a prompt block listing scenarios the model must not duplicate."""
    same_category = [s for s in existing if s.category == category]
    if not same_category:
        return ""

    lines = [
        "EXISTING SCENARIOS IN THIS CATEGORY (shared across all trainers/admins — do NOT duplicate):"
    ]
    for scenario in same_category[:max_entries]:
        title = (scenario.title or "")[:90]
        description = (scenario.description or "")[:140]
        persona = (scenario.customer_persona or "")[:90]
        lines.append(f"- Title: {title} | Problem: {description} | Customer: {persona}")

    other = [s for s in existing if s.category != category][:6]
    if other:
        lines.append("Other categories already cover these themes (avoid overlapping problems):")
        for scenario in other:
            lines.append(f"- [{scenario.category.value}] {(scenario.title or '')[:80]}")

    lines.extend(
        [
            "REQUIREMENTS FOR YOUR NEW SCENARIO:",
            "- Completely different customer name, company, product/service, and issue details.",
            "- Different resolution path and SOP focus than any title above.",
            "- opening_sentiment MUST be \"frustrated\" (customer starts frustrated or impatient).",
        ]
    )
    return "\n".join(lines)


def build_variation_hint(attempt: int) -> str:
    return _VARIATION_HINTS[attempt % len(_VARIATION_HINTS)]


def pick_least_similar_fallback(
    variants: list[dict[str, Any]],
    existing: list[ScenarioScript],
) -> dict[str, Any] | None:
    """Choose a built-in template that is not already in the library."""
    if not variants:
        return None

    scored: list[tuple[float, dict[str, Any]]] = []
    for variant in variants:
        probe = {
            "title": variant.get("title", ""),
            "description": variant.get("description", ""),
            "customer_persona": variant.get("customer_persona", ""),
        }
        if title_exists(probe["title"], existing):
            continue
        tokens = content_tokens(probe)
        max_sim = max(
            (jaccard_similarity(tokens, content_tokens(item)) for item in existing),
            default=0.0,
        )
        scored.append((max_sim, variant))

    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0])
    best_score, best = scored[0]
    if existing and best_score >= _SIMILARITY_THRESHOLD:
        return None
    return best
