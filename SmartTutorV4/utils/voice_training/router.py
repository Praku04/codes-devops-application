"""Voice training REST API — PostgreSQL + S3, Smart Tutor JWT auth."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from utils.security_helpers import (
    GENERIC_API_ERROR_MESSAGE,
    SERVICE_UNAVAILABLE_MESSAGE,
    log_safe,
    safe_client_error_detail,
)
from utils.timezone_helpers import now_ist
from utils.voice_training.bedrock_service import VoiceBedrockService
from utils.voice_training.config import get_voice_training_settings
from utils.voice_training.conversation_service import VoiceConversationService
from utils.voice_training.document_parser import (
    combine_document_texts,
    extract_text_from_bytes,
    is_text_transcript_filename,
)
from utils.voice_training.evaluation_service import VoiceEvaluationService
from utils.voice_training.assessment_helpers import coerce_assessment_result
from utils.voice_training.pdf_service import generate_assessment_pdf_bytes
from utils.voice_training.polly_service import VoicePollyService
from utils.voice_training.transcript_parser import file_primary_generation
from utils.voice_training.transcribe_service import VoiceTranscribeService, is_audio_filename
from utils.upload_validation import (
    VOICE_AUDIO_EXTENSIONS,
    assert_safe_upload_filename,
    validate_voice_scenario_upload,
)
from utils.voice_training.scenario_generator_service import VoiceScenarioGeneratorService
from utils.voice_training.scenario_repository import ScenarioRepository, get_scenario
from utils.voice_training.script_turns import (
    count_expected_session_turns,
    extract_scenario_facts,
    normalize_script,
    repair_script_continuity,
)
from utils.voice_training.scenario_helpers import scenario_to_summary
from utils.voice_training.evaluation_context import map_sop_to_script_cues
from utils.voice_training.schemas import (
    ConversationTurn,
    DashboardStats,
    EvaluateRequest,
    GenerateScenarioRequest,
    PromptExamplesRequest,
    ScenarioCategory,
    SessionMode,
    PublishVoiceScenarioRequest,
    ManagePublishAudienceRequest,
    VoiceAssessmentConfigRequest,
    StartSessionRequest,
    UpdateScenarioRequest,
    ScenarioScript,
    BulkScenarioIdsRequest,
    BulkAssessmentIdsRequest,
)
from utils.voice_training.s3_keys import assessment_pdf_key, sanitize_id, session_tts_key
from utils.voice_training.notifications import (
    notify_voice_assessment_completed,
    notify_voice_scenario_published,
)
from utils.voice_training.publish import (
    apply_assessment_config,
    apply_publish_audience,
    apply_publish_config,
    assessment_access,
    assessment_block_reason,
    get_assessment_config,
    get_publish_config,
    is_assessment_configured,
    list_published_scenarios_for_trainee,
    publish_audience_summary,
    resolve_publish_audience,
    resolve_scenario_publisher,
    scenario_visible_to_trainee,
)
from utils.voice_training.storage import VoiceTrainingStore

from .dependencies import VoiceTrainingDependencies


def create_voice_training_router(deps: VoiceTrainingDependencies) -> APIRouter:
    router = APIRouter(prefix="/voice-training", tags=["Voice Training"])
    logger = deps.logger
    settings = get_voice_training_settings()

    def require_staff(current_user):
        if current_user.role not in ("trainer", "super_admin"):
            raise HTTPException(
                status_code=403,
                detail="Access denied. Only trainers and super admins can manage Voice Training.",
            )

    def require_access(current_user):
        """Any role that may use voice training (trainee = published scenarios only)."""
        if current_user.role not in ("trainer", "super_admin", "trainee"):
            raise HTTPException(status_code=403, detail="Access denied.")

    def require_trainee(current_user):
        if current_user.role != "trainee":
            raise HTTPException(status_code=403, detail="Trainee access only.")

    def _trainer_trainee_ids(db: Session, trainer_id: str) -> list[str]:
        from models.domain import BatchMember, TraineeBatch

        batch_ids = [
            row[0]
            for row in db.query(TraineeBatch.id)
            .filter(TraineeBatch.trainer_id == trainer_id, TraineeBatch.is_active == True)
            .all()
        ]
        if not batch_ids:
            return []
        return [
            row[0]
            for row in db.query(BatchMember.trainee_id)
            .filter(BatchMember.batch_id.in_(batch_ids))
            .distinct()
            .all()
        ]

    def _scenario_summary_with_meta(
        store: VoiceTrainingStore,
        scenario: ScenarioScript,
        *,
        current_user=None,
    ) -> dict:
        from models.domain import VoiceScenario

        summary = scenario_to_summary(scenario)
        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario.id).first()
        content = row.content if row and isinstance(row.content, dict) else {}
        summary["publish"] = get_publish_config(content)
        summary["assessment_config"] = get_assessment_config(content)
        if summary["publish"].get("is_published") and row:
            summary["publisher"] = resolve_scenario_publisher(store.db, row, content)
        if current_user and current_user.role in ("trainer", "super_admin"):
            trainer_id = current_user.id
            proc_id = row.process_id if row else None
            if summary["publish"].get("is_published"):
                full_audience = resolve_publish_audience(
                    store.db, content, trainer_id=trainer_id, process_id=proc_id
                )
                summary["publish_audience"] = publish_audience_summary(
                    store.db, content, trainer_id, process_id=proc_id
                )
                pass_pct = int(summary["publish"].get("pass_percentage", 70))
                summary["trainee_assessment_results"] = store.build_scenario_trainee_assessment_results(
                    scenario.id,
                    full_audience.get("active_trainees") or [],
                    pass_percentage=pass_pct,
                )
        return summary

    def _assert_scenario_staff_access(row, current_user):
        if current_user.role == "super_admin":
            return
        if row.author_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only the scenario author can manage this scenario.")

    def _scenario_access(store: VoiceTrainingStore, scenario_id: str, user_id: str) -> dict:
        from models.domain import VoiceScenario

        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        publish = get_publish_config(row.content if isinstance(row.content, dict) else {})
        practice_count = store.count_practice_sessions(user_id, scenario_id)
        assessment_count = store.count_assessment_attempts(user_id, scenario_id)
        pass_status = store.get_assessment_pass_status(
            user_id, scenario_id, publish.get("pass_percentage", 70)
        )
        access = assessment_access(
            practice_count=practice_count,
            assessment_count=assessment_count,
            publish=publish,
            has_passed=bool(pass_status.get("has_passed")),
            best_percentage=float(pass_status.get("best_percentage") or 0),
        )
        access["is_published"] = publish.get("is_published", False)
        scenario_assessments = store.list_assessments_for_user_scenario(user_id, scenario_id)
        access["scenario_assessments"] = [
            {
                "assessment_id": a.get("assessment_id"),
                "attempt_number": a.get("attempt_number"),
                "attempt_total": a.get("attempt_total"),
                "final_percentage": a.get("final_percentage"),
                "grade": a.get("grade"),
                "passed": bool(a.get("passed")),
                "created_at": a.get("created_at"),
            }
            for a in scenario_assessments
        ]
        if scenario_assessments:
            access["latest_assessment_id"] = scenario_assessments[-1].get("assessment_id")
        return access

    def _assert_assessment_owner(data: dict | None, current_user, db: Session | None = None):
        if not data:
            raise HTTPException(status_code=404, detail="Assessment not found")
        if data.get("user_id") == current_user.id:
            return
        if current_user.role == "super_admin":
            return
        if current_user.role == "trainer" and db is not None:
            trainee_ids = _trainer_trainee_ids(db, current_user.id)
            if data.get("user_id") in trainee_ids:
                return
        raise HTTPException(status_code=403, detail="Access denied")

    def require_super_admin(current_user):
        if current_user.role != "super_admin":
            raise HTTPException(status_code=403, detail="Super admin access required.")

    def _store(db: Session) -> VoiceTrainingStore:
        return VoiceTrainingStore(db, deps.s3_client, deps.s3_bucket_name)

    def _bedrock() -> VoiceBedrockService:
        return VoiceBedrockService(deps.bedrock_client, deps.generation_model)

    def _repo(store: VoiceTrainingStore, current_user) -> ScenarioRepository:
        return ScenarioRepository(store, current_user.id, current_user.process_id)

    def _conversation(store: VoiceTrainingStore) -> VoiceConversationService:
        return VoiceConversationService(store, _bedrock(), VoicePollyService(deps.polly_client, store))

    def _transcribe() -> VoiceTranscribeService:
        return VoiceTranscribeService(deps.s3_client, deps.transcribe_client, deps.s3_bucket_name)

    def _assert_session_owner(session: dict | None, current_user, allow_super: bool = True):
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.get("user_id") != current_user.id:
            if not (allow_super and current_user.role == "super_admin"):
                raise HTTPException(status_code=403, detail="Access denied")

    @router.get("/health")
    async def voice_training_health(
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        transcribe_svc = _transcribe()
        stt_mode = "browser"
        if settings.use_aws_agent_stt and transcribe_svc.is_configured:
            stt_mode = "aws_transcribe"
        return {
            "status": "ok",
            "bedrock_available": _bedrock().is_available,
            "bedrock_model": deps.generation_model,
            "scenario_count": store.count_scenarios(),
            "use_polly": settings.use_polly,
            "transcribe_configured": transcribe_svc.is_configured,
            "s3_bucket_configured": bool(deps.s3_bucket_name),
            "agent_stt_mode": stt_mode,
            "agent_stt_fallback": "browser",
            "agent_stt_max_wait_ms": settings.agent_stt_max_wait_ms,
            "transcribe_language": settings.transcribe_language,
        }

    @router.get("/scenarios")
    async def list_scenarios(
        category: ScenarioCategory | None = None,
        include_archived: bool = Query(False),
        archived_only: bool = Query(False),
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        repo = _repo(_store(db), current_user)
        scenarios = repo.list_all(
            category,
            include_archived=include_archived,
            archived_only=archived_only,
            author_id=current_user.id,
        )
        return [_scenario_summary_with_meta(_store(db), s, current_user=current_user) for s in scenarios]

    @router.get("/scenarios/{scenario_id}/assessment-config")
    async def get_scenario_assessment_config(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        from models.domain import VoiceScenario

        store = _store(db)
        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if row.author_id != current_user.id and current_user.role != "super_admin":
            raise HTTPException(status_code=403, detail="Only the scenario author can view assessment config.")
        content = row.content if isinstance(row.content, dict) else {}
        return {
            "scenario_id": scenario_id,
            "scenario_title": row.title,
            "assessment_config": get_assessment_config(content),
        }

    @router.post("/scenarios/{scenario_id}/assessment-config")
    async def configure_scenario_assessment(
        scenario_id: str,
        req: VoiceAssessmentConfigRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        from models.domain import VoiceScenario

        store = _store(db)
        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if row.author_id != current_user.id and current_user.role != "super_admin":
            raise HTTPException(status_code=403, detail="Only the scenario author can configure assessment.")
        content = dict(row.content or {})
        row.content = apply_assessment_config(
            content,
            min_practice_sessions=req.min_practice_sessions,
            max_assessment_attempts=req.max_assessment_attempts,
            pass_percentage=req.pass_percentage,
            configured_by=current_user.id,
        )
        store.db.commit()
        store.db.refresh(row)
        deps.log_activity(db, current_user.id, "voice_training_assessment_configured", scenario_id)
        return {
            "message": "Voice training assessment configuration saved.",
            "assessment_config": get_assessment_config(row.content),
        }

    @router.post("/scenarios/{scenario_id}/delete")
    async def archive_scenario(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        store = _store(db)
        from models.domain import VoiceScenario

        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        _assert_scenario_staff_access(row, current_user)
        scenario = get_scenario(store, scenario_id)
        if not scenario:
            raise HTTPException(status_code=404, detail="Scenario not found")
        archived = store.archive_scenario(scenario_id)
        if not archived:
            raise HTTPException(status_code=404, detail="Scenario not found")
        deps.log_activity(db, current_user.id, "voice_training_scenario_archived", scenario_id)
        return scenario_to_summary(ScenarioScript.model_validate(archived))

    @router.post("/scenarios/{scenario_id}/restore")
    async def restore_scenario(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        store = _store(db)
        from models.domain import VoiceScenario

        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        _assert_scenario_staff_access(row, current_user)
        restored = store.restore_scenario(scenario_id)
        if not restored:
            raise HTTPException(status_code=404, detail="Scenario not found")
        deps.log_activity(db, current_user.id, "voice_training_scenario_restored", scenario_id)
        return scenario_to_summary(ScenarioScript.model_validate(restored))

    @router.post("/scenarios/{scenario_id}/permanent/delete")
    async def permanent_delete_scenario(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        store = _store(db)
        author_info = store.get_scenario_author(scenario_id)
        if not author_info:
            raise HTTPException(status_code=404, detail="Scenario not found")
        author_id, title = author_info
        if author_id != current_user.id and current_user.role != "super_admin":
            raise HTTPException(status_code=403, detail="Access denied")
        archived = store.is_scenario_archived(scenario_id)
        if not archived:
            raise HTTPException(
                status_code=400,
                detail="Only archived scenarios can be permanently deleted. Archive it first.",
            )
        if not store.permanent_delete_scenario(scenario_id):
            raise HTTPException(status_code=404, detail="Scenario not found")
        deps.log_activity(
            db,
            current_user.id,
            "voice_training_scenario_deleted",
            f"{scenario_id}:{title}",
        )
        return {"success": True, "message": "Scenario permanently deleted."}

    @router.get("/scenarios/{scenario_id}")
    async def get_scenario_detail(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        from models.domain import VoiceScenario

        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if current_user.role == "trainee" and not scenario_visible_to_trainee(
            db, row, current_user
        ):
            raise HTTPException(status_code=403, detail="This scenario is not assigned to you.")
        if current_user.role in ("trainer", "super_admin"):
            _assert_scenario_staff_access(row, current_user)
        scenario = get_scenario(store, scenario_id)
        if not scenario:
            raise HTTPException(status_code=404, detail="Scenario not found")
        data = scenario.model_dump(mode="json")
        content = row.content if isinstance(row.content, dict) else {}
        data["assessment_config"] = get_assessment_config(content)
        data["publish"] = get_publish_config(content)
        data["sop_script_cues"] = map_sop_to_script_cues(
            scenario.success_script,
            scenario.sop_activities,
        )
        if current_user.role == "trainee":
            data["access"] = _scenario_access(store, scenario_id, current_user.id)
        return data

    @router.get("/scenarios/{scenario_id}/access")
    async def get_scenario_access(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        return _scenario_access(store, scenario_id, current_user.id)

    @router.post("/scenarios/{scenario_id}/publish")
    async def publish_scenario(
        scenario_id: str,
        req: PublishVoiceScenarioRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        from models.domain import TraineeBatch, VoiceScenario

        store = _store(db)
        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if row.author_id != current_user.id and current_user.role != "super_admin":
            raise HTTPException(status_code=403, detail="Only the scenario author can publish.")
        content = dict(row.content or {})
        if not is_assessment_configured(content):
            raise HTTPException(
                status_code=400,
                detail="Configure assessment before publishing. Use Configure Assessment on the scenario card.",
            )

        pub_type = (req.publication_type or "batch").strip()
        batch_ids = list(req.batch_ids or [])
        if pub_type in ("batch", "multi_batch") and not batch_ids:
            raise HTTPException(status_code=400, detail="Select at least one batch to publish.")
        if pub_type == "all_process" and current_user.role != "super_admin":
            from services.tutor_cache import get_system_setting

            global_sharing = get_system_setting(db, "allow_global_sharing", "false") == "true"
            if not global_sharing:
                raise HTTPException(
                    status_code=403,
                    detail="Publishing to all trainees requires global sharing enabled by super admin.",
                )
        if pub_type in ("batch", "multi_batch"):
            owned = {
                b.id
                for b in db.query(TraineeBatch)
                .filter(
                    TraineeBatch.id.in_(batch_ids),
                    TraineeBatch.trainer_id == current_user.id,
                    TraineeBatch.is_active == True,
                )
                .all()
            }
            if current_user.role != "super_admin" and len(owned) != len(set(batch_ids)):
                raise HTTPException(status_code=403, detail="One or more batches are not owned by you.")

        content = dict(row.content or {})
        row.content = apply_publish_config(
            content,
            publication_type=pub_type,
            batch_ids=batch_ids,
            published_by=current_user.id,
            trainee_ids=req.trainee_ids,
            excluded_trainee_ids=req.excluded_trainee_ids,
        )
        store.db.commit()
        store.db.refresh(row)
        trainer_id = None if current_user.role == "super_admin" else current_user.id
        audience = resolve_publish_audience(
            db,
            row.content if isinstance(row.content, dict) else {},
            trainer_id=trainer_id,
            process_id=row.process_id,
        )
        trainee_ids = [t["id"] for t in audience.get("active_trainees") or []]
        if trainee_ids:
            store.clear_scenario_trainee_progress(scenario_id, trainee_ids)
        deps.log_activity(db, current_user.id, "voice_training_scenario_published", scenario_id)
        notify_voice_scenario_published(
            db,
            trainee_ids=[t["id"] for t in audience.get("active_trainees") or []],
            scenario_title=row.title,
            publisher_email=current_user.email,
        )
        db.commit()
        scenario = get_scenario(store, scenario_id)
        summary = _scenario_summary_with_meta(store, scenario, current_user=current_user)
        return {
            "message": f"Scenario published to {pub_type.replace('_', ' ')}.",
            "scenario": summary,
            "audience": audience,
        }

    @router.get("/scenarios/{scenario_id}/publish-audience")
    async def get_publish_audience(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        from models.domain import VoiceScenario

        store = _store(db)
        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        _assert_scenario_staff_access(row, current_user)
        content = row.content if isinstance(row.content, dict) else {}
        if not get_publish_config(content).get("is_published"):
            raise HTTPException(status_code=400, detail="Scenario is not published yet.")
        trainer_id = None if current_user.role == "super_admin" else current_user.id
        return resolve_publish_audience(
            db,
            content,
            trainer_id=trainer_id,
            process_id=row.process_id,
        )

    @router.post("/scenarios/{scenario_id}/publish-audience/update")
    async def update_publish_audience(
        scenario_id: str,
        req: ManagePublishAudienceRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        from models.domain import TraineeBatch, VoiceScenario

        store = _store(db)
        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        _assert_scenario_staff_access(row, current_user)
        content = dict(row.content or {})
        if not get_publish_config(content).get("is_published"):
            raise HTTPException(status_code=400, detail="Publish the scenario before managing trainees.")

        batch_ids = list(req.batch_ids or [])
        if current_user.role != "super_admin" and batch_ids:
            owned = {
                b.id
                for b in db.query(TraineeBatch)
                .filter(
                    TraineeBatch.id.in_(batch_ids),
                    TraineeBatch.trainer_id == current_user.id,
                    TraineeBatch.is_active == True,
                )
                .all()
            }
            if len(owned) != len(set(batch_ids)):
                raise HTTPException(status_code=403, detail="One or more batches are not owned by you.")

        trainer_id = None if current_user.role == "super_admin" else current_user.id
        prev_audience = resolve_publish_audience(
            db, content, trainer_id=trainer_id, process_id=row.process_id
        )
        prev_ids = {t["id"] for t in prev_audience.get("active_trainees") or []}

        try:
            row.content = apply_publish_audience(
                content,
                batch_ids=batch_ids,
                trainee_ids=list(req.trainee_ids or []),
                excluded_trainee_ids=list(req.excluded_trainee_ids or []),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=safe_client_error_detail(exc)) from exc
        store.db.commit()
        store.db.refresh(row)
        deps.log_activity(db, current_user.id, "voice_training_publish_audience_updated", scenario_id)
        trainer_id = None if current_user.role == "super_admin" else current_user.id
        audience = resolve_publish_audience(
            db,
            row.content if isinstance(row.content, dict) else {},
            trainer_id=trainer_id,
            process_id=row.process_id,
        )
        new_active_ids = {t["id"] for t in audience.get("active_trainees") or []}
        removed_ids = prev_ids - new_active_ids
        added_ids = new_active_ids - prev_ids
        if removed_ids:
            store.clear_scenario_trainee_progress(scenario_id, list(removed_ids))
        if added_ids:
            store.clear_scenario_trainee_progress(scenario_id, list(added_ids))
        if not get_publish_config(row.content if isinstance(row.content, dict) else {}).get(
            "is_published"
        ):
            store.clear_scenario_trainee_progress(scenario_id, list(prev_ids))
        new_ids = new_active_ids - prev_ids
        if new_ids:
            notify_voice_scenario_published(
                db,
                trainee_ids=list(new_ids),
                scenario_title=row.title,
                publisher_email=current_user.email,
            )
            db.commit()
        return {
            "message": "Publish audience updated.",
            "audience": audience,
        }

    @router.get("/trainee/scenarios")
    async def list_trainee_scenarios(
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_trainee(current_user)
        store = _store(db)
        rows = list_published_scenarios_for_trainee(db, current_user)
        results = []
        for row in rows:
            scenario = get_scenario(store, row.id)
            if not scenario:
                continue
            summary = _scenario_summary_with_meta(store, scenario, current_user=current_user)
            summary["access"] = _scenario_access(store, row.id, current_user.id)
            results.append(summary)
        return results

    @router.get("/trainer/trainee-assessments")
    async def list_trainer_trainee_assessments(
        batch_id: str | None = None,
        scenario_id: str | None = None,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        if current_user.role not in ("trainer", "super_admin"):
            raise HTTPException(status_code=403, detail="Trainer or super admin access only.")
        from models.domain import BatchMember, TraineeBatch

        store = _store(db)
        author_id = current_user.id
        if batch_id:
            batch = (
                db.query(TraineeBatch)
                .filter(
                    TraineeBatch.id == batch_id,
                    TraineeBatch.trainer_id == current_user.id,
                )
                .first()
            )
            if not batch:
                raise HTTPException(status_code=404, detail="Batch not found")
            trainee_ids = [
                m.trainee_id
                for m in db.query(BatchMember).filter(BatchMember.batch_id == batch_id).all()
            ]
        else:
            trainee_ids = _trainer_trainee_ids(db, current_user.id)
        return store.list_assessments_for_trainees(
            trainee_ids,
            scenario_id=scenario_id,
            author_id=author_id,
        )

    @router.post("/scenarios/{scenario_id}/update")
    async def update_scenario(
        scenario_id: str,
        req: UpdateScenarioRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        store = _store(db)
        repo = _repo(store, current_user)
        scenario = repo.get(scenario_id)
        if not scenario:
            raise HTTPException(status_code=404, detail="Scenario not found")

        from models.domain import Notification, VoiceScenario

        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        _assert_scenario_staff_access(row, current_user)
        author_id = row.author_id

        data = scenario.model_dump(mode="json")
        if req.title is not None:
            data["title"] = req.title.strip()
        if req.description is not None:
            data["description"] = req.description.strip()
        if req.customer_persona is not None:
            data["customer_persona"] = req.customer_persona.strip()
        if req.opening_sentiment is not None:
            data["opening_sentiment"] = req.opening_sentiment.value
        if req.sop_activities is not None:
            data["sop_activities"] = [a.model_dump(mode="json") for a in req.sop_activities]
        if req.success_script is not None:
            from utils.voice_training.script_turns import normalize_script_manual_edit

            turns = [ConversationTurn.model_validate(t) for t in req.success_script]
            raw_turns = [t.model_dump(mode="json") for t in turns]
            cleaned = normalize_script_manual_edit(raw_turns)
            if len(cleaned) < 2:
                raise HTTPException(status_code=400, detail="Script must include at least two turns with text.")
            data["success_script"] = cleaned
            data["script_editor_preserved"] = True

        updated = ScenarioScript.model_validate(data)
        repo.save(updated)
        deps.log_activity(db, current_user.id, "voice_training_scenario_updated", scenario_id)
        if (
            author_id
            and current_user.role == "super_admin"
            and author_id != current_user.id
        ):
            db.add(
                Notification(
                    user_id=author_id,
                    message=(
                        f"A super admin updated your voice training scenario "
                        f"\"{updated.title}\". Review it in Voice Training Agent."
                    ),
                    related_link="/voice-training",
                )
            )
            db.commit()
        return updated.model_dump(mode="json")

    @router.post("/scenarios/prompt-examples")
    async def prompt_examples(
        body: PromptExamplesRequest = Body(default_factory=PromptExamplesRequest),
        category: ScenarioCategory | None = None,
        topic_hint: str | None = Query(None),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        bedrock = _bedrock()
        if not bedrock.is_available:
            raise HTTPException(status_code=503, detail="Bedrock is not configured.")
        hint = (body.topic_hint or topic_hint or "").strip() or None
        try:
            return await bedrock.generate_prompt_examples(
                category.value if category else None,
                topic_hint=hint,
            )
        except Exception as exc:
            logger.warning("Prompt examples failed: %s", log_safe(exc))
            raise HTTPException(status_code=502, detail=GENERIC_API_ERROR_MESSAGE) from exc

    @router.post("/scenarios/generate")
    async def generate_scenarios(
        req: GenerateScenarioRequest = GenerateScenarioRequest(),
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        store = _store(db)
        repo = _repo(store, current_user)
        gen = VoiceScenarioGeneratorService(repo, _bedrock())
        category = req.category
        if req.generation_mode == "prompt_guided" and not category:
            category = ScenarioCategory.CUSTOM
        try:
            if category:
                scenario = await gen.generate_for_category(
                    category,
                    custom_prompt=req.custom_prompt,
                    generation_mode=req.generation_mode,
                    custom_category_label=req.custom_category_label,
                    custom_use_case=req.custom_use_case,
                    document_context=req.document_context,
                )
                if not scenario:
                    detail = (
                        gen.last_generation_warnings[-1]
                        if gen.last_generation_warnings
                        else "Could not generate a unique scenario. Try a different prompt or category."
                    )
                    raise HTTPException(status_code=409, detail=detail)
                summaries = [scenario_to_summary(scenario)]
                total = store.count_scenarios()
                return _generation_payload(summaries, f"Added 1 scenario ({total} total)", gen.last_generation_warnings)
            scenarios = await gen.generate_auto_batch(
                count=req.count,
                custom_prompt=req.custom_prompt,
                generation_mode=req.generation_mode,
            )
            summaries = [scenario_to_summary(s) for s in scenarios]
            total = store.count_scenarios()
            return _generation_payload(
                summaries, f"Added {len(scenarios)} scenarios ({total} total)", gen.last_generation_warnings
            )
        except RuntimeError as exc:
            logger.warning("Scenario generation unavailable: %s", log_safe(exc))
            raise HTTPException(status_code=503, detail=SERVICE_UNAVAILABLE_MESSAGE) from exc
        except Exception as exc:
            logger.error("Scenario generation failed: %s", log_safe(exc), exc_info=True)
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE) from exc

    @router.post("/scenarios/generate-rich")
    async def generate_scenarios_rich(
        force_regenerate: bool = Form(False),
        category: str | None = Form(None),
        custom_category_label: str | None = Form(None),
        custom_use_case: str | None = Form(None),
        custom_prompt: str | None = Form(None),
        generation_mode: str = Form("prompt_guided"),
        files: list[UploadFile] = File(default=[]),
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_staff(current_user)
        del force_regenerate
        store = _store(db)
        repo = _repo(store, current_user)
        gen = VoiceScenarioGeneratorService(repo, _bedrock())

        parsed_category: ScenarioCategory | None = None
        if category and category.strip():
            try:
                parsed_category = ScenarioCategory(category.strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid category: {category}") from exc

        extracted: list[tuple[str, str]] = []
        doc_names: list[str] = []
        parse_errors: list[str] = []
        transcribe_svc = _transcribe()

        # Index text uploads by basename so File.wav can pair with File.txt
        text_by_basename: dict[str, tuple[str, bytes]] = {}
        audio_uploads: list[tuple[str, bytes]] = []
        other_uploads: list[tuple[str, bytes]] = []

        for upload in files:
            if not upload.filename:
                continue
            content = await upload.read()
            if not content:
                continue
            name = upload.filename
            content_ok, content_err = validate_voice_scenario_upload(name, content)
            if not content_ok:
                raise HTTPException(status_code=400, detail=content_err)
            if is_audio_filename(name):
                audio_uploads.append((name, content))
            elif is_text_transcript_filename(name):
                base = name.rsplit(".", 1)[0].lower()
                text_by_basename[base] = (name, content)
            else:
                other_uploads.append((name, content))

        for name, content in other_uploads:
            try:
                text = extract_text_from_bytes(content, name)
                if not text.strip():
                    parse_errors.append(f"{log_safe(name)}: No readable text found in document")
                    continue
                extracted.append((name, text))
                doc_names.append(name)
            except Exception as exc:
                # Do not log upload filenames (Sonar pythonsecurity:S5145).
                logger.debug("Document parse failed for an uploaded file: %s", log_safe(exc))
                parse_errors.append(f"{log_safe(name)}: Could not parse document")

        paired_txt_bases: set[str] = set()

        for name, content in audio_uploads:
            base = name.rsplit(".", 1)[0].lower()
            paired = text_by_basename.get(base)
            if paired:
                try:
                    text = extract_text_from_bytes(paired[1], paired[0])
                    label = f"{name} (from {paired[0]})"
                    extracted.append((label, text))
                    doc_names.append(label)
                    paired_txt_bases.add(base)
                    continue
                except Exception as exc:
                    logger.debug(
                        "Paired document parse failed for an uploaded file: %s",
                        log_safe(exc),
                    )
                    parse_errors.append(f"{log_safe(paired[0])}: Could not parse document")

            if not transcribe_svc.is_configured:
                parse_errors.append(
                    f"{log_safe(name)}: AWS Transcribe is not configured. "
                    "Upload a .txt file with the same name (e.g. File_3.txt) containing the transcript."
                )
                continue
            try:
                text = await asyncio.to_thread(
                    transcribe_svc.transcribe_bytes,
                    current_user.id,
                    content,
                    name,
                )
                label = f"{name} (transcript)"
                extracted.append((label, text))
                doc_names.append(label)
            except Exception as exc:
                # Filename is user-controlled; log only the exception (Sonar S5145).
                logger.warning(
                    "Audio transcription failed for an uploaded file: %s",
                    log_safe(exc),
                )
                parse_errors.append(
                    f"{log_safe(name)}: Could not transcribe audio. "
                    f"Upload a .txt file with the same name (e.g. {log_safe(base)}.txt) "
                    "containing the transcript."
                )

        for base, (name, content) in text_by_basename.items():
            if base in paired_txt_bases:
                continue
            try:
                text = extract_text_from_bytes(content, name)
                if not text.strip():
                    parse_errors.append(f"{log_safe(name)}: No readable text found in document")
                    continue
                extracted.append((name, text))
                doc_names.append(name)
            except Exception as exc:
                logger.debug("Document parse failed for an uploaded file: %s", log_safe(exc))
                parse_errors.append(f"{log_safe(name)}: Could not parse document")

        document_context = combine_document_texts(extracted)
        if parse_errors:
            gen.last_generation_warnings = list(parse_errors)

        had_uploads = bool(audio_uploads or other_uploads or text_by_basename)
        if had_uploads and not document_context:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Could not read the uploaded file(s). "
                    "For audio, upload a matching .txt transcript (e.g. File_3.txt alongside File_3.wav) "
                    "if AWS Transcribe is unavailable."
                ),
            )

        file_primary = file_primary_generation(custom_prompt, had_uploads)
        gen_kwargs = dict(
            custom_prompt=custom_prompt,
            generation_mode=generation_mode,
            custom_category_label=custom_category_label,
            custom_use_case=custom_use_case,
            document_context=document_context or None,
            source_documents=doc_names,
            file_primary=file_primary,
        )
        if generation_mode == "prompt_guided" and not parsed_category:
            parsed_category = ScenarioCategory.CUSTOM

        try:
            if parsed_category:
                scenario = await gen.generate_for_category(parsed_category, **gen_kwargs)
                if not scenario:
                    detail = (
                        gen.last_generation_warnings[-1]
                        if gen.last_generation_warnings
                        else "Could not generate a unique scenario. Try a different prompt or category."
                    )
                    raise HTTPException(status_code=409, detail=detail)
                summaries = [scenario_to_summary(scenario)]
                total = store.count_scenarios()
                return _generation_payload(
                    summaries, f"Added 1 scenario ({total} total)", gen.last_generation_warnings
                )
            scenarios = await gen.generate_all_categories(**gen_kwargs)
            summaries = [scenario_to_summary(s) for s in scenarios]
            total = store.count_scenarios()
            return _generation_payload(
                summaries, f"Added {len(scenarios)} scenarios ({total} total)", gen.last_generation_warnings
            )
        except RuntimeError as exc:
            logger.warning("Scenario generation unavailable: %s", log_safe(exc))
            if had_uploads or file_primary:
                detail = (
                    gen.last_generation_warnings[-1]
                    if gen.last_generation_warnings
                    else (
                        "Could not generate a scenario from the uploaded file. "
                        "Try a clearer recording or upload a matching .txt transcript."
                    )
                )
                raise HTTPException(status_code=400, detail=detail) from exc
            raise HTTPException(status_code=503, detail=SERVICE_UNAVAILABLE_MESSAGE) from exc
        except Exception as exc:
            logger.error("Rich scenario generation failed: %s", log_safe(exc), exc_info=True)
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE) from exc

    @router.post("/sessions/start")
    async def start_session(
        req: StartSessionRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        from models.domain import VoiceScenario

        row = store.db.query(VoiceScenario).filter(VoiceScenario.id == req.scenario_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Scenario not found")
        content = row.content if isinstance(row.content, dict) else {}
        assess_cfg = get_assessment_config(content)
        if current_user.role == "trainee":
            if not scenario_visible_to_trainee(db, row, current_user):
                raise HTTPException(status_code=403, detail="This scenario is not assigned to you.")
            # DF_52: practice sessions = 0 disables Practice for published trainees only
            try:
                trainee_min_practice = int(assess_cfg.get("min_practice_sessions", 1))
            except (TypeError, ValueError):
                trainee_min_practice = 1
            if req.mode == SessionMode.PRACTICE and trainee_min_practice <= 0:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Practice is disabled for trainees because "
                        "practice sessions before assessment is set to 0."
                    ),
                )
            access = _scenario_access(store, req.scenario_id, current_user.id)
            if req.mode == SessionMode.ASSESSMENT:
                reason = assessment_block_reason(access)
                if reason:
                    raise HTTPException(status_code=403, detail=reason)
        elif current_user.role in ("trainer", "super_admin"):
            # Owners can always practice (including when min practice is 0)
            _assert_scenario_staff_access(row, current_user)

        conv = _conversation(store)
        try:
            session = conv.start_session(current_user.id, req.scenario_id, req.mode)
            scenario = get_scenario(store, req.scenario_id)
            hints_on = session.get("hints_enabled")
            customer_response = None
            customer_audio = None

            if req.mode == SessionMode.ASSESSMENT:
                guidance = conv.get_session_display_state(req.scenario_id, session)
                hints: list = []
                current_sentiment = scenario.opening_sentiment.value if scenario else session["current_sentiment"]
            else:
                opening_line = conv.get_opening_agent_line(req.scenario_id)
                guidance = conv.get_practice_guidance(req.scenario_id, session)
                if opening_line and not guidance.get("suggested_response"):
                    guidance = {**guidance, "suggested_response": opening_line}
                hints = guidance.get("hints", [])
                current_sentiment = scenario.opening_sentiment.value if scenario else session["current_sentiment"]

            payload = {
                "session_id": session["session_id"],
                "mode": session["mode"],
                "hints_enabled": session["hints_enabled"],
                "current_sentiment": current_sentiment,
                "conversation": session.get("conversation", []),
                "customer_response": customer_response,
                "customer_audio": customer_audio,
                "scenario_title": scenario.title if scenario else "",
                "scenario_description": scenario.description if scenario else "",
                "customer_persona": scenario.customer_persona if scenario else "",
                "scenario_category": scenario.category.value if scenario else "",
                "opening_sentiment": scenario.opening_sentiment.value if scenario else "",
                "sop_activities": [a.model_dump(mode="json") for a in scenario.sop_activities] if scenario else [],
                "guidance": guidance,
                "suggested_response": guidance.get("suggested_response") if hints_on else None,
                "hints": hints if hints_on else [],
            }
            if scenario:
                script_dicts = [t.model_dump(mode="json") for t in scenario.success_script]
                payload["expected_turns"] = count_expected_session_turns(script_dicts)
                payload["scenario_facts"] = extract_scenario_facts(
                    script_dicts,
                    scenario.customer_persona or "",
                    scenario.description or "",
                )
            if hints_on:
                payload["opening_agent_line"] = conv.get_opening_agent_line(req.scenario_id)
            elif req.mode == SessionMode.ASSESSMENT:
                payload["opening_agent_line"] = conv.get_opening_agent_line(req.scenario_id)
            return payload
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=safe_client_error_detail(exc)) from exc

    @router.post("/sessions/{session_id}/transcribe-reply")
    async def transcribe_agent_reply(
        session_id: str,
        audio: UploadFile = File(...),
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        """Transcribe a short agent voice reply with AWS Transcribe (practice/assessment)."""
        require_access(current_user)
        if not settings.use_aws_agent_stt:
            raise HTTPException(status_code=503, detail="AWS agent speech recognition is disabled.")
        store = _store(db)
        _assert_session_owner(store.get_session(session_id), current_user)
        transcribe_svc = _transcribe()
        if not transcribe_svc.is_configured:
            raise HTTPException(status_code=503, detail="AWS Transcribe is not configured.")
        filename = (audio.filename or "reply.webm").strip()
        name_ok, name_err = assert_safe_upload_filename(filename, VOICE_AUDIO_EXTENSIONS)
        if not name_ok:
            raise HTTPException(status_code=400, detail=name_err)
        if not is_audio_filename(filename):
            raise HTTPException(
                status_code=400,
                detail="Upload a supported audio recording (WEBM, WAV, MP3, M4A, etc.).",
            )
        content = await audio.read()
        if not content:
            raise HTTPException(status_code=400, detail="Empty audio recording.")
        try:
            text = await transcribe_svc.transcribe_reply_bytes_async(
                current_user.id,
                content,
                filename,
                language_code=settings.transcribe_language,
            )
            return {
                "text": text,
                "source": "aws_transcribe_stream",
                "language": settings.transcribe_language,
            }
        except TimeoutError as exc:
            logger.warning(
                "Agent reply transcription timed out for %s: %s",
                log_safe(session_id),
                log_safe(exc),
            )
            raise HTTPException(
                status_code=504,
                detail="Speech recognition timed out. Please try again.",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=safe_client_error_detail(exc)) from exc
        except Exception as exc:
            logger.warning(
                "Agent reply transcription failed for %s: %s",
                log_safe(session_id),
                log_safe(exc),
            )
            err_name = type(exc).__name__
            err_text = log_safe(exc)
            if "AccessDenied" in err_name or "AccessDenied" in err_text or "not authorized" in err_text.lower():
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "AWS Transcribe permission missing on the server IAM role. "
                        "Add transcribe:StartStreamTranscription and transcribe:StartTranscriptionJob."
                    ),
                ) from exc
            raise HTTPException(status_code=502, detail=GENERIC_API_ERROR_MESSAGE) from exc

    @router.post("/sessions/{session_id}/message")
    async def send_message(
        session_id: str,
        body: dict,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Message text is required")
        store = _store(db)
        _assert_session_owner(store.get_session(session_id), current_user)
        conv = _conversation(store)
        try:
            return await conv.process_trainee_message(session_id, text)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=safe_client_error_detail(exc)) from exc
        except Exception as exc:
            logger.error(
                "Voice session message failed for %s: %s",
                log_safe(session_id),
                log_safe(exc),
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE) from exc

    @router.get("/sessions/{session_id}")
    async def get_session(
        session_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        session = store.get_session(session_id)
        _assert_session_owner(session, current_user)
        return session

    @router.post("/sessions/{session_id}/cancel")
    async def cancel_session(
        session_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        """Abandon an in-progress session without scoring (assessment or practice)."""
        require_access(current_user)
        store = _store(db)
        session = store.get_session(session_id)
        _assert_session_owner(session, current_user)
        if session.get("status") == "completed":
            raise HTTPException(status_code=400, detail="This session is already completed.")
        session["status"] = "cancelled"
        session["ended_at"] = now_ist().isoformat()
        store.save_session(session)
        return {"ok": True, "session_id": session_id}

    @router.post("/sessions/{session_id}/complete")
    async def complete_practice_session(
        session_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        """Submit a completed practice session (counts toward assessment unlock)."""
        require_access(current_user)
        store = _store(db)
        session = store.get_session(session_id)
        _assert_session_owner(session, current_user)
        if session.get("mode") != "practice":
            raise HTTPException(
                status_code=400,
                detail="Only practice sessions can be submitted this way.",
            )
        if session.get("status") == "completed":
            return {"ok": True, "session_id": session_id, "already_completed": True}
        if session.get("status") == "cancelled":
            raise HTTPException(status_code=400, detail="This practice session was cancelled.")
        if not session.get("conversation_complete"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Complete the full practice call before submitting. "
                    "Finish every conversation turn, then use End practice. "
                    "Use the back button to leave without counting it."
                ),
            )
        session["status"] = "completed"
        session["ended_at"] = now_ist().isoformat()
        store.save_session(session)
        return {"ok": True, "session_id": session_id}

    @router.post("/assessments/evaluate")
    async def evaluate_assessment(
        req: EvaluateRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        session = store.get_session(req.session_id)
        _assert_session_owner(session, current_user)
        existing = store.get_assessment_by_session(req.session_id)
        if existing:
            return existing
        eval_svc = VoiceEvaluationService(store)
        try:
            result = eval_svc.evaluate_session(req.session_id, current_user.id)
            pdf_bytes = generate_assessment_pdf_bytes(result)
            pdf_key = store.upload_assessment_pdf(current_user.id, result.assessment_id, pdf_bytes)
            payload = result.model_dump(mode="json")
            payload["pdf_url"] = f"/voice-training/assessments/{result.assessment_id}/pdf"
            store.save_assessment(payload, pdf_s3_key=pdf_key)
            session = store.get_session(req.session_id)
            if session:
                session["status"] = "completed"
                session["assessment_id"] = result.assessment_id
                store.save_session(session)
            from models.domain import VoiceScenario

            scen_row = (
                db.query(VoiceScenario).filter(VoiceScenario.id == result.scenario_id).first()
            )
            pub = get_publish_config(
                scen_row.content if scen_row and isinstance(scen_row.content, dict) else {}
            )
            pass_pct = int(pub.get("pass_percentage", 70))
            passed = float(result.final_percentage) >= pass_pct
            notify_voice_assessment_completed(
                db,
                trainee=current_user,
                scenario_title=result.scenario_title,
                author_id=scen_row.author_id if scen_row else None,
                final_percentage=float(result.final_percentage),
                grade=result.grade,
                pass_percentage=pass_pct,
                passed=passed,
            )
            db.commit()
            deps.log_activity(db, current_user.id, "voice_training_assessment", result.assessment_id)
            return payload
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=safe_client_error_detail(exc)) from exc
        except Exception as exc:
            logger.error(
                "Assessment evaluation failed for session %s: %s",
                log_safe(req.session_id),
                log_safe(exc),
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE) from exc

    @router.get("/assessments")
    async def list_assessments(
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        return store.list_assessments_for_user(current_user.id)

    @router.get("/assessments/{assessment_id}")
    async def get_assessment(
        assessment_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        data = store.get_assessment(assessment_id)
        _assert_assessment_owner(data, current_user, db)
        return data

    @router.post("/assessments/{assessment_id}/delete")
    async def delete_assessment(
        assessment_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        data = store.get_assessment(assessment_id)
        _assert_assessment_owner(data, current_user, db)
        if not store.delete_assessment(assessment_id):
            raise HTTPException(status_code=404, detail="Assessment not found")
        deps.log_activity(db, current_user.id, "voice_training_assessment_deleted", assessment_id)
        return {"success": True, "message": "Assessment deleted."}

    @router.get("/assessments/{assessment_id}/pdf")
    async def download_assessment_pdf(
        assessment_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        from models.domain import VoiceAssessment

        row = db.query(VoiceAssessment).filter(VoiceAssessment.id == assessment_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found")
        data = store.get_assessment(assessment_id)
        if not data:
            raise HTTPException(status_code=404, detail="Assessment not found")
        _assert_assessment_owner(data, current_user, db)

        pdf_key = row.pdf_s3_key or assessment_pdf_key(row.user_id, assessment_id)
        pdf_bytes = store.get_object_bytes(pdf_key)
        if not pdf_bytes:
            try:
                assessment = coerce_assessment_result(data)
                pdf_bytes = generate_assessment_pdf_bytes(assessment)
            except Exception as exc:
                logger.error(
                    "PDF generation failed for %s: %s",
                    log_safe(assessment_id),
                    log_safe(exc),
                    exc_info=True,
                )
                raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE) from exc
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{assessment_id}_report.pdf"'},
        )

    @router.get("/dashboard")
    async def get_dashboard(
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        assessments = store.list_assessments_for_user(current_user.id)
        if not assessments:
            return DashboardStats(
                scenarios_mastered=0,
                average_empathy_score=0,
                sop_compliance_rate=0,
                total_practice_sessions=store.count_practice_sessions(current_user.id),
                total_assessments=0,
                recent_assessments=[],
            )

        empathy_scores = []
        sop_rates = []
        mastered = set()
        for a in assessments:
            for m in a.get("metrics", []):
                if m.get("parameter") == "Empathy":
                    empathy_scores.append(m.get("score", 0))
            sop_rates.append(a.get("sop_compliance_rate", 0))
            if a.get("final_percentage", 0) >= 80:
                mastered.add(a.get("scenario_id"))

        return DashboardStats(
            scenarios_mastered=len(mastered),
            average_empathy_score=round(sum(empathy_scores) / max(len(empathy_scores), 1), 2),
            sop_compliance_rate=round(sum(sop_rates) / max(len(sop_rates), 1), 1),
            total_practice_sessions=store.count_practice_sessions(current_user.id),
            total_assessments=len(assessments),
            recent_assessments=assessments[:5],
        )

    @router.get("/admin/reports")
    async def admin_reports(
        process_id: str | None = None,
        role: str | None = None,
        author_id: str | None = None,
        user_id: str | None = None,
        publish_status: str | None = None,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_super_admin(current_user)
        store = _store(db)
        return store.get_admin_reports(
            process_id=process_id,
            role=role,
            author_id=author_id,
            user_id=user_id,
            publish_status=publish_status,
        )

    @router.post("/admin/scenarios/{scenario_id}/delete")
    async def admin_remove_scenario(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_super_admin(current_user)
        from models.domain import Notification

        store = _store(db)
        author_info = store.get_scenario_author(scenario_id)
        if not author_info:
            raise HTTPException(status_code=404, detail="Scenario not found")
        author_id, title = author_info
        archived = store.archive_scenario(scenario_id)
        if not archived:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if author_id != current_user.id:
            db.add(
                Notification(
                    user_id=author_id,
                    message=(
                        f"Your voice training scenario \"{title}\" was archived by a super admin. "
                        "You can restore it from Show archived in Voice Training Agent."
                    ),
                    related_link="/voice-training",
                )
            )
        deps.log_activity(
            db,
            current_user.id,
            "voice_training_scenario_admin_removed",
            scenario_id,
        )
        db.commit()
        return scenario_to_summary(ScenarioScript.model_validate(archived))

    @router.post("/admin/scenarios/{scenario_id}/restore")
    async def admin_restore_scenario(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_super_admin(current_user)
        store = _store(db)
        restored = store.restore_scenario(scenario_id)
        if not restored:
            raise HTTPException(status_code=404, detail="Scenario not found")
        deps.log_activity(db, current_user.id, "voice_training_scenario_admin_restored", scenario_id)
        return scenario_to_summary(ScenarioScript.model_validate(restored))

    @router.post("/admin/scenarios/{scenario_id}/permanent/delete")
    async def admin_permanent_delete_scenario(
        scenario_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_super_admin(current_user)
        store = _store(db)
        author_info = store.get_scenario_author(scenario_id)
        if not author_info:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if not store.is_scenario_archived(scenario_id):
            raise HTTPException(
                status_code=400,
                detail="Only archived scenarios can be permanently deleted.",
            )
        _, title = author_info
        if not store.permanent_delete_scenario(scenario_id):
            raise HTTPException(status_code=404, detail="Scenario not found")
        deps.log_activity(
            db,
            current_user.id,
            "voice_training_scenario_admin_deleted",
            f"{scenario_id}:{title}",
        )
        return {"success": True, "message": "Scenario permanently deleted."}

    @router.post("/admin/assessments/{assessment_id}/delete")
    async def admin_delete_assessment(
        assessment_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_super_admin(current_user)
        store = _store(db)
        if not store.get_assessment(assessment_id):
            raise HTTPException(status_code=404, detail="Assessment not found")
        if not store.delete_assessment(assessment_id):
            raise HTTPException(status_code=404, detail="Assessment not found")
        deps.log_activity(db, current_user.id, "voice_training_assessment_admin_deleted", assessment_id)
        return {"success": True, "message": "Assessment deleted."}

    @router.post("/admin/scenarios/bulk-archive")
    async def admin_bulk_archive_scenarios(
        req: BulkScenarioIdsRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_super_admin(current_user)
        from models.domain import Notification

        store = _store(db)
        archived_ids: list[str] = []
        for scenario_id in req.scenario_ids:
            author_info = store.get_scenario_author(scenario_id)
            if not author_info:
                continue
            author_id, title = author_info
            archived = store.archive_scenario(scenario_id)
            if not archived:
                continue
            archived_ids.append(scenario_id)
            if author_id != current_user.id:
                db.add(
                    Notification(
                        user_id=author_id,
                        message=(
                            f"Your voice training scenario \"{title}\" was archived by a super admin. "
                            "You can restore it from Show archived in Voice Training Agent."
                        ),
                        related_link="/voice-training",
                    )
                )
            deps.log_activity(
                db,
                current_user.id,
                "voice_training_scenario_admin_removed",
                scenario_id,
            )
        db.commit()
        return {
            "success": True,
            "archived": len(archived_ids),
            "message": f"Archived {len(archived_ids)} scenario(s).",
        }

    @router.post("/admin/scenarios/bulk-permanent-delete")
    async def admin_bulk_permanent_delete_scenarios(
        req: BulkScenarioIdsRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_super_admin(current_user)
        store = _store(db)
        deleted = 0
        for scenario_id in req.scenario_ids:
            if not store.is_scenario_archived(scenario_id):
                continue
            author_info = store.get_scenario_author(scenario_id)
            if not author_info:
                continue
            _, title = author_info
            if store.permanent_delete_scenario(scenario_id):
                deleted += 1
                deps.log_activity(
                    db,
                    current_user.id,
                    "voice_training_scenario_admin_deleted",
                    f"{scenario_id}:{title}",
                )
        db.commit()
        return {
            "success": True,
            "deleted": deleted,
            "message": f"Permanently deleted {deleted} archived scenario(s).",
        }

    @router.post("/admin/assessments/bulk-delete")
    async def admin_bulk_delete_assessments(
        req: BulkAssessmentIdsRequest,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_super_admin(current_user)
        store = _store(db)
        deleted = 0
        for assessment_id in req.assessment_ids:
            if not store.get_assessment(assessment_id):
                continue
            if store.delete_assessment(assessment_id):
                deleted += 1
                deps.log_activity(
                    db,
                    current_user.id,
                    "voice_training_assessment_admin_deleted",
                    assessment_id,
                )
        db.commit()
        return {
            "success": True,
            "deleted": deleted,
            "message": f"Deleted {deleted} assessment(s).",
        }

    @router.get("/audio/{session_id}/{turn_id}")
    async def get_tts_audio(
        session_id: str,
        turn_id: str,
        db: Session = Depends(deps.get_db),
        current_user=Depends(deps.get_current_user),
    ):
        require_access(current_user)
        store = _store(db)
        session = store.get_session(session_id)
        _assert_session_owner(session, current_user)
        try:
            safe_session = sanitize_id(session_id)
            safe_turn = sanitize_id(turn_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid audio reference") from exc
        key = session_tts_key(session["user_id"], safe_session, safe_turn)
        audio_bytes = store.get_object_bytes(key)
        if not audio_bytes:
            raise HTTPException(status_code=404, detail="Audio not found")
        return StreamingResponse(iter([audio_bytes]), media_type="audio/mpeg")

    return router


def _generation_payload(summaries: list, message: str, warnings: list[str]) -> dict:
    unique_warnings = list(dict.fromkeys(warnings))
    used_fallback = bool(unique_warnings)
    if used_fallback:
        message = f"{message} (used built-in templates — see warnings.)"
    return {
        "generated": len(summaries),
        "message": message,
        "scenarios": summaries,
        "source": "fallback" if used_fallback else "bedrock",
        "warnings": unique_warnings,
    }
