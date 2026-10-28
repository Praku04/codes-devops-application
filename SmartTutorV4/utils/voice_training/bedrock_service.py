"""Bedrock text generation for voice training scenarios and dialogue."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from utils.security_helpers import log_safe
from utils.voice_training.transcript_parser import is_ivr_or_system_line
from utils.voice_training.schemas import SentimentLevel
from utils.voice_training.script_turns import (
    MIN_SCRIPT_TURNS,
    MAX_SCRIPT_TURNS,
    agent_asks_resolution_check,
    agent_deviated_from_script,
    alternating_script_turns,
    build_contextual_agent_followup_response,
    build_contextual_customer_fallback,
    build_resolution_check_reply,
    conversation_should_complete,
    conversation_state_prompt_block,
    count_speakers,
    customer_reply_invents_off_timeline_question,
    enforce_closing_customer_reply,
    ensure_customer_intro,
    extract_scenario_facts,
    facts_prompt_block,
    last_message_from,
    needs_contextual_customer_reply,
    next_customer_line,
    next_trainee_line,
    repair_dangling_text,
    resolve_customer_turn_text,
    script_customer_exhausted,
    should_advance_to_script_beat,
    should_mark_conversation_complete,
    shorten_speech_text,
    upcoming_customer_beats,
)

logger = logging.getLogger(__name__)


class VoiceBedrockService:
    def __init__(self, bedrock_client: Any, model_id: str):
        self._client = bedrock_client
        self.model_id = model_id
        self._credential_hint: str | None = None
        if bedrock_client is None:
            self._credential_hint = "Bedrock client is not configured."

    @property
    def is_available(self) -> bool:
        return self._client is not None

    @property
    def model_label(self) -> str:
        if self.is_available:
            return f"Bedrock ({self.model_id})"
        return "local fallback"

    @property
    def setup_hint(self) -> str | None:
        return self._credential_hint

    async def generate_scenario(
        self,
        category: str,
        category_label: str,
        custom_prompt: str | None = None,
        generation_mode: str = "ai_auto",
        custom_use_case: str | None = None,
        document_context: str | None = None,
        existing_avoid_block: str | None = None,
        variation_hint: str | None = None,
        temperature: float | None = None,
        *,
        file_primary: bool = False,
    ) -> dict[str, Any]:
        if file_primary and document_context and document_context.strip():
            return await self.generate_scenario_from_transcript(
                document_context,
                category=category,
                category_label=category_label,
                custom_prompt=custom_prompt,
                custom_use_case=custom_use_case,
                existing_avoid_block=existing_avoid_block,
            )
        context_block = self._build_scenario_context(
            custom_prompt=custom_prompt,
            generation_mode=generation_mode,
            custom_use_case=custom_use_case,
            document_context=document_context,
        )
        uniqueness_block = ""
        if existing_avoid_block and existing_avoid_block.strip():
            uniqueness_block = f"\n{existing_avoid_block.strip()}\n"
        if variation_hint and variation_hint.strip():
            uniqueness_block += f"\nDIVERSITY DIRECTIVE: {variation_hint.strip()}\n"
        prompt = (
            f"Generate a customer support VOICE training scenario for: {category_label} ({category}).\n"
            f"{context_block}\n"
            f"{uniqueness_block}"
            "Return ONLY valid JSON (no markdown fences) with this exact structure:\n"
            "{\n"
            '  "title": "specific scenario title",\n'
            '  "description": "2 sentence problem description with concrete details",\n'
            '  "customer_persona": "name, role, mood, and why they are calling",\n'
            '  "opening_sentiment": "frustrated",\n'
            '  "sop_activities": [{"name": "...", "description": "..."}],\n'
            '  "success_script": [{"speaker": "trainee|customer", "text": "..."}]\n'
            "}\n"
            f"{self._voice_dialogue_rules_block()}"
        )
        invoke_temp = temperature if temperature is not None else 0.82
        text = await self._invoke(prompt, max_tokens=4096, temperature=invoke_temp)
        return self._parse_json(text)

    @staticmethod
    def _voice_dialogue_rules_block() -> str:
        """Shared voice-call dialogue rules for auto and upload generation."""
        return (
            "Dialogue rules (voice-call style):\n"
            "- Use speaker \"trainee\" for the support agent and \"customer\" for the caller (never \"agent\").\n"
            "- Agent (trainee) opens the call first, then strict customer/agent alternation.\n"
            f"- Include exactly {MIN_SCRIPT_TURNS} to {MAX_SCRIPT_TURNS} turns total — many short back-and-forth exchanges.\n"
            "- opening_sentiment MUST be \"frustrated\" — the customer starts upset or impatient.\n"
            "- Each line is ONE short spoken sentence (max 18 words customer, 22 words agent).\n"
            "- Never repeat the same sentence twice in one turn.\n"
            "- Spread verification, troubleshooting, and resolution across many short turns.\n"
            "- End with a proper call close: agent gives a substantive summary, customer thanks agent, agent says goodbye.\n"
            "- Agent MUST ask before the customer provides email, account numbers, or card digits.\n"
            "- Conversation flow: agent greets with \"How may I help you today?\" → customer states issue → "
            "agent empathizes and troubleshoots. Do NOT re-ask \"how can I help\" after the issue is stated.\n"
            "- Agent lines: greetings, verification questions, empathy, plan/pricing explanation, refund offer, closing.\n"
            "- Customer lines: verification answers, billing complaint, frustration, agreement, thanks.\n"
            "- NEVER put \"how may I help you today\" or \"how can I help you\" on a customer line.\n"
            "- NEVER put billing complaints (e.g. \"you are making a fool\", \"on the website I see\") on agent lines.\n"
            "- NEVER put empathy/apology (\"I'm sorry for the inconvenience\") on customer lines.\n"
            "- Keep names, card last-4 digits, amounts, and plan names CONSISTENT across all turns.\n"
            "- NAME RULES: customer_persona MUST start with a real person name (e.g. "
            "\"Emma Rodriguez, HR Coordinator, frustrated, …\"). "
            "The customer has exactly that one first name. Early customer lines MUST introduce "
            "it (e.g. \"Hi, this is Emma.\"). "
            "Agent lines may address the caller ONLY by that first name "
            "(e.g. \"I appreciate you sharing that, Emma\" / \"To summarize, Emma: …\"). "
            "NEVER use severity, mood, priority, or exclamation words as names "
            "(critical, urgent, frustrated, angry, priority, ridiculous, unacceptable, absurd, etc.). "
            "Phrases like \"this is critical\", \"this is urgent\", or \"this is ridiculous!\" "
            "are NOT name introductions — they are complaint language. "
            "If the customer never gave a real first name, agent lines must NOT invent one "
            "(say \"I hear your frustration.\" not \"I hear your frustration, ridiculous.\"). "
            "Closing summary must be \"To summarize:\" or \"To summarize, {RealFirstName}:\" — "
            "never \"To summarize, ridiculous:\". "
            "Other people mentioned in the story (e.g. an employee named Sarah) are NOT the caller. "
            "The agent has a DIFFERENT name only if the agent introduces it (e.g. \"My name is Alex\"). "
            "Never thank or address the customer as if they were the agent. "
            "CRITICAL ROLE RULE: After the summary, the customer thanks the AGENT — "
            "e.g. \"That sounds great, Alex — thank you for explaining everything clearly.\" "
            "NEVER write \"That sounds great, Jane\" when Jane is the customer. "
            "Customer lines must NOT use the agent's name (Hi Alex / Thank you, Alex) unless that "
            "name already appeared in an earlier trainee intro turn. If the agent never said their "
            "name, customer thanks must be unnamed (e.g. \"That sounds great — thank you for explaining\"). "
            "Do not repeat the same thanks twice in one agent turn "
            "(avoid \"Thanks …, Jane. Thank you, Jane.\").\n"
            "Include at least 5 SOP activities."
        )

    async def structure_transcript_to_dialogue(
        self,
        transcript: str,
        *,
        trainer_notes: str | None = None,
    ) -> list[dict[str, str]]:
        """Convert raw/unlabeled call transcription into alternating trainee/customer turns."""
        notes_block = ""
        if trainer_notes and trainer_notes.strip():
            notes_block = f"{trainer_notes.strip()}\n\n"
        prompt = (
            "You are structuring a raw customer-support call transcription into a training script.\n"
            f"{notes_block}"
            "The text below may be one continuous block without speaker labels (from AWS Transcribe).\n"
            "Infer who is the support agent (trainee) vs the customer from context.\n"
            "Follow trainer instructions for tone, de-escalation, and training emphasis when provided.\n"
            "CRITICAL RULES:\n"
            "- Extract names, products, dollar amounts, device counts, and resolution ONLY from the transcript text.\n"
            "- Do NOT invent or substitute different customer names, companies, or billing amounts.\n"
            "- Do NOT use names or amounts from trainer notes unless they also appear in the transcript.\n"
            "- Agent MUST open turn 1 with a professional greeting (e.g. \"Hello, thank you for calling support. How may I help you today?\").\n"
            "- SKIP and OMIT IVR/hold/system lines (e.g. \"please stay on the line\", \"while you are connected\") — never use them as dialogue.\n"
            "- Interpret garbled ASR wording into clear spoken sentences; do NOT copy nonsense verbatim.\n"
            "- Use speaker \"trainee\" for agent and \"customer\" for caller.\n"
            f"- Produce {MIN_SCRIPT_TURNS} to {MAX_SCRIPT_TURNS} short alternating turns (one spoken sentence each).\n"
            "- Preserve the real story: verification, complaint, explanation, refund/downgrade, closing.\n"
            "- Polish grammar lightly but keep facts faithful (e.g. $149.99, $49.99, McAfee plan names).\n"
            "RAW TRANSCRIPT:\n"
            f"{transcript.strip()[:14000]}\n\n"
            "Return ONLY a JSON array (no markdown fences):\n"
            '[{"speaker":"trainee|customer","text":"..."}, ...]'
        )
        text = await self._invoke(prompt, max_tokens=6144, temperature=0.25, timeout_sec=120.0)
        data = self._parse_json_list_objects(text)
        turns: list[dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            speaker = str(item.get("speaker") or "").strip().lower()
            if speaker not in ("customer", "trainee"):
                continue
            line = str(item.get("text") or "").strip()
            if line and not is_ivr_or_system_line(line):
                turns.append({"speaker": speaker, "text": line})
        if len(turns) < 8:
            raise ValueError("Structured transcript too short")
        return turns

    async def validate_upload_dialogue(
        self,
        transcript: str,
        draft_turns: list[dict[str, str]],
        *,
        trainer_notes: str | None = None,
    ) -> list[dict[str, str]]:
        """Validate speaker roles and call flow against source transcript before saving."""
        notes_block = ""
        if trainer_notes and trainer_notes.strip():
            notes_block = f"{trainer_notes.strip()}\n\n"
        draft_json = json.dumps(
            [{"speaker": t.get("speaker"), "text": t.get("text")} for t in draft_turns if t.get("text")],
            ensure_ascii=True,
        )[:12000]
        prompt = (
            "Validate and correct a voice-training call script against the source transcript.\n"
            f"{notes_block}"
            "SOURCE TRANSCRIPT (facts must match this):\n"
            f"{transcript.strip()[:12000]}\n\n"
            "DRAFT SCRIPT (may have wrong speaker labels or garbled lines):\n"
            f"{draft_json}\n\n"
            "CORRECTION RULES:\n"
            "- Agent (trainee): greeting, verification questions, empathy, plan explanation, refund/downgrade offer, closing.\n"
            "- Customer: answers verification, states billing complaint, frustration, agreement/thanks.\n"
            "- NEVER put customer complaint text on trainee lines or agent explanation on customer lines.\n"
            "- Turn 1 MUST be trainee greeting with \"Hello, thank you for calling support. How may I help you today?\"\n"
            "- Strict trainee/customer alternation; one short sentence per turn.\n"
            "- NEVER put \"how may I help\" or \"how can I help\" on customer lines.\n"
            "- NEVER put billing complaints or \"on the website\" pricing arguments on trainee lines.\n"
            "- NEVER put \"I'm sorry for the inconvenience\" or plan explanations on customer lines.\n"
            "- Customer provides name/card digits ONLY when answering agent verification questions.\n"
            "- Use ONLY names, amounts, products, and resolution details from the source transcript.\n"
            "- Follow trainer instructions for tone and training focus when consistent with the transcript.\n"
            "- Interpret garbled ASR phrases into clear spoken sentences.\n"
            f"- Return {MIN_SCRIPT_TURNS} to {MAX_SCRIPT_TURNS} turns when the transcript supports it; "
            "never fewer than 12 substantive turns.\n"
            "Return ONLY a JSON array:\n"
            '[{"speaker":"trainee|customer","text":"..."}, ...]'
        )
        text = await self._invoke(prompt, max_tokens=6144, temperature=0.2, timeout_sec=120.0)
        data = self._parse_json_list_objects(text)
        turns: list[dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            speaker = str(item.get("speaker") or "").strip().lower()
            if speaker not in ("customer", "trainee"):
                continue
            line = str(item.get("text") or "").strip()
            if line and not is_ivr_or_system_line(line):
                turns.append({"speaker": speaker, "text": line})
        if len(turns) < 12:
            raise ValueError("Validated upload dialogue too short")
        return turns

    async def generate_scenario_from_transcript(
        self,
        transcript: str,
        *,
        category: str,
        category_label: str,
        custom_prompt: str | None = None,
        custom_use_case: str | None = None,
        existing_avoid_block: str | None = None,
        trainer_notes: str | None = None,
        speaker_correction: bool = False,
    ) -> dict[str, Any]:
        """Build a scenario anchored on uploaded transcript content — not generic category templates."""
        trainer_block = ""
        if trainer_notes and trainer_notes.strip():
            trainer_block = f"{trainer_notes.strip()}\n"
        elif custom_prompt and custom_prompt.strip():
            trainer_block = (
                "Optional trainer style notes (IGNORE names/amounts here if not in transcript):\n"
                f"{custom_prompt.strip()[:400]}\n"
            )
        if custom_use_case and custom_use_case.strip():
            trainer_block += f"Policies / resolution rules: {custom_use_case.strip()[:400]}\n"
        uniqueness_block = ""
        if existing_avoid_block and existing_avoid_block.strip():
            uniqueness_block = f"\n{existing_avoid_block.strip()}\n"

        correction_block = ""
        if speaker_correction:
            correction_block = (
                "SPEAKER CORRECTION REQUIRED: A prior draft had agent/customer lines swapped. "
                "Re-read the transcript and assign every line correctly before returning JSON.\n"
            )

        prompt = (
            "Convert the uploaded CALL TRANSCRIPT into a voice training scenario JSON.\n"
            f"Training category tag (secondary only): {category_label} ({category}).\n"
            f"{trainer_block}"
            f"{correction_block}"
            "CRITICAL RULES:\n"
            "- The transcript is the ONLY source of truth for names, emails, dollar amounts, card digits, and issue details.\n"
            "- Do NOT invent a different customer name, company, product, or billing story.\n"
            "- Do NOT substitute a generic template — mirror the real conversation from the file.\n"
            "- Agent lines use speaker \"trainee\"; customer lines use speaker \"customer\".\n"
            "- Turn 1 MUST be agent greeting: \"Hello, thank you for calling support. How may I help you today?\"\n"
            "- SKIP IVR/hold/system lines.\n"
            "- Interpret messy transcription into natural call dialogue; keep facts faithful.\n"
            "- Derive title, description, and customer_persona directly from the transcript facts.\n"
            f"{uniqueness_block}"
            "UPLOADED TRANSCRIPT:\n"
            f"{transcript.strip()[:12000]}\n\n"
            "Return ONLY valid JSON (no markdown fences) with this exact structure:\n"
            "{\n"
            '  "title": "specific scenario title from the call",\n'
            '  "description": "2 sentence problem description using transcript facts",\n'
            '  "customer_persona": "name, role, mood, and why they are calling — from transcript",\n'
            '  "opening_sentiment": "frustrated",\n'
            '  "sop_activities": [{"name": "...", "description": "..."}],\n'
            '  "success_script": [{"speaker": "trainee|customer", "text": "..."}]\n'
            "}\n"
            f"{self._voice_dialogue_rules_block()}"
        )
        text = await self._invoke(prompt, max_tokens=6144, temperature=0.35, timeout_sec=120.0)
        return self._parse_json(text)

    async def generate_scenario_metadata_from_transcript(
        self,
        transcript: str,
        *,
        dialogue_turns: list[dict[str, str]] | None = None,
        trainer_notes: str | None = None,
        custom_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Title, persona, description, and SOP only — dialogue already parsed from transcript."""
        trainer_block = ""
        if trainer_notes and trainer_notes.strip():
            trainer_block = f"{trainer_notes.strip()}\n"
        elif custom_prompt and custom_prompt.strip():
            trainer_block = (
                "Optional trainer style notes (IGNORE names/amounts here if not in transcript):\n"
                f"{custom_prompt.strip()[:400]}\n"
            )
        dialogue_block = ""
        if dialogue_turns:
            dialogue_block = (
                "Structured dialogue from the upload (use these facts for metadata):\n"
                + "\n".join(
                    f"- {t['speaker']}: {t['text']}"
                    for t in dialogue_turns
                    if t.get("text")
                )[:10000]
                + "\n\n"
            )
        prompt = (
            "From this call transcript, extract scenario metadata only.\n"
            f"{trainer_block}"
            "Use ONLY facts present in the transcript and structured dialogue — do not invent names or amounts.\n"
            f"{dialogue_block}"
            f"TRANSCRIPT:\n{transcript.strip()[:10000]}\n\n"
            "Return ONLY valid JSON:\n"
            "{\n"
            '  "title": "...",\n'
            '  "description": "...",\n'
            '  "customer_persona": "...",\n'
            '  "opening_sentiment": "frustrated",\n'
            '  "sop_activities": [{"name": "...", "description": "..."}]\n'
            "}"
        )
        text = await self._invoke(prompt, max_tokens=2048, temperature=0.3, timeout_sec=90.0)
        return self._parse_json(text)

    def _build_scenario_context(
        self,
        custom_prompt: str | None,
        generation_mode: str,
        custom_use_case: str | None,
        document_context: str | None,
    ) -> str:
        parts: list[str] = []
        if custom_prompt and custom_prompt.strip():
            parts.append(f"Trainer prompt: {custom_prompt.strip()[:800]}")
        if custom_use_case and custom_use_case.strip():
            parts.append(f"Use case: {custom_use_case.strip()[:400]}")
        if document_context and document_context.strip():
            parts.append(f"Reference documents:\n{document_context.strip()[:6000]}")
        if generation_mode == "prompt_guided":
            parts.append("Follow the trainer prompt and uploaded reference material closely.")
        if document_context and document_context.strip():
            parts.append(
                "Uploaded reference material is the primary source — do not replace it with invented details."
            )
        return "\n".join(parts) if parts else "Use realistic enterprise customer-support details."

    async def _invoke(
        self,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float | None = None,
        timeout_sec: float = 60.0,
    ) -> str:
        if not self._client:
            raise RuntimeError(self._credential_hint or "Bedrock unavailable")

        def _call() -> str:
            inference: dict[str, Any] = {"maxTokens": max_tokens}
            if temperature is not None:
                inference["temperature"] = temperature
            request_body = {
                "messages": [{"role": "user", "content": [{"text": prompt}]}],
                "inferenceConfig": inference,
            }
            response = self._client.invoke_model(
                modelId=self.model_id,
                body=json.dumps(request_body),
                contentType="application/json",
            )
            result = json.loads(response["body"].read())
            return result["output"]["message"]["content"][0]["text"]

        return await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout_sec)

    async def generate_prompt_examples(
        self, category: str | None, topic_hint: str | None = None
    ) -> dict[str, Any]:
        category_hint = f"Category focus: {category}.\n" if category else ""
        topic_block = (
            f"Topic hint: {topic_hint.strip()}\n"
            if topic_hint and topic_hint.strip()
            else "Generate varied creative examples across HR, billing, tech support, and telecom.\n"
        )
        prompt = (
            "Generate exactly 3 DETAILED customer support voice-training scenario briefs.\n"
            f"{category_hint}{topic_block}"
            "Each brief is ONE string with 4–6 sentences (about 90–180 words) that a trainer can paste "
            "directly into an AI generator to produce a full practice call script.\n"
            "Every brief MUST include:\n"
            "- Caller name, role, and company or department\n"
            "- The specific problem with concrete facts (dates, amounts, error text, ticket IDs, policy names)\n"
            "- Why it is urgent now and how frustrated or anxious they are\n"
            "- What they already tried (portal, email, manager, previous agent)\n"
            "- The outcome they want from this call\n"
            "- One constraint the agent must handle (policy, SLA, security verification, escalation rule)\n"
            "Write in second person or narrative style suitable for a trainer prompt box — not bullet points.\n"
            "Do NOT write one-line summaries. Each brief must stand alone as a complete scenario seed.\n"
            "Return ONLY a JSON array of exactly 3 strings (no markdown fences)."
        )
        text = await self._invoke(prompt, max_tokens=2500, temperature=0.88)
        examples = self._parse_json_list(text)
        if len(examples) < 3:
            raise ValueError(f"Expected 3 examples, got {len(examples)}")
        return {"examples": examples[:3], "source": "bedrock"}

    async def generate_customer_response(
        self,
        scenario_context: dict,
        conversation_history: list[dict],
        mode: str,
        trainee_count: int,
    ) -> dict[str, Any]:
        script = scenario_context.get("success_script", [])
        customer_count = count_speakers(conversation_history, "customer")
        last_agent = last_message_from(conversation_history, "trainee") or ""

        if agent_asks_resolution_check(last_agent):
            reply = build_resolution_check_reply(
                conversation_history, scenario_context, last_agent
            )
            reply = enforce_closing_customer_reply(
                reply, conversation_history, last_agent, scenario_context
            )
            return {
                "text": reply,
                "sentiment": self._detect_sentiment(reply),
                "conversation_complete": should_mark_conversation_complete(
                    script, conversation_history, customer_count + 1, last_agent, reply
                ),
            }

        if script_customer_exhausted(script, customer_count):
            followup = build_contextual_agent_followup_response(
                last_agent, conversation_history, scenario_context
            )
            if followup:
                return {
                    "text": followup,
                    "sentiment": self._detect_sentiment(followup),
                    "conversation_complete": should_mark_conversation_complete(
                        script, conversation_history, customer_count + 1, last_agent, followup
                    ),
                }
            if self.is_available and last_agent:
                try:
                    return await self._bedrock_customer_response(
                        scenario_context,
                        conversation_history,
                        mode,
                        reference_line="",
                        deviated=True,
                        upcoming_beats=[],
                        script_exhausted=True,
                    )
                except Exception as exc:
                    logger.warning("Bedrock exhausted-script response failed: %s", log_safe(exc))
            return self._closing_customer_response(
                scenario_context, mode, last_agent, conversation_history
            )

        # Always use preview timeline order — never skip scripted "filler" beats
        # (e.g. billing history / statement match / timing) or practice jumps ahead.
        reference_line = next_customer_line(script, customer_count) or ""
        deviated = agent_deviated_from_script(script, conversation_history)
        upcoming = upcoming_customer_beats(script, customer_count + 1)

        if trainee_count > 0:
            resolved, source = resolve_customer_turn_text(
                reference_line,
                last_agent,
                conversation_history,
                scenario_context,
                script,
            )
            if source in ("reference", "contextual"):
                complete = should_mark_conversation_complete(
                    script, conversation_history, customer_count + 1, last_agent, resolved
                )
                return {
                    "text": resolved,
                    "sentiment": self._detect_sentiment(resolved),
                    "conversation_complete": complete,
                }

            if reference_line and should_advance_to_script_beat(
                reference_line, last_agent, script, conversation_history
            ):
                text = repair_dangling_text((reference_line or "").strip())
                text = ensure_customer_intro(
                    scenario_context, text, customer_count, last_agent, conversation_history
                )
                return {
                    "text": text,
                    "sentiment": self._detect_sentiment(text),
                    "conversation_complete": should_mark_conversation_complete(
                        script, conversation_history, customer_count + 1, last_agent, text
                    ),
                }

            needs_contextual = needs_contextual_customer_reply(
                reference_line, last_agent, script, conversation_history
            )

            if reference_line and not needs_contextual:
                text = repair_dangling_text((reference_line or "").strip())
                text = ensure_customer_intro(
                    scenario_context, text, customer_count, last_agent, conversation_history
                )
                return {
                    "text": text,
                    "sentiment": self._detect_sentiment(text),
                    "conversation_complete": should_mark_conversation_complete(
                        script, conversation_history, customer_count + 1, last_agent, text
                    ),
                }

            if self.is_available and needs_contextual:
                try:
                    result = await self._bedrock_customer_response(
                        scenario_context,
                        conversation_history,
                        mode,
                        reference_line=reference_line,
                        deviated=deviated,
                        upcoming_beats=upcoming,
                    )
                    text = result.get("text", "")
                    text = ensure_customer_intro(
                        scenario_context, text, customer_count, last_agent, conversation_history
                    )
                    if customer_count > 0:
                        text = repair_dangling_text(shorten_speech_text(text, "customer"))
                    if reference_line and customer_reply_invents_off_timeline_question(text, reference_line):
                        text = repair_dangling_text(shorten_speech_text(reference_line, "customer"))
                    result["text"] = text
                    result["conversation_complete"] = should_mark_conversation_complete(
                        script,
                        conversation_history,
                        customer_count + 1,
                        last_agent,
                        text,
                    )
                    return result
                except Exception as exc:
                    logger.warning("Bedrock customer response failed: %s", log_safe(exc))

            text = build_contextual_customer_fallback(
                reference_line,
                last_agent,
                conversation_history,
                scenario_context,
                deviated=deviated,
            )
            complete = should_mark_conversation_complete(
                script, conversation_history, customer_count + 1, last_agent, text
            )
            return {
                "text": text,
                "sentiment": self._detect_sentiment(text),
                "conversation_complete": complete,
            }

        if reference_line:
            text = ensure_customer_intro(
                scenario_context, reference_line, customer_count, last_agent, conversation_history
            )
            text = repair_dangling_text(shorten_speech_text(text, "customer"))
            complete = should_mark_conversation_complete(
                script, conversation_history, customer_count + 1, last_agent, text
            )
            return {
                "text": text,
                "sentiment": self._detect_sentiment(text),
                "conversation_complete": complete,
            }

        if self.is_available:
            try:
                return await self._bedrock_customer_response(
                    scenario_context,
                    conversation_history,
                    mode,
                    reference_line=reference_line,
                    deviated=False,
                    upcoming_beats=upcoming,
                )
            except Exception as exc:
                logger.warning("Bedrock customer response failed: %s", log_safe(exc))
        return self._script_fallback(scenario_context, customer_count, mode)

    async def _bedrock_customer_response(
        self,
        scenario_context: dict,
        conversation_history: list[dict],
        mode: str,
        reference_line: str = "",
        deviated: bool = False,
        upcoming_beats: list[str] | None = None,
        script_exhausted: bool = False,
    ) -> dict[str, Any]:
        history_text = "\n".join(
            f"{'Customer' if h['speaker'] == 'customer' else 'Agent'}: {h['text']}"
            for h in conversation_history
        )
        last_agent = last_message_from(conversation_history, "trainee") or ""
        facts = extract_scenario_facts(
            scenario_context.get("success_script"),
            scenario_context.get("customer_persona", ""),
            scenario_context.get("description", ""),
        )
        facts_block = facts_prompt_block(facts)
        phase_block = conversation_state_prompt_block(conversation_history, scenario_context, last_agent)
        beats = upcoming_beats or []
        beats_block = ""
        if beats:
            beats_block = "Upcoming story beats (advance only when they fit the live call):\n" + "\n".join(
                f"- {b}" for b in beats[:3]
            )

        deviation_block = ""
        if deviated:
            expected = next_trainee_line(
                scenario_context.get("success_script", []),
                max(0, count_speakers(conversation_history, "trainee") - 1),
            )
            deviation_block = (
                "IMPORTANT: The agent's last reply did NOT follow the expected script flow. "
                f"Expected something like: \"{expected or 'a relevant troubleshooting step'}\". "
                "React to what they ACTUALLY said — show confusion, redirect, or answer their question "
                "before moving on. Do NOT pretend they gave a perfect response.\n"
            )

        ref_block = ""
        if reference_line:
            if not deviated:
                ref_block = (
                    "Use this reference response closely — confirm you completed what the agent asked:\n"
                    f"\"{reference_line}\"\n"
                )
            else:
                ref_block = (
                    "Reference line for this customer turn (plot guide — adapt, do not read verbatim if agent went off-script):\n"
                    f"\"{reference_line}\"\n"
                )

        mode_note = (
            "Practice and assessment: stay in character; follow the scenario timeline beats from the "
            "reference script — do NOT invent new questions unless that exact question is in the "
            "reference/upcoming beats. Remember the full conversation so far."
        )

        exhausted_block = ""
        if script_exhausted:
            exhausted_block = (
                "The reference script lines are finished, but the agent may still be asking questions. "
                "Answer ONLY what the agent just asked — do not repeat a generic goodbye unless they are closing the call.\n"
                "- If they ask whether the issue is resolved/fixed/working: answer yes or no based on the conversation so far.\n"
                "- If they ask 'anything else?': say no politely if you are done.\n"
                "- If they say thank you or goodbye: respond briefly and warmly.\n"
            )

        prompt = (
            f"You are simulating the CUSTOMER in a live voice support training call.\n"
            f"Persona: {scenario_context.get('customer_persona', '')}\n"
            f"Problem: {scenario_context.get('description', '')}\n"
            f"{mode_note}\n"
            f"{facts_block}\n"
            f"{phase_block}\n\n"
            f"FULL conversation so far (remember every detail — names, models, errors, promises):\n"
            f"{history_text or '(Agent opened the call — no customer lines yet.)'}\n\n"
            f"The agent JUST said:\n\"{last_agent}\"\n\n"
            f"{ref_block}"
            f"{beats_block}\n"
            f"{deviation_block}"
            f"{exhausted_block}"
            "Reply as the customer in 1-2 SHORT spoken sentences (max 22 words total).\n"
            "Rules:\n"
            "- Respond DIRECTLY to the agent's last message — answer their question before anything else.\n"
            "- If they ask whether the problem is fixed/resolved/working, answer honestly from the conversation history.\n"
            "- If they ask 'anything else?', answer that specific question — do not ignore it.\n"
            "- Remember everything already said; do not contradict earlier facts.\n"
            "- Do NOT use the agent's name unless they already introduced themselves in this call.\n"
            "- Follow the reference/upcoming story beats — advance the scenario timeline naturally.\n"
            "- If the agent asked for information, provide it from the conversation or reference line.\n"
            "- If the agent was vague, wrong, or skipped your problem, say so politely and restate what you need.\n"
            "- Do NOT ask new timeline or follow-up questions unless they appear in the reference or upcoming beats.\n"
            "- If the agent escalated or gave a substantive update, acknowledge per the reference beat — do not improvise new asks.\n"
            "- Do NOT ignore the agent and jump ahead as if they said the perfect script line.\n"
            "- Sound natural, conversational, and in-character.\n"
            "Return ONLY the customer's spoken dialogue — no labels or quotes."
        )
        text = await self._invoke(prompt, max_tokens=220, temperature=0.55)
        text = shorten_speech_text(text.strip().strip('"'), "customer")
        if reference_line and customer_reply_invents_off_timeline_question(text, reference_line):
            text = shorten_speech_text(reference_line, "customer")
        text = enforce_closing_customer_reply(
            text, conversation_history, last_agent, scenario_context
        )
        customer_count = count_speakers(conversation_history, "customer")
        text = ensure_customer_intro(
            scenario_context, text, customer_count, last_agent, conversation_history
        )
        script = scenario_context.get("success_script", [])
        complete = should_mark_conversation_complete(
            script, conversation_history, customer_count + 1, last_agent, text
        )
        return {
            "text": text,
            "sentiment": self._detect_sentiment(text),
            "conversation_complete": complete,
        }

    def _parse_json(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            cleaned = cleaned.rsplit("```", 1)[0]
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
        return json.loads(cleaned)

    def _parse_json_list(self, text: str) -> list[str]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            cleaned = cleaned.rsplit("```", 1)[0]
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
        data = json.loads(cleaned)
        if not isinstance(data, list):
            raise ValueError("Expected JSON array")
        return [str(item).strip() for item in data if str(item).strip()]

    def _parse_json_list_objects(self, text: str) -> list[Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            cleaned = cleaned.rsplit("```", 1)[0]
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
        data = json.loads(cleaned)
        if not isinstance(data, list):
            raise ValueError("Expected JSON array")
        return data

    def _closing_customer_response(
        self,
        scenario_context: dict,
        mode: str,
        last_agent: str = "",
        conversation_history: list[dict] | None = None,
    ) -> dict[str, Any]:
        followup = None
        if last_agent and conversation_history:
            followup = build_contextual_agent_followup_response(
                last_agent, conversation_history, scenario_context
            )
        if followup:
            text = followup
        else:
            closings = [
                "That's everything I needed. Thank you for your help today.",
                "Perfect, I'm all set now. Thanks so much.",
                "No more questions. I appreciate your time.",
            ]
            text = shorten_speech_text(secrets.choice(closings), "customer")
        return {
            "text": text,
            "sentiment": self._detect_sentiment(text),
            "conversation_complete": True,
        }

    def _script_fallback(self, scenario_context: dict, customer_count: int, mode: str) -> dict[str, Any]:
        script = scenario_context.get("success_script", [])
        if script_customer_exhausted(script, customer_count):
            return self._closing_customer_response(scenario_context, mode)
        reference = next_customer_line(script, customer_count) or ""
        text = shorten_speech_text(reference, "customer") if reference else ""
        if text:
            text = ensure_customer_intro(
                scenario_context, text, customer_count, "", None
            )
        if not text:
            return self._closing_customer_response(scenario_context, mode)
        complete = should_mark_conversation_complete(
            script, [], customer_count + 1, "", text
        )
        return {
            "text": text,
            "sentiment": self._detect_sentiment(text),
            "conversation_complete": complete,
        }

    def _detect_sentiment(self, text: str) -> str:
        lower = text.lower()
        if any(w in lower for w in ["thank", "great", "perfect", "appreciate", "works"]):
            return SentimentLevel.SATISFIED.value
        if any(w in lower for w in ["unacceptable", "frustrated", "angry", "manager"]):
            return SentimentLevel.FRUSTRATED.value
        return SentimentLevel.NEUTRAL.value
