"""Build human-readable call summaries from conversation transcripts."""

from __future__ import annotations

import re
from typing import Any

from utils.voice_training.schemas import ConversationTurn, MetricScore, SOPActivity, SentimentLevel


def _truncate(text: str, max_len: int = 120) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rsplit(" ", 1)[0] + "…"


def _first_customer_issue(conversation: list[ConversationTurn]) -> str:
    for turn in conversation:
        if turn.speaker == "customer" and turn.text.strip():
            return _truncate(turn.text, 140)
    return "Customer contacted support with an account or service issue."


def _last_customer_line(conversation: list[ConversationTurn]) -> str:
    for turn in reversed(conversation):
        if turn.speaker == "customer" and turn.text.strip():
            return _truncate(turn.text, 100)
    return ""


def _agent_actions(trainee_turns: list[ConversationTurn]) -> list[str]:
    actions: list[str] = []
    keywords = [
        ("verify", "Verified customer identity or account details"),
        ("record", "Stated call recording disclosure"),
        ("apolog", "Acknowledged customer frustration"),
        ("understand", "Showed empathy and active listening"),
        ("walk you through", "Guided customer through resolution steps"),
        ("activate", "Assisted with account activation"),
        ("reset", "Helped reset credentials or access"),
        ("escalat", "Escalated or opened a ticket"),
        ("summar", "Summarized resolution and next steps"),
        ("anything else", "Checked for additional needs before closing"),
    ]
    combined = " ".join(t.text.lower() for t in trainee_turns)
    for pattern, label in keywords:
        if pattern in combined and label not in actions:
            actions.append(label)
    if not actions and trainee_turns:
        actions.append(f"Responded across {len(trainee_turns)} agent turns")
    return actions[:5]


def build_transcript_summary(
    conversation: list[ConversationTurn],
    scenario_title: str,
    sop_activities: list[SOPActivity],
    metrics: list[MetricScore],
    sentiment_start: SentimentLevel,
    sentiment_end: SentimentLevel,
    turn_analysis: list[dict[str, Any]],
    sop_rate: float,
    fcr: bool,
    final_grade: str,
    final_pct: float,
    completion_ratio: float = 1.0,
    expected_trainee_turns: int = 0,
) -> dict[str, Any]:
    trainee_turns = [t for t in conversation if t.speaker == "trainee"]
    customer_turns = [t for t in conversation if t.speaker == "customer"]
    opening = _first_customer_issue(conversation)
    closing = _last_customer_line(conversation)

    completed_sop = [a.name for a in sop_activities if a.completed]
    missed_sop = [a.name for a in sop_activities if a.required and not a.completed]
    passed_metrics = [m.parameter for m in metrics if m.passed]
    failed_metrics = [m.parameter for m in metrics if not m.passed]

    avg_ctx = 0.0
    if turn_analysis:
        avg_ctx = sum(t.get("combined_pct", 0) for t in turn_analysis) / len(turn_analysis)

    narrative_parts = [
        f"The customer reached out regarding {scenario_title}. "
        f"They opened with: \"{opening}\"",
        f"Over {len(conversation)} messages ({len(trainee_turns)} agent replies, "
        f"{len(customer_turns)} customer messages), the call moved from "
        f"{sentiment_start.value} to {sentiment_end.value} sentiment.",
    ]
    if avg_ctx:
        narrative_parts.append(
            f"Contextual evaluation averaged {avg_ctx:.0f}% intent match "
            f"(paraphrasing allowed — exact script wording not required)."
        )
    incomplete = completion_ratio < 1.0
    if incomplete:
        scripted = (
            f" of the {expected_trainee_turns} agent turns the scenario scripts"
            if expected_trainee_turns
            else ""
        )
        narrative_parts.append(
            f"The call was submitted before it finished: {len(trainee_turns)} agent replies were "
            f"given{scripted}, so scores carry only {completion_ratio * 100:.0f}% of the credit a "
            "completed call earns."
        )
    narrative_parts.append(
        f"Final score: {final_pct:.0f}% (Grade {final_grade}). "
        f"SOP compliance was {sop_rate * 100:.0f}%"
        + (" with First Contact Resolution achieved." if fcr else "; FCR was not fully achieved.")
    )
    if closing:
        narrative_parts.append(f"The customer's last message: \"{closing}\"")

    highlights: list[str] = []
    if incomplete:
        highlights.append(
            "Call ended early — "
            + (
                f"{len(trainee_turns)}/{expected_trainee_turns} agent turns answered"
                if expected_trainee_turns
                else f"only {len(trainee_turns)} agent replies given"
            )
        )
    if completed_sop:
        highlights.append(
            f"Completed SOP steps: {', '.join(completed_sop[:3])}"
            + ("…" if len(completed_sop) > 3 else "")
        )
    if missed_sop:
        highlights.append(f"Missed required steps: {', '.join(missed_sop)}")
    if passed_metrics:
        highlights.append(f"Strong areas: {', '.join(passed_metrics[:4])}")
    if failed_metrics:
        highlights.append(f"Improve: {', '.join(failed_metrics[:4])}")
    if turn_analysis:
        passed_turns = sum(1 for t in turn_analysis if t.get("passed"))
        highlights.append(f"Contextual turns passed: {passed_turns}/{len(turn_analysis)}")

    preview = []
    for i, turn in enumerate(conversation):
        preview.append(
            {
                "turn": i + 1,
                "speaker": turn.speaker,
                "text": turn.text,
                "preview": _truncate(turn.text, 90),
            }
        )

    return {
        "narrative": " ".join(narrative_parts),
        "highlights": highlights,
        # A call the trainee walked away from is not "partially resolved" - it is unfinished,
        # and the outcome line is the first thing a trainer reads on the report.
        "call_outcome": "Incomplete - call not finished" if incomplete else (
            "Resolved" if fcr and sentiment_end == SentimentLevel.SATISFIED else (
                "Partially resolved" if sentiment_end != SentimentLevel.FRUSTRATED else "Needs follow-up"
            )
        ),
        "key_actions": _agent_actions(trainee_turns),
        "transcript_preview": preview,
        "opening_issue": opening,
        "closing_customer": closing,
        "agent_response_count": len(trainee_turns),
        "customer_message_count": len(customer_turns),
    }
