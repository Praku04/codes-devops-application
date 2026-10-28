"""Helpers for scenario API responses."""

from datetime import datetime

from utils.timezone_helpers import format_ist_iso
from utils.voice_training.schemas import ScenarioScript


def format_utc_iso(dt: datetime | None) -> str | None:
    """Backward-compatible alias — values are serialized in IST."""
    return format_ist_iso(dt)


def scenario_to_summary(scenario: ScenarioScript) -> dict:
    generated_at = getattr(scenario, "generated_at", None)
    label = scenario.custom_category_label
    if not label and scenario.category.value == "custom":
        label = "Custom"
    return {
        "id": scenario.id,
        "category": scenario.category.value,
        "custom_category_label": label,
        "title": scenario.title,
        "description": scenario.description,
        "customer_persona": scenario.customer_persona,
        "opening_sentiment": scenario.opening_sentiment.value,
        "sop_count": len(scenario.sop_activities),
        "turn_count": len(scenario.success_script),
        "generated_at": format_utc_iso(generated_at),
        "archived": bool(getattr(scenario, "archived", False)),
        "archived_at": format_utc_iso(getattr(scenario, "archived_at", None)),
    }
