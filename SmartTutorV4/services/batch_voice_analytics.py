"""Voice training metrics for batch management (courses + voice share TraineeBatch)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from models.domain import VoiceAssessment, VoiceScenario, VoiceSession
from utils.voice_training.publish import get_assessment_config, get_publish_config


def _scenario_content(row: VoiceScenario) -> dict[str, Any]:
    return row.content if isinstance(row.content, dict) else {}


def _assessment_score(row: VoiceAssessment) -> float:
    content = row.content if isinstance(row.content, dict) else {}
    return float(row.final_percentage or content.get("final_percentage") or 0)


def _assessment_grade(row: VoiceAssessment) -> str:
    content = row.content if isinstance(row.content, dict) else {}
    return str(row.grade or content.get("grade") or "")


def list_scenarios_published_to_batch(
    db: Session,
    batch_id: str,
    process_id: str | None = None,
) -> list[dict[str, Any]]:
    """Published voice scenarios whose audience includes this batch."""
    query = db.query(VoiceScenario)
    if process_id:
        query = query.filter(VoiceScenario.process_id == process_id)
    scenarios: list[dict[str, Any]] = []
    for row in query.all():
        publish = get_publish_config(_scenario_content(row))
        if not publish.get("is_published"):
            continue
        batch_ids = list(publish.get("batch_ids") or [])
        if batch_id not in batch_ids:
            continue
        assessment_cfg = get_assessment_config(_scenario_content(row))
        scenarios.append(
            {
                "id": row.id,
                "title": row.title,
                "category": row.category,
                "min_practice_sessions": assessment_cfg.get("min_practice_sessions", 1),
                "max_assessment_attempts": assessment_cfg.get("max_assessment_attempts", 5),
                "pass_percentage": assessment_cfg.get("pass_percentage", 70),
            }
        )
    return scenarios


def get_trainee_voice_scenario_progress(
    db: Session,
    trainee_id: str,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Per-trainee progress for one published voice scenario."""
    scenario_id = scenario["id"]
    pass_pct = int(scenario.get("pass_percentage") or 70)
    min_practice = int(scenario.get("min_practice_sessions") or 1)

    practice_completed = (
        db.query(VoiceSession)
        .filter(
            VoiceSession.user_id == trainee_id,
            VoiceSession.scenario_id == scenario_id,
            VoiceSession.mode == "practice",
            VoiceSession.status == "completed",
        )
        .count()
    )

    assessments = (
        db.query(VoiceAssessment)
        .filter(
            VoiceAssessment.user_id == trainee_id,
            VoiceAssessment.scenario_id == scenario_id,
        )
        .order_by(VoiceAssessment.created_at.asc())
        .all()
    )

    attempt_details: list[dict[str, Any]] = []
    best_score: float | None = None
    has_passed = False
    for index, assessment in enumerate(assessments, start=1):
        score = round(_assessment_score(assessment), 1)
        passed = score >= pass_pct
        best_score = score if best_score is None else max(best_score, score)
        if passed:
            has_passed = True
        attempt_details.append(
            {
                "attempt_number": index,
                "score": score,
                "grade": _assessment_grade(assessment),
                "passed": passed,
                "created_at": assessment.created_at.isoformat() if assessment.created_at else None,
            }
        )

    assessment_unlocked = practice_completed >= min_practice
    if practice_completed == 0 and not assessments:
        status = "not_started"
    elif not assessment_unlocked:
        status = "practice_in_progress"
    elif has_passed:
        status = "passed"
    elif assessments:
        status = "assessment_in_progress"
    else:
        status = "assessment_ready"

    return {
        "id": scenario_id,
        "title": scenario["title"],
        "category": scenario.get("category"),
        "practice_completed": practice_completed,
        "practice_required": min_practice,
        "assessment_unlocked": assessment_unlocked,
        "assessment_attempts": len(assessments),
        "max_assessment_attempts": scenario.get("max_assessment_attempts", 5),
        "pass_percentage": pass_pct,
        "best_score": best_score,
        "has_passed": has_passed,
        "assessment_frozen": has_passed,
        "status": status,
        "attempt_details": attempt_details,
    }


def build_trainee_voice_progress(
    db: Session,
    trainee_id: str,
    scenarios: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [get_trainee_voice_scenario_progress(db, trainee_id, s) for s in scenarios]


def voice_summary_lines_for_trainee(
    trainee_email: str,
    voice_items: list[dict[str, Any]],
    *,
    include_header: bool = True,
) -> list[str]:
    lines: list[str] = []
    if include_header:
        lines.extend([f"Trainee: {trainee_email}", ""])
    if not voice_items:
        lines.append("Voice Training: no scenarios published to this batch.")
        return lines
    lines.append("Voice Training scenarios:")
    for item in voice_items:
        status = item.get("status", "not_started")
        status_label = {
            "not_started": "Not Started",
            "practice_in_progress": "Practice In Progress",
            "assessment_ready": "Assessment Ready",
            "assessment_in_progress": "Assessment In Progress",
            "passed": "Passed",
        }.get(status, status)
        lines.append(
            f"  Scenario '{item['title']}': practice {item['practice_completed']}/{item['practice_required']}, "
            f"attempts {item['assessment_attempts']}/{item['max_assessment_attempts']}, "
            f"best={item['best_score']}%, status={status_label}"
        )
        for attempt in item.get("attempt_details", []):
            result = "PASS" if attempt.get("passed") else "FAIL"
            lines.append(
                f"    Attempt {attempt['attempt_number']}: {attempt['score']}% ({result}), grade={attempt.get('grade') or '—'}"
            )
    lines.append("")
    return lines


def enrich_batch_analytics_with_voice(
    db: Session,
    batch_id: str,
    process_id: str | None,
    analytics: dict[str, Any],
) -> dict[str, Any]:
    """Attach voice scenario catalog and per-trainee voice progress to batch analytics."""
    scenarios = list_scenarios_published_to_batch(db, batch_id, process_id)
    analytics["voice_scenarios"] = scenarios

    voice_scores: list[float] = []
    voice_passed_pairs = 0
    voice_active_trainees = 0

    for trainee in analytics.get("trainees", []):
        voice_items = build_trainee_voice_progress(db, trainee["id"], scenarios)
        trainee["voice_scenarios"] = voice_items
        trainee_has_voice_activity = False
        for item in voice_items:
            if item.get("practice_completed", 0) > 0 or item.get("assessment_attempts", 0) > 0:
                trainee_has_voice_activity = True
            if item.get("best_score") is not None:
                voice_scores.append(float(item["best_score"]))
            if item.get("has_passed"):
                voice_passed_pairs += 1
        if trainee_has_voice_activity:
            voice_active_trainees += 1

    summary = analytics.setdefault("summary", {})
    summary["total_voice_scenarios"] = len(scenarios)
    summary["avg_voice_score"] = (
        round(sum(voice_scores) / len(voice_scores), 1) if voice_scores else None
    )
    summary["voice_passed_count"] = voice_passed_pairs
    summary["voice_active_trainees"] = voice_active_trainees
    return analytics


def trainee_has_voice_activity(db: Session, trainee_id: str, scenario_ids: list[str]) -> bool:
    if not scenario_ids:
        return False
    practice_count = (
        db.query(VoiceSession)
        .filter(
            VoiceSession.user_id == trainee_id,
            VoiceSession.scenario_id.in_(scenario_ids),
            VoiceSession.mode == "practice",
            VoiceSession.status == "completed",
        )
        .count()
    )
    assessment_count = (
        db.query(VoiceAssessment)
        .filter(
            VoiceAssessment.user_id == trainee_id,
            VoiceAssessment.scenario_id.in_(scenario_ids),
        )
        .count()
    )
    return practice_count > 0 or assessment_count > 0
