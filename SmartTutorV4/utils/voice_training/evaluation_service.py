"""Assessment scoring for voice training sessions."""

from __future__ import annotations

from utils.timezone_helpers import now_ist

from utils.voice_training.assessment_helpers import coerce_assessment_result
from utils.voice_training.config import get_voice_training_settings
from utils.voice_training.evaluation_context import (
    PROFESSIONAL_ANTI,
    analyze_trainee_turns,
    check_sop_contextual,
    infer_scenario_resolved,
    score_contextual_empathy,
    score_contextual_politeness,
    score_from_turn_analysis,
)
from utils.voice_training.evaluation_rubric import RUBRIC, score_to_grade
from utils.voice_training.scenario_repository import get_scenario
from utils.voice_training.script_turns import trainee_lines_from_script
from utils.voice_training.schemas import (
    AssessmentResult,
    ConversationTurn,
    MetricScore,
    SentimentLevel,
    SessionMode,
)
from utils.voice_training.storage import VoiceTrainingStore, new_id
from utils.voice_training.transcript_summary import build_transcript_summary


class VoiceEvaluationService:
    def __init__(self, store: VoiceTrainingStore):
        self.store = store
        self.settings = get_voice_training_settings()

    def evaluate_session(self, session_id: str, user_id: str) -> AssessmentResult:
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")
        if session.get("user_id") != user_id:
            raise ValueError("Session does not belong to this user")
        if session.get("mode") != "assessment":
            raise ValueError("Only assessment sessions can be scored. Practice sessions cannot be evaluated.")
        if session.get("status") == "cancelled":
            raise ValueError("This assessment was cancelled and cannot be scored.")

        existing = self.store.get_assessment_by_session(session_id)
        if existing:
            return coerce_assessment_result(existing)

        scenario = get_scenario(self.store, session["scenario_id"], persist_repair=False)
        if not scenario:
            raise ValueError(f"Scenario {session['scenario_id']} not found")

        conversation = [ConversationTurn(**t) for t in session.get("conversation", [])]
        trainee_turns = [t for t in conversation if t.speaker == "trainee"]
        if not trainee_turns:
            raise ValueError(
                "Cannot evaluate an assessment with no agent responses. "
                "Use Cancel Assessment to exit without a score, or complete at least your opening greeting."
            )
        if not self._customer_was_answered(conversation):
            raise ValueError(
                "Cannot evaluate an assessment where the customer's issue was never answered. "
                "Reply to the customer at least once, or use Cancel Assessment to exit without a score."
            )
        trainee_text = " ".join(t.text.lower() for t in trainee_turns)

        turn_analysis = analyze_trainee_turns(conversation, scenario.success_script)
        turn_scores = score_from_turn_analysis(turn_analysis)
        sop_activities = check_sop_contextual(scenario.sop_activities, conversation)
        sop_rate = sum(1 for a in sop_activities if a.completed) / max(len(sop_activities), 1)
        issue_resolved = infer_scenario_resolved(scenario, trainee_text, conversation)

        expected_trainee_turns = len(trainee_lines_from_script(scenario.success_script))
        completion_ratio = self._completion_ratio(
            session, len(trainee_turns), expected_trainee_turns, sop_rate
        )

        metrics = self._score_metrics(
            trainee_text, trainee_turns, conversation, sop_rate, session, turn_scores, turn_analysis,
            issue_resolved=issue_resolved,
            completion_ratio=completion_ratio,
        )
        final_score = sum(m.weighted_score for m in metrics)
        final_percentage = (final_score / 5.0) * 100

        sentiment_start = scenario.opening_sentiment
        sentiment_end = self._infer_sentiment_end(conversation, sentiment_start)
        sentiment_resolved = bool(trainee_turns) and (
            sentiment_end == SentimentLevel.SATISFIED
            and sentiment_start != sentiment_end
        )
        fcr = self._fcr_achieved(
            sop_rate, metrics, sentiment_end, issue_resolved, sentiment_resolved
        )
        remarks = self._build_remarks(
            metrics,
            sop_activities,
            sentiment_resolved,
            fcr,
            turn_analysis,
            completion_ratio=completion_ratio,
            trainee_turn_count=len(trainee_turns),
            expected_trainee_turns=expected_trainee_turns,
        )
        grade = score_to_grade(final_percentage)
        transcript_summary = build_transcript_summary(
            conversation,
            scenario.title,
            sop_activities,
            metrics,
            sentiment_start,
            sentiment_end,
            turn_analysis,
            sop_rate,
            fcr,
            grade,
            final_percentage,
            completion_ratio=completion_ratio,
            expected_trainee_turns=expected_trainee_turns,
        )

        assessment_id = new_id("asm-")
        result = AssessmentResult(
            assessment_id=assessment_id,
            session_id=session_id,
            user_id=user_id,
            scenario_id=scenario.id,
            scenario_title=scenario.title,
            category=scenario.category,
            mode=SessionMode(session.get("mode", "assessment")),
            metrics=metrics,
            final_score=round(final_score, 2),
            final_percentage=round(final_percentage, 1),
            grade=grade,
            remarks=remarks,
            sop_compliance_rate=round(sop_rate * 100, 1),
            sentiment_start=sentiment_start,
            sentiment_end=sentiment_end,
            sentiment_resolved=sentiment_resolved,
            fcr_achieved=fcr,
            conversation=conversation,
            metadata={
                "sop_activities": [a.model_dump(mode="json") for a in sop_activities],
                "trainee_turn_count": len(trainee_turns),
                "total_turns": len(conversation),
                "expected_trainee_turns": expected_trainee_turns,
                "completion_ratio": round(completion_ratio, 3),
                "call_completed": bool(session.get("conversation_complete")),
                "turn_analysis": turn_analysis,
                "avg_context_match_pct": turn_scores["avg_context"],
                "avg_combined_match_pct": turn_scores.get("avg_combined", turn_scores["avg_context"]),
                "evaluation_mode": "contextual",
                "evaluated_at": now_ist().isoformat(),
                "transcript_summary": transcript_summary,
            },
        )
        return result

    @staticmethod
    def _customer_was_answered(conversation: list[ConversationTurn]) -> bool:
        """True once the agent has replied to something the customer actually said.

        An assessment can be submitted early - a real caller can hang up - but an opening
        greeting on its own is not a call. Scoring a session where the customer stated the
        issue and never got an answer (or never spoke at all) reports a score for a
        conversation that never happened, so those sessions are refused instead.
        """
        first_customer = next(
            (i for i, turn in enumerate(conversation) if turn.speaker == "customer"), None
        )
        if first_customer is None:
            return False
        return any(turn.speaker == "trainee" for turn in conversation[first_customer + 1:])

    @staticmethod
    def _completion_ratio(
        session: dict,
        trainee_turn_count: int,
        expected_trainee_turns: int,
        sop_rate: float,
    ) -> float:
        """How much of the call the trainee actually handled, as 0.0-1.0.

        analyze_trainee_turns() only produces an entry per turn the trainee took, so turns
        that were never attempted are invisible to the per-turn scores. This ratio is what
        makes them count. It is 1.0 - i.e. scoring is unchanged - whenever the conversation
        engine marked the call complete (all scripted customer beats delivered and the
        exchange closed), whenever the trainee covered every scripted agent line, or when the
        scenario has no scripted agent lines to measure against. SOP coverage is taken as a
        floor so a trainee who resolved the issue in fewer words than the script is not
        penalised for being concise.
        """
        if session.get("conversation_complete"):
            return 1.0
        if expected_trainee_turns <= 0:
            return 1.0
        turn_ratio = trainee_turn_count / expected_trainee_turns
        return max(0.0, min(1.0, max(turn_ratio, sop_rate)))

    def _score_metrics(
        self,
        trainee_text: str,
        trainee_turns: list[ConversationTurn],
        conversation: list[ConversationTurn],
        sop_rate: float,
        session: dict,
        turn_scores: dict,
        turn_analysis: list[dict],
        issue_resolved: bool = False,
        completion_ratio: float = 1.0,
    ) -> list[MetricScore]:
        weights = {
            "weight_intent": self.settings.weight_intent,
            "weight_resolution": self.settings.weight_resolution,
            "weight_sop": self.settings.weight_sop,
            "weight_tone": self.settings.weight_tone,
            "weight_voice": self.settings.weight_voice,
            "weight_dialogue": self.settings.weight_dialogue,
            "weight_performance": self.settings.weight_performance,
        }
        # Politeness and empathy are scored contextually below (score_contextual_*), so only the
        # unprofessional-language count is still derived from flat marker matching.
        unprofessional = sum(1 for m in PROFESSIONAL_ANTI if m in trainee_text)
        avg_turn_pass = sum(1 for t in turn_analysis if t.get("passed")) / max(len(turn_analysis), 1)
        contextual_empathy = score_contextual_empathy(conversation)
        contextual_politeness = score_contextual_politeness(conversation)

        scores_map = {
            "Intent Accuracy": turn_scores["intent"],
            "Issue Resolution": min(
                5.0,
                2.0 + sop_rate * 2.0 + avg_turn_pass * 1.0 + (0.6 if issue_resolved else 0.0),
            ),
            "Process Adherence": min(5.0, 1.0 + sop_rate * 4.0),
            "Hallucination Control": 5 if "policy" not in trainee_text or "according" in trainee_text else 4,
            "Politeness": contextual_politeness,
            "Empathy": contextual_empathy,
            "Professionalism": max(1.0, 5.0 - unprofessional),
            "Speech Clarity": max(1.0, float(session.get("voice_metrics", {}).get("clarity", 4) or 4)),
            "Speech Pace": max(1.0, float(session.get("voice_metrics", {}).get("pace", 4) or 4)),
            "Conversation Flow": turn_scores["dialogue"],
            "Response Time": max(1.0, float(session.get("voice_metrics", {}).get("response_time", 4) or 4)),
        }

        category_counts: dict[str, int] = {}
        for item in RUBRIC:
            cat = item["category"]
            category_counts[cat] = category_counts.get(cat, 0) + 1

        category_weights: dict[str, float] = {}
        for item in RUBRIC:
            wk = item["weight_key"]
            category_weights[item["parameter"]] = weights[wk] / category_counts[item["category"]]

        metrics: list[MetricScore] = []
        for item in RUBRIC:
            param = item["parameter"]
            raw = float(scores_map.get(param, 3))
            raw = max(1.0, min(5.0, raw))
            # Every parameter above has a generous floor (a default of 4 for the voice metrics,
            # 5 for Hallucination Control on a call that quoted no policy, and so on), which is
            # fair for a call that was actually handled and far too kind to one that was
            # abandoned. Scaling the credit above the 1.0 floor by how much of the call happened
            # is a no-op at completion_ratio 1.0, so completed calls score exactly as before.
            if completion_ratio < 1.0:
                raw = 1.0 + (raw - 1.0) * completion_ratio
            weight = category_weights[param]
            weighted = raw * weight
            passed = raw >= 3
            remark = self._metric_remark(param, raw, passed, turn_scores, turn_analysis)
            metrics.append(
                MetricScore(
                    category=item["category"],
                    parameter=param,
                    score=round(raw, 1),
                    weight=round(weight, 4),
                    weighted_score=round(weighted, 3),
                    remarks=remark,
                    passed=passed,
                )
            )
        return metrics

    def _infer_sentiment_end(
        self,
        conversation: list[ConversationTurn],
        sentiment_start: SentimentLevel | None = None,
    ) -> SentimentLevel:
        customer_turns = [t.text.lower() for t in conversation if t.speaker == "customer"]
        if not customer_turns:
            return sentiment_start or SentimentLevel.NEUTRAL
        customer_text = " ".join(customer_turns[-3:])
        if any(w in customer_text for w in ["thank", "great", "perfect", "appreciate", "works", "sounds good"]):
            return SentimentLevel.SATISFIED
        if any(w in customer_text for w in ["unacceptable", "manager", "cancel", "angry", "still waiting", "not resolved"]):
            return SentimentLevel.FRUSTRATED
        return SentimentLevel.NEUTRAL

    def _fcr_achieved(
        self,
        sop_rate: float,
        metrics: list[MetricScore],
        sentiment_end: SentimentLevel,
        issue_resolved: bool,
        sentiment_resolved: bool,
    ) -> bool:
        issue_res = next((m.score for m in metrics if m.parameter == "Issue Resolution"), 0.0)
        if sop_rate >= 0.8 and issue_res >= 4:
            return True
        if (
            sop_rate >= 0.6
            and sentiment_resolved
            and sentiment_end == SentimentLevel.SATISFIED
            and issue_resolved
            and issue_res >= 3.0
        ):
            return True
        return False

    def _metric_remark(
        self, param: str, score: float, passed: bool, turn_scores: dict, turn_analysis: list[dict]
    ) -> str:
        ctx = turn_scores.get("avg_combined", turn_scores.get("avg_context", 0))
        if param == "Intent Accuracy":
            return (
                f"Contextual intent match averaged {round(ctx, 1)}% across your replies "
                f"(paraphrasing allowed — exact wording not required)."
            )
        if param == "Conversation Flow":
            passed_turns = sum(1 for t in turn_analysis if t.get("passed"))
            total = max(len(turn_analysis), 1)
            return f"Dialogue flow: {passed_turns}/{total} turns met contextual expectations."
        if passed:
            return f"{param}: Meets expectations (score {round(score, 1)}/5)."
        return f"{param}: Needs improvement (score {round(score, 1)}/5). Review training materials."

    def _build_remarks(
        self,
        metrics: list[MetricScore],
        sop: list,
        sentiment_resolved: bool,
        fcr: bool,
        turn_analysis: list[dict],
        completion_ratio: float = 1.0,
        trainee_turn_count: int = 0,
        expected_trainee_turns: int = 0,
    ) -> str:
        parts = []
        if completion_ratio < 1.0:
            covered = f"{trainee_turn_count} of {expected_trainee_turns}" if expected_trainee_turns else "part"
            parts.append(
                f"Incomplete call: you answered {covered} of the scenario's agent turns before "
                f"submitting, so every score was reduced to {completion_ratio * 100:.0f}% of the "
                "credit a completed call earns. Work through the whole conversation next time."
            )
        if turn_analysis:
            avg = sum(t["combined_pct"] for t in turn_analysis) / len(turn_analysis)
            parts.append(
                f"Contextual evaluation: {avg:.0f}% average match — "
                "wording may differ from the reference script if intent is correct."
            )
        weak = [m.parameter for m in metrics if not m.passed]
        if weak:
            parts.append(f"Focus areas: {', '.join(weak)}.")
        missed_sop = [a.name for a in sop if a.required and not a.completed]
        if missed_sop:
            parts.append(f"Missed SOP steps: {', '.join(missed_sop)}.")
        parts.append(
            "Customer sentiment improved by end of call."
            if sentiment_resolved
            else "Customer sentiment did not fully resolve."
        )
        parts.append("First Contact Resolution achieved." if fcr else "FCR not achieved — review resolution path.")
        return " ".join(parts)
