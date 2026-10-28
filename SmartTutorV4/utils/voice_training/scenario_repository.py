"""Database-backed scenario repository."""

from __future__ import annotations

import json
import logging
import uuid

from utils.voice_training.schemas import ScenarioCategory, ScenarioScript
from utils.voice_training.script_turns import (
    MIN_SCRIPT_TURNS,
    ensure_min_script_turns,
    finalize_generated_script,
    normalize_script,
    normalize_script_manual_edit,
    repair_script_continuity,
)
from utils.security_helpers import log_safe
from utils.voice_training.storage import VoiceTrainingStore

logger = logging.getLogger(__name__)


def _prepare_scenario_data(data: dict, *, repair: bool = True) -> dict:
    if not repair or not data.get("success_script"):
        return data
    if data.get("script_editor_preserved"):
        return {
            **data,
            "success_script": normalize_script_manual_edit(data["success_script"]),
        }
    description = data.get("description") or ""
    persona = data.get("customer_persona") or ""
    raw_script = data["success_script"]
    repaired = repair_script_continuity(raw_script, description=description, persona=persona)
    normalized = normalize_script(repaired)
    if len(normalized) < MIN_SCRIPT_TURNS:
        normalized = ensure_min_script_turns(
            normalized, MIN_SCRIPT_TURNS, description=description, persona=persona
        )
        normalized = finalize_generated_script(
            normalized, description=description, persona=persona
        )
    return {**data, "success_script": normalized}


def _script_changed(before: list | None, after: list | None) -> bool:
    return json.dumps(before or [], sort_keys=True) != json.dumps(after or [], sort_keys=True)


def get_scenario(store: VoiceTrainingStore, scenario_id: str, *, persist_repair: bool = True) -> ScenarioScript | None:
    data = store.get_scenario(scenario_id)
    if not data:
        return None
    original_script = data.get("success_script")
    prepared = _prepare_scenario_data(dict(data))
    if persist_repair and _script_changed(original_script, prepared.get("success_script")):
        author_info = store.get_scenario_author(scenario_id)
        if author_info:
            author_id, _title = author_info
            store.save_scenario(
                prepared,
                author_id=author_id,
                process_id=prepared.get("process_id") or data.get("process_id"),
            )
    return ScenarioScript.model_validate(prepared)


class ScenarioRepository:
    def __init__(
        self,
        store: VoiceTrainingStore,
        author_id: str,
        process_id: str | None = None,
    ):
        self.store = store
        self.author_id = author_id
        self.process_id = process_id

    def save(self, scenario: ScenarioScript) -> ScenarioScript:
        self.store.save_scenario(
            scenario.model_dump(mode="json"),
            author_id=self.author_id,
            process_id=self.process_id,
        )
        return scenario

    def get(self, scenario_id: str) -> ScenarioScript | None:
        return get_scenario(self.store, scenario_id)

    def list_all(
        self,
        category: ScenarioCategory | None = None,
        include_archived: bool = False,
        archived_only: bool = False,
        author_id: str | None = None,
    ) -> list[ScenarioScript]:
        cat_value = category.value if category else None
        rows = self.store.list_scenarios(
            category=cat_value,
            process_id=self.process_id,
            include_global=True,
            include_archived=include_archived,
            archived_only=archived_only,
            author_id=author_id,
        )
        scenarios: list[ScenarioScript] = []
        for data in rows:
            try:
                # Always repair on list so name-role / continuity fixes apply to every scenario
                prepared = _prepare_scenario_data(dict(data))
                scenarios.append(ScenarioScript.model_validate(prepared))
            except Exception as repair_exc:
                logger.warning(
                    "Skipping invalid scenario row %s after repair failure: %s",
                    log_safe(data.get("id", "unknown")),
                    log_safe(repair_exc),
                )
                continue
        return scenarios

    def list_org_existing(
        self,
        category: ScenarioCategory | None = None,
        include_archived: bool = False,
    ) -> list[ScenarioScript]:
        """All active scenarios org-wide (every trainer/admin), for uniqueness checks."""
        cat_value = category.value if category else None
        rows = self.store.list_scenarios_org_wide(
            category=cat_value,
            include_archived=include_archived,
        )
        scenarios: list[ScenarioScript] = []
        for data in rows:
            try:
                prepared = _prepare_scenario_data(dict(data))
                scenarios.append(ScenarioScript.model_validate(prepared))
            except Exception as repair_exc:
                logger.debug(
                    "Skipping invalid org scenario row %s: %s",
                    log_safe(data.get("id", "unknown")),
                    log_safe(repair_exc),
                )
                continue
        return scenarios

    def count(self) -> int:
        return self.store.count_scenarios()

    def new_id(self, category: ScenarioCategory) -> str:
        if category == ScenarioCategory.CUSTOM:
            prefix = "cust"
        else:
            prefix = category.value.split("_")[0][:4]
        return f"{prefix}-{uuid.uuid4().hex[:8]}"


def get_scenarios_by_category(
    store: VoiceTrainingStore,
    process_id: str | None,
    category: ScenarioCategory | None = None,
) -> list[ScenarioScript]:
    repo = ScenarioRepository(store, author_id="", process_id=process_id)
    return repo.list_all(category)
