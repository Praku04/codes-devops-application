"""Scenario generation via Bedrock with built-in fallbacks."""

from __future__ import annotations

import logging

from utils.timezone_helpers import now_ist

from utils.voice_training.bedrock_service import VoiceBedrockService
from utils.voice_training.fallback_scenarios import CATEGORY_LABELS, FALLBACK_VARIANTS, STANDARD_CATEGORIES
from utils.voice_training.scenario_repository import ScenarioRepository
from utils.voice_training.scenario_uniqueness import (
    build_existing_scenarios_block,
    build_variation_hint,
    is_too_similar,
    pick_least_similar_fallback,
    select_categories_for_count,
)
from utils.voice_training.script_turns import (
    MIN_SCRIPT_TURNS,
    alternating_script_turns,
    ensure_min_script_turns,
    finalize_generated_script,
    normalize_script,
    repair_script_continuity,
)
from utils.voice_training.transcript_parser import (
    clean_raw_transcript,
    coerce_scenario_script_turns,
    extract_transcript_body,
    finalize_upload_script_turns,
    merge_short_turns,
    script_speaker_sanity,
    trainer_notes_for_upload,
)
from utils.voice_training.schemas import (
    ConversationTurn,
    ScenarioCategory,
    ScenarioScript,
    SentimentLevel,
    SOPActivity,
)

logger = logging.getLogger(__name__)

_MAX_UNIQUE_ATTEMPTS = 2

_DEFAULT_UPLOAD_SOP = [
    {"name": "Greet and verify", "description": "Open professionally and verify the customer account."},
    {"name": "Listen and empathize", "description": "Acknowledge frustration and confirm the billing concern."},
    {"name": "Explain pricing", "description": "Clarify introductory vs renewal pricing using account facts."},
    {"name": "Offer resolution", "description": "Propose refund, plan change, or downgrade aligned with policy."},
    {"name": "Confirm and close", "description": "Summarize resolution, confirm satisfaction, and close warmly."},
]


def _normalize_turns(script: list[ConversationTurn], description: str = "", persona: str = "") -> list[ConversationTurn]:
    raw = [t.model_dump() for t in script if (t.text or "").strip()]
    ordered = alternating_script_turns(raw)
    repaired = repair_script_continuity(ordered, description=description, persona=persona)
    padded = ensure_min_script_turns(repaired, min_turns=MIN_SCRIPT_TURNS, description=description, persona=persona)
    normalized = normalize_script(padded)
    finalized = finalize_generated_script(normalized, description=description, persona=persona)
    return [ConversationTurn(**turn) for turn in finalized]


def _normalize_upload_turns(
    script: list[ConversationTurn],
    description: str = "",
    persona: str = "",
) -> list[ConversationTurn]:
    """Light normalization for upload-derived scripts — professional opening, no IVR noise."""
    raw = [t.model_dump() for t in script if (t.text or "").strip()]
    finalized = finalize_upload_script_turns(raw, description=description, persona=persona)
    return [ConversationTurn(**turn) for turn in finalized]


class VoiceScenarioGeneratorService:
    def __init__(self, repo: ScenarioRepository, bedrock: VoiceBedrockService):
        self.repo = repo
        self.bedrock = bedrock
        self.last_generation_warnings: list[str] = []

    async def ensure_scenarios_exist(self) -> list[ScenarioScript]:
        existing = self.repo.list_all()
        if existing:
            return existing
        return await self.generate_auto_batch(count=len(STANDARD_CATEGORIES))

    async def generate_auto_batch(
        self,
        count: int = 1,
        custom_prompt: str | None = None,
        generation_mode: str = "ai_auto",
        custom_use_case: str | None = None,
        document_context: str | None = None,
        source_documents: list[str] | None = None,
    ) -> list[ScenarioScript]:
        """Generate up to `count` unique scenarios (1–5), one at a time."""
        count = max(1, min(5, count))
        self.last_generation_warnings = []
        shared_existing = self.repo.list_org_existing()
        categories = select_categories_for_count(count, shared_existing)
        results: list[ScenarioScript] = []

        for category in categories:
            scenario = await self.generate_for_category(
                category,
                custom_prompt=custom_prompt,
                generation_mode=generation_mode,
                custom_use_case=custom_use_case,
                document_context=document_context,
                source_documents=source_documents,
                existing_library=list(shared_existing),
            )
            if scenario is None:
                continue
            shared_existing.append(scenario)
            results.append(scenario)

        if not results:
            raise RuntimeError(
                "No unique scenarios could be generated. Similar scenarios may already exist — try count 1 or archive duplicates."
            )
        return results

    async def generate_all_categories(
        self,
        custom_prompt: str | None = None,
        generation_mode: str = "ai_auto",
        custom_use_case: str | None = None,
        document_context: str | None = None,
        source_documents: list[str] | None = None,
    ) -> list[ScenarioScript]:
        """Backward-compatible wrapper — generates one per standard category (max 4)."""
        return await self.generate_auto_batch(
            count=len(STANDARD_CATEGORIES),
            custom_prompt=custom_prompt,
            generation_mode=generation_mode,
            custom_use_case=custom_use_case,
            document_context=document_context,
            source_documents=source_documents,
        )

    async def generate_for_category(
        self,
        category: ScenarioCategory,
        custom_prompt: str | None = None,
        generation_mode: str = "ai_auto",
        custom_category_label: str | None = None,
        custom_use_case: str | None = None,
        document_context: str | None = None,
        source_documents: list[str] | None = None,
        existing_library: list[ScenarioScript] | None = None,
        file_primary: bool = False,
    ) -> ScenarioScript | None:
        scenario_id = self.repo.new_id(category)
        label = (
            custom_category_label.strip()
            if custom_category_label and custom_category_label.strip()
            else CATEGORY_LABELS[category]
        )
        user_guided = self._is_user_guided_generation(
            generation_mode, custom_prompt, custom_use_case, document_context
        )
        existing = existing_library if existing_library is not None else self.repo.list_org_existing()
        avoid_block = build_existing_scenarios_block(existing, category)

        if file_primary and document_context and document_context.strip():
            scenario = await self._generate_from_uploaded_transcript(
                scenario_id,
                category,
                label,
                custom_prompt=custom_prompt,
                custom_use_case=custom_use_case,
                custom_category_label=custom_category_label,
                document_context=document_context,
                source_documents=source_documents or [],
                existing=existing,
                avoid_block=avoid_block,
            )
            if scenario is not None:
                return scenario
            raise RuntimeError(
                "Could not build a scenario from the uploaded file. "
                "Check the recording quality or upload a matching .txt transcript."
            )

        if self.bedrock.is_available:
            for attempt in range(_MAX_UNIQUE_ATTEMPTS):
                try:
                    raw = await self.bedrock.generate_scenario(
                        category.value,
                        label,
                        custom_prompt=custom_prompt,
                        generation_mode=generation_mode,
                        custom_use_case=custom_use_case,
                        document_context=document_context,
                        existing_avoid_block=avoid_block,
                        variation_hint=build_variation_hint(attempt),
                        temperature=0.78 + attempt * 0.08,
                        file_primary=file_primary,
                    )
                    scenario = self._parse_scenario(
                        scenario_id,
                        category,
                        raw,
                        custom_category_label=custom_category_label,
                        custom_use_case=custom_use_case,
                        source_documents=source_documents or [],
                    )
                    if is_too_similar(scenario, existing):
                        logger.info(
                            "Generated scenario too similar to existing library (attempt %s/%s)",
                            attempt + 1,
                            _MAX_UNIQUE_ATTEMPTS,
                        )
                        continue
                    self.repo.save(scenario)
                    return scenario
                except ValueError as exc:
                    logger.info(
                        "Incomplete scenario from Bedrock (attempt %s/%s): %s",
                        attempt + 1,
                        _MAX_UNIQUE_ATTEMPTS,
                        exc,
                    )
                    if attempt == _MAX_UNIQUE_ATTEMPTS - 1:
                        if file_primary:
                            raise RuntimeError(
                                self._record_bedrock_failure(
                                    category, exc, file_primary=True
                                )
                            ) from exc
                        self._record_bedrock_failure(category, exc, file_primary=False)
                        break
                except Exception as exc:
                    if user_guided or file_primary:
                        raise RuntimeError(
                            self._record_bedrock_failure(category, exc, file_primary=file_primary)
                        ) from exc
                    self._record_bedrock_failure(category, exc, file_primary=file_primary)
                    break
        elif user_guided or file_primary:
            hint = self.bedrock.setup_hint or "Bedrock is not available for guided generation."
            self.last_generation_warnings.append(hint)
            raise RuntimeError(hint)

        scenario = self._build_fallback(
            scenario_id,
            category,
            existing=existing,
            custom_prompt=custom_prompt,
            custom_category_label=custom_category_label,
            custom_use_case=custom_use_case,
            document_context=document_context,
            source_documents=source_documents or [],
        )
        if scenario is None:
            label = CATEGORY_LABELS.get(category, category.value)
            warning = f"Skipped {label}: a similar scenario already exists in the library."
            if warning not in self.last_generation_warnings:
                self.last_generation_warnings.append(warning)
            return None
        self.repo.save(scenario)
        return scenario

    async def _generate_from_uploaded_transcript(
        self,
        scenario_id: str,
        category: ScenarioCategory,
        category_label: str,
        *,
        custom_prompt: str | None,
        custom_use_case: str | None,
        custom_category_label: str | None,
        document_context: str,
        source_documents: list[str],
        existing: list[ScenarioScript],
        avoid_block: str,
    ) -> ScenarioScript | None:
        """Generate upload scenarios with the same Bedrock format as AI auto mode."""
        transcript_body = clean_raw_transcript(extract_transcript_body(document_context))
        upload_notes = trainer_notes_for_upload(custom_prompt, custom_use_case)

        if not self.bedrock.is_available:
            hint = self.bedrock.setup_hint or "Bedrock is not configured for file upload generation."
            if hint not in self.last_generation_warnings:
                self.last_generation_warnings.append(hint)
            return None

        try:
            raw = await self.bedrock.generate_scenario_from_transcript(
                transcript_body,
                category=category.value,
                category_label=category_label,
                custom_use_case=custom_use_case,
                existing_avoid_block=avoid_block,
                trainer_notes=upload_notes,
            )

            script_turns = coerce_scenario_script_turns(raw.get("success_script", []))

            if not script_speaker_sanity(script_turns):
                logger.warning("Upload script speaker sanity failed — validating with Bedrock")
                try:
                    fixed = await self.bedrock.validate_upload_dialogue(
                        transcript_body,
                        script_turns,
                        trainer_notes=upload_notes,
                    )
                    raw["success_script"] = merge_short_turns(fixed)
                except Exception:
                    logger.warning("Upload dialogue validation failed — regenerating")
                    raw = await self.bedrock.generate_scenario_from_transcript(
                        transcript_body,
                        category=category.value,
                        category_label=category_label,
                        custom_use_case=custom_use_case,
                        existing_avoid_block=avoid_block,
                        trainer_notes=upload_notes,
                        speaker_correction=True,
                    )

            scenario = self._parse_scenario(
                scenario_id,
                category,
                raw,
                custom_category_label=custom_category_label,
                custom_use_case=custom_use_case,
                source_documents=source_documents,
                from_upload=True,
            )
            final_turns = [{"speaker": t.speaker, "text": t.text} for t in scenario.success_script]
            if not script_speaker_sanity(final_turns):
                warn = (
                    "Generated script may need speaker review in the editor — "
                    "verify agent vs customer lines before publishing."
                )
                if warn not in self.last_generation_warnings:
                    self.last_generation_warnings.append(warn)
            if source_documents:
                self.repo.save(scenario)
                return scenario
            if is_too_similar(scenario, existing):
                return None
            self.repo.save(scenario)
            return scenario
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            warning = f"Upload scenario build failed: {detail[:220]}"
            if warning not in self.last_generation_warnings:
                self.last_generation_warnings.append(warning)
            logger.warning("Uploaded transcript scenario generation failed")
            return None

    @staticmethod
    def _is_user_guided_generation(
        generation_mode: str,
        custom_prompt: str | None,
        custom_use_case: str | None,
        document_context: str | None,
    ) -> bool:
        if generation_mode == "prompt_guided":
            return True
        return bool(
            (custom_prompt and custom_prompt.strip())
            or (custom_use_case and custom_use_case.strip())
            or (document_context and document_context.strip())
        )

    def _record_bedrock_failure(
        self,
        category: ScenarioCategory,
        _exc: Exception,
        *,
        file_primary: bool = False,
    ) -> str:
        logger.warning("Bedrock scenario generation failed.")
        if file_primary:
            hint = "Bedrock could not build a scenario from the uploaded file."
        else:
            hint = f"Bedrock failed for {category.value} — using built-in template."
        if hint not in self.last_generation_warnings:
            self.last_generation_warnings.append(hint)
        return hint

    def _build_fallback(
        self,
        scenario_id: str,
        category: ScenarioCategory,
        existing: list[ScenarioScript] | None = None,
        custom_prompt: str | None = None,
        custom_category_label: str | None = None,
        custom_use_case: str | None = None,
        document_context: str | None = None,
        source_documents: list[str] | None = None,
    ) -> ScenarioScript | None:
        variants = FALLBACK_VARIANTS.get(category) or FALLBACK_VARIANTS[ScenarioCategory.CUSTOM]
        template = pick_least_similar_fallback(variants, existing or [])
        if template is None:
            return None
        sop = [
            SOPActivity(id=f"sop-{i+1}", name=a["name"], description=a["description"])
            for i, a in enumerate(template["sop_activities"])
        ]
        title = template["title"]
        description = template["description"]
        persona = template["customer_persona"]
        script = [ConversationTurn(speaker=t["speaker"], text=t["text"]) for t in template["success_script"]]
        script = _normalize_turns(script, description=description, persona=persona)
        sentiment = SentimentLevel.FRUSTRATED
        if custom_category_label and custom_category_label.strip():
            title = f"{custom_category_label.strip()}: {title}"
        if custom_use_case and custom_use_case.strip():
            description = f"{custom_use_case.strip()[:200]}. {description}"
        if custom_prompt and custom_prompt.strip():
            persona = f"{persona} (User context: {custom_prompt.strip()[:200]})"
        if document_context and document_context.strip():
            persona = f"{persona} (Document context applied.)"
        return ScenarioScript(
            id=scenario_id,
            category=category,
            title=title,
            description=description,
            customer_persona=persona,
            opening_sentiment=sentiment,
            sop_activities=sop,
            success_script=script,
            generated_at=now_ist(),
            custom_category_label=custom_category_label,
            custom_use_case=custom_use_case,
            source_documents=source_documents or [],
        )

    def _parse_scenario(
        self,
        scenario_id: str,
        category: ScenarioCategory,
        raw: dict,
        custom_category_label: str | None = None,
        custom_use_case: str | None = None,
        source_documents: list[str] | None = None,
        preserve_upload_script: bool = False,
        from_upload: bool = False,
    ) -> ScenarioScript:
        sop = [
            SOPActivity(
                id=f"sop-{i+1}",
                name=a.get("name", f"Step {i+1}"),
                description=a.get("description", ""),
            )
            for i, a in enumerate(raw.get("sop_activities", []))
        ]
        if not sop:
            sop = [
                SOPActivity(id=f"sop-{i+1}", name=a["name"], description=a["description"])
                for i, a in enumerate(_DEFAULT_UPLOAD_SOP)
            ]
        script = [
            ConversationTurn(speaker=t["speaker"], text=t["text"])
            for t in coerce_scenario_script_turns(raw.get("success_script", []))
        ]
        description = raw.get("description", "")
        persona = raw.get("customer_persona", "")

        if from_upload:
            script = _normalize_turns(script, description=description, persona=persona)
        elif preserve_upload_script:
            script = _normalize_upload_turns(script, description=description, persona=persona)
        else:
            script = _normalize_turns(script, description=description, persona=persona)

        sentiment = SentimentLevel.FRUSTRATED
        if len(script) < MIN_SCRIPT_TURNS:
            raise ValueError(
                f"Generated scenario is incomplete ({len(script)} turns, need {MIN_SCRIPT_TURNS})"
            )
        return ScenarioScript(
            id=scenario_id,
            category=category,
            title=raw.get("title", CATEGORY_LABELS[category]),
            description=description,
            customer_persona=persona,
            opening_sentiment=sentiment,
            sop_activities=sop,
            success_script=script,
            generated_at=now_ist(),
            custom_category_label=custom_category_label,
            custom_use_case=custom_use_case,
            source_documents=source_documents or [],
            script_editor_preserved=from_upload or preserve_upload_script,
        )
