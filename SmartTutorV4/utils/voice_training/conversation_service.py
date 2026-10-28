"""Voice session conversation logic."""

from __future__ import annotations

from utils.timezone_helpers import now_ist

from utils.voice_training.bedrock_service import VoiceBedrockService
from utils.voice_training.polly_service import VoicePollyService
from utils.voice_training.scenario_repository import get_scenario
from utils.voice_training.evaluation_context import (
    check_sop_contextual,
    dedupe_repeated_speech,
    map_sop_to_script_cues,
)
from utils.voice_training.schemas import ConversationTurn, SessionMode
from utils.voice_training.script_turns import (
    count_speakers,
    ensure_customer_intro,
    is_intro_only_customer,
    last_message_from,
    last_speaker,
    next_customer_line,
    next_trainee_line,
    repair_dangling_text,
    repair_mid_sentence_agent_line,
    resolve_known_customer_name,
    sanitize_agent_opening,
    should_hold_for_script_drift,
    should_mark_conversation_complete,
)
from utils.voice_training.storage import VoiceTrainingStore, new_id


class VoiceConversationService:
    def __init__(
        self,
        store: VoiceTrainingStore,
        bedrock: VoiceBedrockService,
        polly: VoicePollyService,
    ):
        self.store = store
        self.bedrock = bedrock
        self.polly = polly

    def start_session(self, user_id: str, scenario_id: str, mode: SessionMode) -> dict:
        scenario = get_scenario(self.store, scenario_id)
        if not scenario:
            raise ValueError(f"Scenario {scenario_id} not found")
        if getattr(scenario, "archived", False):
            raise ValueError("This scenario has been archived. Restore it from the archive to practice again.")

        session_id = new_id("sess-")
        conversation: list[dict] = []

        session = {
            "session_id": session_id,
            "user_id": user_id,
            "scenario_id": scenario_id,
            "mode": mode.value,
            "conversation": conversation,
            "current_sentiment": scenario.opening_sentiment.value,
            "hints_enabled": mode == SessionMode.PRACTICE,
            "started_at": now_ist().isoformat(),
            "turn_count": 0,
            "voice_metrics": {"clarity": 4, "pace": 4, "response_time": 4},
            "status": "active",
        }
        self.store.save_session(session)
        return session

    def get_opening_agent_line(self, scenario_id: str) -> str | None:
        scenario = get_scenario(self.store, scenario_id)
        if not scenario:
            return None
        script_dicts = [t.model_dump(mode="json") for t in scenario.success_script]
        raw = next_trainee_line(script_dicts, 0)
        # Practice/assessment opening must match preview Agent (reference) #1 exactly.
        if raw and str(raw).strip():
            return str(raw).strip()
        return sanitize_agent_opening(
            "",
            scenario.description or "",
            scenario.customer_persona or "",
            script_dicts,
        )

    def get_opening_customer_line(self, scenario_id: str) -> str | None:
        """First customer line, verbatim from the script — same contract as the agent opening.

        It must match preview Customer (reference) #1 exactly, so nothing here rewrites or
        pads it with the scenario description.
        """
        scenario = get_scenario(self.store, scenario_id)
        if not scenario:
            return None
        script_dicts = [t.model_dump(mode="json") for t in scenario.success_script]
        line = next_customer_line(script_dicts, 0)
        if not line or not str(line).strip():
            return None
        return repair_dangling_text(str(line).strip())

    async def bootstrap_assessment_opening(self, session_id: str) -> dict:
        """Deprecated — agent opens the call in assessment (same as practice)."""
        session = self.store.get_session(session_id)
        return {
            "session": session,
            "customer_response": None,
            "audio": None,
            "sentiment": session.get("current_sentiment") if session else None,
            "guidance": self.get_session_display_state(session["scenario_id"], session) if session else {},
            "hints": [],
        }

    async def bootstrap_opening_exchange(self, session_id: str) -> dict:
        """Auto-send agent greeting, then first customer reply (practice & assessment)."""
        session = self.store.get_session(session_id)
        if not session or session.get("conversation"):
            return {
                "session": session,
                "customer_response": None,
                "audio": None,
                "agent_audio": None,
                "sentiment": session.get("current_sentiment") if session else None,
                "guidance": {},
                "hints": [],
            }

        opening = self.get_opening_agent_line(session["scenario_id"])
        if not opening:
            return {
                "session": session,
                "customer_response": None,
                "audio": None,
                "agent_audio": None,
                "sentiment": session.get("current_sentiment"),
                "guidance": {},
                "hints": [],
            }

        session["conversation"].append(
            ConversationTurn(speaker="trainee", text=opening).model_dump(mode="json")
        )
        session["turn_count"] = 1

        scenario = get_scenario(self.store, session["scenario_id"])
        if not scenario:
            raise ValueError("Scenario not found")

        # Same rule as process_trainee_message: the opening customer line comes from the
        # script verbatim so practice matches the scenario the trainee just read.
        scripted_customer = self.get_opening_customer_line(session["scenario_id"])
        if scripted_customer:
            response = {
                "text": scripted_customer,
                "sentiment": scenario.opening_sentiment.value,
            }
        else:
            scenario_ctx = scenario.model_dump(mode="json")
            response = await self.bedrock.generate_customer_response(
                scenario_ctx,
                session["conversation"],
                session["mode"],
                trainee_count=1,
            )
        session["conversation"].append(
            ConversationTurn(speaker="customer", text=response["text"]).model_dump(mode="json")
        )
        session["current_sentiment"] = response.get("sentiment", "neutral")

        agent_audio = await self.polly.synthesize(
            opening,
            session["user_id"],
            session_id,
            "agent-opening",
        )
        customer_audio = await self.polly.synthesize(
            response["text"],
            session["user_id"],
            session_id,
            "t1",
        )
        self.store.save_session(session)

        if session.get("hints_enabled"):
            guidance = self.get_practice_guidance(session["scenario_id"], session)
            hints = guidance.get("hints", [])
        else:
            guidance = self.get_session_display_state(session["scenario_id"], session)
            hints = []
        self.store.save_session(session)

        return {
            "session": session,
            "customer_response": response["text"],
            "audio": customer_audio,
            "agent_audio": agent_audio,
            "opening_agent_line": opening,
            "sentiment": session["current_sentiment"],
            "guidance": guidance,
            "hints": hints,
            "suggested_response": guidance.get("suggested_response") if session.get("hints_enabled") else None,
        }

    def _build_sop_state(self, scenario, session: dict) -> dict:
        """Compute SOP progress from conversation and persist on the session."""
        if session.get("conversation_complete"):
            completed_ids = {act.id for act in scenario.sop_activities}
        else:
            turns = [ConversationTurn(**t) for t in session.get("conversation", [])]
            checked = check_sop_contextual(scenario.sop_activities, turns)
            completed_ids = {a.id for a in checked if a.completed}

        current_step = self._current_sop_index(scenario, completed_ids, session)
        cues = map_sop_to_script_cues(scenario.success_script, scenario.sop_activities)
        cue_by_id = {c["sop_id"]: c for c in cues}
        progress = []
        for act in scenario.sop_activities:
            cue = cue_by_id.get(act.id) or {}
            progress.append({
                **act.model_dump(mode="json"),
                "completed": act.id in completed_ids,
                "script_cue": cue.get("script_line"),
                "script_index": cue.get("script_index"),
            })

        session["sop_progress"] = progress
        session["current_sop_step"] = current_step + 1
        session["sop_script_cues"] = cues

        return {
            "current_step": current_step + 1,
            "total_steps": len(scenario.sop_activities),
            "current_sop": scenario.sop_activities[current_step].model_dump(mode="json")
            if current_step < len(scenario.sop_activities)
            else None,
            "sop_progress": progress,
            "sop_script_cues": cues,
        }

    def get_practice_guidance(self, scenario_id: str, session: dict) -> dict:
        scenario = get_scenario(self.store, scenario_id)
        if not scenario:
            return {}

        script_dicts = [t.model_dump(mode="json") for t in scenario.success_script]
        trainee_count = count_speakers(session.get("conversation", []), "trainee")
        last_sp = last_speaker(session.get("conversation", []))
        last_customer = last_message_from(session.get("conversation", []), "customer")
        next_response = None
        if last_sp in (None, "customer"):
            raw = next_trainee_line(script_dicts, trainee_count)
            if raw and str(raw).strip():
                # Prefer the repaired timeline line (heals mid-sentence debris like "concern.").
                known = resolve_known_customer_name(
                    script_dicts,
                    scenario.customer_persona or "",
                    scenario.description or "",
                )
                next_response = repair_mid_sentence_agent_line(str(raw).strip(), known)
            elif trainee_count == 0 and not last_customer:
                next_response = sanitize_agent_opening(
                    "",
                    scenario.description or "",
                    scenario.customer_persona or "",
                    script_dicts,
                )
        sop_state = self._build_sop_state(scenario, session)
        current_step = (sop_state["current_step"] or 1) - 1
        completed_sop_ids = {a["id"] for a in sop_state["sop_progress"] if a.get("completed")}
        hints = []
        contextual_hint = self._contextual_hint(last_customer, next_response, current_step, scenario)

        if session.get("hints_enabled"):
            if contextual_hint:
                hints.append(contextual_hint)
            if next_response:
                hints.append(f'Suggested reply: "{next_response}"')
            for i, act in enumerate(scenario.sop_activities):
                if act.id not in completed_sop_ids:
                    hints.append(f"Step {i + 1}: {act.name} — {act.description}")
                    break

        return {
            **sop_state,
            "suggested_response": next_response,
            "last_customer_message": last_customer,
            "contextual_hint": contextual_hint,
            "hints": hints[:4],
        }

    def _contextual_hint(self, last_customer: str | None, next_response: str | None, step: int, scenario) -> str:
        if not last_customer:
            return (
                "You open the call — greet the customer warmly and ask how you can help. "
                "Do not apologize or mention their problem yet."
            )
        lower = last_customer.lower()
        sop_name = scenario.sop_activities[step].name if step < len(scenario.sop_activities) else "Close the call"
        if is_intro_only_customer(last_customer):
            return f"Customer introduced themselves — greet them and ask how you can help. Do not apologize yet. Next: {sop_name}."
        if any(w in lower for w in ["verify", "name", "email", "account"]) or "?" in last_customer:
            return f"Customer is responding to you — acknowledge their info, then complete: {sop_name}."
        if any(w in lower for w in ["charged", "down", "outage", "broken", "manager", "angry", "stuck"]):
            return (
                f"Customer described their problem — use brief, specific empathy once (not 'I understand' every turn), "
                f"then work on: {sop_name}."
            )
        if any(w in lower for w in ["done", "still", "thank", "okay", "works"]):
            return f"Customer updated you on progress — address their latest point, then: {sop_name}."
        return f"Respond to what the customer just said, then complete: {sop_name}."

    def get_session_display_state(self, scenario_id: str, session: dict) -> dict:
        scenario = get_scenario(self.store, scenario_id)
        if not scenario:
            return {}
        sop_state = self._build_sop_state(scenario, session)
        last_customer = last_message_from(session.get("conversation", []), "customer")
        return {
            **sop_state,
            "last_customer_message": last_customer,
        }

    def _completed_sop_ids(self, scenario, session: dict) -> set[str]:
        progress = session.get("sop_progress")
        if progress:
            return {p["id"] for p in progress if p.get("completed")}
        if session.get("conversation_complete"):
            return {act.id for act in scenario.sop_activities}
        conv = session.get("conversation", [])
        turns = [ConversationTurn(**t) for t in conv]
        checked = check_sop_contextual(scenario.sop_activities, turns)
        return {a.id for a in checked if a.completed}

    def _current_sop_index(self, scenario, completed_sop_ids: set[str], session: dict | None = None) -> int:
        if session and session.get("conversation_complete"):
            return max(len(scenario.sop_activities) - 1, 0)
        for i, act in enumerate(scenario.sop_activities):
            if act.id not in completed_sop_ids:
                return i
        return max(len(scenario.sop_activities) - 1, 0)

    async def process_trainee_message(self, session_id: str, text: str) -> dict:
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        scenario = get_scenario(self.store, session["scenario_id"])
        if not scenario:
            raise ValueError("Scenario not found")

        cleaned = dedupe_repeated_speech(text.strip())
        script_dicts = [t.model_dump(mode="json") for t in scenario.success_script]
        trainee_count_before = count_speakers(session["conversation"], "trainee")
        expected_line = next_trainee_line(script_dicts, trainee_count_before)

        # DF_S3: complete off-script → hold + toast; context match → customer replies (practice & assessment)
        if should_hold_for_script_drift(
            cleaned,
            expected_line,
            session.get("mode") or "practice",
            session.get("conversation") or [],
        ):
            if session.get("hints_enabled"):
                guidance = self.get_practice_guidance(session["scenario_id"], session)
                hints = guidance.get("hints", [])
                hold_reason = (
                    "That was off-script. Use the suggested line or the same meaning, "
                    "then the customer will continue."
                )
            else:
                guidance = self.get_session_display_state(session["scenario_id"], session)
                guidance = {
                    **guidance,
                    "suggested_response": expected_line,
                }
                hints = []
                hold_reason = (
                    "That was off-script. Stay on the same topic as the script "
                    "(you may use your own words), then the customer will continue."
                )
            self.store.save_session(session)
            return {
                "session": session,
                "customer_response": None,
                "sentiment": session.get("current_sentiment"),
                "audio": None,
                "hints": hints,
                "guidance": guidance,
                # Practice shows suggested line; assessment may still use it for coaching context
                "recommended_next_step": expected_line if session.get("hints_enabled") else None,
                "suggested_response": expected_line,
                "script_hold": True,
                "script_hold_reason": hold_reason,
                "conversation_complete": False,
            }

        trainee_turn = ConversationTurn(speaker="trainee", text=cleaned)
        session["conversation"].append(trainee_turn.model_dump(mode="json"))
        session["turn_count"] = session.get("turn_count", 0) + 1
        trainee_count = count_speakers(session["conversation"], "trainee")
        scenario_ctx = scenario.model_dump(mode="json")
        customer_count_before = count_speakers(session["conversation"], "customer")
        last_agent_msg = last_message_from(session["conversation"], "trainee") or ""

        # The customer's FIRST line must read exactly as the scenario script / preview timeline
        # shows it. Going through the LLM (or through ensure_customer_intro, which pads an
        # intro-only script line with up to 140 characters of scenario.description) turned that
        # opening into a paragraph the trainee never saw in the scenario.
        scripted_opening = (
            (next_customer_line(script_dicts, 0) or "").strip()
            if customer_count_before == 0
            else ""
        )
        if scripted_opening:
            customer_text = repair_dangling_text(scripted_opening)
            response = {
                "text": customer_text,
                "sentiment": scenario.opening_sentiment.value,
                "conversation_complete": should_mark_conversation_complete(
                    script_dicts,
                    session["conversation"],
                    customer_count_before + 1,
                    last_agent_msg,
                    customer_text,
                ),
            }
        else:
            response = await self.bedrock.generate_customer_response(
                scenario_ctx,
                session["conversation"],
                session["mode"],
                trainee_count,
            )
            customer_text = response["text"]
            customer_text = ensure_customer_intro(
                scenario_ctx,
                customer_text,
                customer_count_before,
                last_agent_msg,
                session["conversation"],
            )
            # Live replies are already short — do not truncate again before TTS/transcript.
            customer_text = repair_dangling_text((customer_text or "").strip())
            response["text"] = customer_text
        customer_turn = ConversationTurn(speaker="customer", text=customer_text)
        session["conversation"].append(customer_turn.model_dump(mode="json"))
        session["current_sentiment"] = response.get("sentiment", "neutral")
        conversation_complete = bool(response.get("conversation_complete"))
        if conversation_complete:
            session["conversation_complete"] = True
            if session.get("mode") == "assessment":
                session["status"] = "ready_to_evaluate"
        turn_id = f"t{session['turn_count']}"
        audio = await self.polly.synthesize_customer(
            customer_text,
            session["user_id"],
            session_id,
            turn_id,
        )
        self.store.save_session(session)

        if session.get("hints_enabled"):
            guidance = self.get_practice_guidance(session["scenario_id"], session)
            hints = guidance.get("hints", [])
        else:
            guidance = self.get_session_display_state(session["scenario_id"], session)
            hints = []
        self.store.save_session(session)

        return {
            "session": session,
            "customer_response": customer_text,
            "sentiment": session["current_sentiment"],
            "audio": audio,
            "hints": hints,
            "guidance": guidance,
            "recommended_next_step": guidance.get("suggested_response") if session.get("hints_enabled") else None,
            "suggested_response": guidance.get("suggested_response") if session.get("hints_enabled") else None,
            "script_hold": False,
            "conversation_complete": conversation_complete,
        }

    def get_session(self, session_id: str) -> dict | None:
        return self.store.get_session(session_id)
