"""Normalize stored assessment JSON before PDF / API validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from utils.voice_training.schemas import AssessmentResult, ScenarioCategory, SentimentLevel, SessionMode


def _coerce_sentiment(value: Any) -> SentimentLevel:
    if isinstance(value, SentimentLevel):
        return value
    if isinstance(value, dict):
        value = value.get("value") or value.get("name") or "neutral"
    if isinstance(value, str):
        try:
            return SentimentLevel(value.lower().strip())
        except ValueError:
            return SentimentLevel.NEUTRAL
    return SentimentLevel.NEUTRAL


def _coerce_category(value: Any) -> str:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ScenarioCategory.CUSTOM.value


def coerce_assessment_result(data: dict) -> AssessmentResult:
    """Coerce DB-stored assessment payload into a valid AssessmentResult."""
    payload = dict(data)
    for extra in (
        "pdf_s3_key",
        "pdf_url",
        "process_name",
        "user_email",
        "user_role",
        "author_email",
        "author_role",
        "scenario_title_dup",
    ):
        payload.pop(extra, None)

    payload["sentiment_start"] = _coerce_sentiment(payload.get("sentiment_start"))
    payload["sentiment_end"] = _coerce_sentiment(payload.get("sentiment_end"))
    payload["category"] = _coerce_category(payload.get("category"))

    mode = payload.get("mode")
    if isinstance(mode, str):
        try:
            payload["mode"] = SessionMode(mode)
        except ValueError:
            payload["mode"] = SessionMode.ASSESSMENT

    if payload.get("created_at") and isinstance(payload["created_at"], str):
        try:
            payload["created_at"] = datetime.fromisoformat(
                payload["created_at"].replace("Z", "+00:00")
            )
        except ValueError:
            payload.pop("created_at", None)

    conversation = []
    for turn in payload.get("conversation") or []:
        if not isinstance(turn, dict):
            continue
        row = dict(turn)
        ts = row.get("timestamp")
        if isinstance(ts, str):
            try:
                row["timestamp"] = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                row.pop("timestamp", None)
        conversation.append(row)
    payload["conversation"] = conversation

    metrics = []
    for m in payload.get("metrics") or []:
        if not isinstance(m, dict):
            continue
        row = dict(m)
        score = float(row.get("score") or 1)
        row["score"] = max(1.0, min(5.0, score))
        row["max_score"] = float(row.get("max_score") or 5)
        row["weight"] = float(row.get("weight") or 0)
        row["weighted_score"] = float(row.get("weighted_score") or 0)
        row["passed"] = bool(row.get("passed", row["score"] >= 3))
        metrics.append(row)
    payload["metrics"] = metrics

    payload["final_score"] = max(0.0, min(5.0, float(payload.get("final_score") or 0)))
    payload["final_percentage"] = max(0.0, min(100.0, float(payload.get("final_percentage") or 0)))
    payload["sop_compliance_rate"] = float(payload.get("sop_compliance_rate") or 0)
    payload["metadata"] = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

    return AssessmentResult.model_validate(payload)
