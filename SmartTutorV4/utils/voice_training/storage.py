"""PostgreSQL + S3 persistence for voice training (no local files)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import desc
from sqlalchemy.orm import Session

from models.domain import VoiceAssessment, VoiceScenario, VoiceSession
from utils.security_helpers import log_safe
from utils.timezone_helpers import now_ist
from utils.voice_training.s3_keys import assessment_pdf_key, scenario_doc_key, session_tts_key

logger = logging.getLogger(__name__)


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class VoiceTrainingStore:
    def __init__(self, db: Session, s3_client: Any, bucket: str):
        self.db = db
        self.s3 = s3_client
        self.bucket = bucket

    def upload_bytes(self, key: str, data: bytes, content_type: str) -> str:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return key

    def get_object_bytes(self, key: str) -> bytes | None:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except ClientError as exc:
            logger.debug("S3 get_object failed for %s: %s", log_safe(key), log_safe(exc))
            return None

    def delete_s3_object(self, key: str | None) -> None:
        if not key:
            return
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            logger.debug("S3 delete skipped for %s: %s", log_safe(key), log_safe(exc))

    def upload_scenario_document(
        self, user_id: str, scenario_id: str, filename: str, content: bytes, content_type: str
    ) -> str:
        key = scenario_doc_key(user_id, scenario_id, filename)
        self.upload_bytes(key, content, content_type)
        return key

    def upload_assessment_pdf(self, user_id: str, assessment_id: str, pdf_bytes: bytes) -> str:
        key = assessment_pdf_key(user_id, assessment_id)
        self.upload_bytes(key, pdf_bytes, "application/pdf")
        return key

    def upload_tts_audio(self, user_id: str, session_id: str, turn_id: str, audio_bytes: bytes) -> str:
        key = session_tts_key(user_id, session_id, turn_id)
        self.upload_bytes(key, audio_bytes, "audio/mpeg")
        return key

    def save_scenario(
        self,
        scenario: dict,
        author_id: str,
        process_id: str | None,
    ) -> dict:
        row = self.db.query(VoiceScenario).filter(VoiceScenario.id == scenario["id"]).first()
        cat = scenario.get("category")
        if hasattr(cat, "value"):
            cat = cat.value
        payload = {
            "category": cat,
            "title": scenario["title"],
            "content": scenario,
        }
        if row:
            row.title = payload["title"]
            row.category = payload["category"]
            row.content = payload["content"]
        else:
            row = VoiceScenario(
                id=scenario["id"],
                author_id=author_id,
                process_id=process_id,
                category=payload["category"],
                title=payload["title"],
                content=payload["content"],
            )
            self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row.content

    def get_scenario(self, scenario_id: str) -> dict | None:
        row = self.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        return row.content if row else None

    def list_scenarios(
        self,
        category: str | None = None,
        process_id: str | None = None,
        include_global: bool = True,
        include_archived: bool = False,
        archived_only: bool = False,
        author_id: str | None = None,
    ) -> list[dict]:
        query = self.db.query(VoiceScenario)
        if author_id:
            query = query.filter(VoiceScenario.author_id == author_id)
        if process_id and include_global:
            from sqlalchemy import or_

            query = query.filter(or_(VoiceScenario.process_id == process_id, VoiceScenario.process_id.is_(None)))
        elif process_id:
            query = query.filter(VoiceScenario.process_id == process_id)
        if category:
            query = query.filter(VoiceScenario.category == category)
        rows = query.order_by(desc(VoiceScenario.created_at)).all()
        results: list[dict] = []
        for row in rows:
            content = row.content or {}
            is_archived = bool(content.get("archived"))
            if archived_only and not is_archived:
                continue
            if not include_archived and not archived_only and is_archived:
                continue
            results.append(content)
        return results

    def list_scenarios_org_wide(
        self,
        category: str | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        """All scenarios across processes/users — used to enforce global uniqueness."""
        query = self.db.query(VoiceScenario)
        if category:
            query = query.filter(VoiceScenario.category == category)
        rows = query.order_by(desc(VoiceScenario.created_at)).all()
        results: list[dict] = []
        for row in rows:
            content = row.content or {}
            if not include_archived and bool(content.get("archived")):
                continue
            results.append(content)
        return results

    def archive_scenario(self, scenario_id: str) -> dict | None:
        row = self.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            return None
        content = dict(row.content or {})
        content["archived"] = True
        content["archived_at"] = now_ist().isoformat()
        row.content = content
        self.db.commit()
        self.db.refresh(row)
        return row.content

    def restore_scenario(self, scenario_id: str) -> dict | None:
        row = self.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            return None
        content = dict(row.content or {})
        content["archived"] = False
        content["archived_at"] = None
        row.content = content
        self.db.commit()
        self.db.refresh(row)
        return row.content

    def permanent_delete_scenario(self, scenario_id: str) -> bool:
        """Hard-delete a scenario and related sessions/assessments. Only allowed when archived."""
        row = self.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            return False
        if not (row.content or {}).get("archived"):
            return False

        assessments = (
            self.db.query(VoiceAssessment)
            .filter(VoiceAssessment.scenario_id == scenario_id)
            .all()
        )
        for assessment in assessments:
            pdf_key = assessment.pdf_s3_key or assessment_pdf_key(assessment.user_id, assessment.id)
            self.delete_s3_object(pdf_key)
            self.db.delete(assessment)

        sessions = (
            self.db.query(VoiceSession)
            .filter(VoiceSession.scenario_id == scenario_id)
            .all()
        )
        for session in sessions:
            self.db.delete(session)

        self.db.delete(row)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return True

    def clear_scenario_trainee_progress(
        self,
        scenario_id: str,
        trainee_ids: list[str] | None = None,
    ) -> dict[str, int]:
        """Remove practice sessions and assessments for a scenario (scoped to trainees if given)."""
        session_query = self.db.query(VoiceSession).filter(VoiceSession.scenario_id == scenario_id)
        assessment_query = self.db.query(VoiceAssessment).filter(
            VoiceAssessment.scenario_id == scenario_id
        )
        if trainee_ids is not None:
            if not trainee_ids:
                return {"sessions_removed": 0, "assessments_removed": 0}
            session_query = session_query.filter(VoiceSession.user_id.in_(trainee_ids))
            assessment_query = assessment_query.filter(VoiceAssessment.user_id.in_(trainee_ids))

        assessments = assessment_query.all()
        for assessment in assessments:
            pdf_key = assessment.pdf_s3_key or assessment_pdf_key(
                assessment.user_id, assessment.id
            )
            self.delete_s3_object(pdf_key)
            self.db.delete(assessment)

        sessions = session_query.all()
        for session in sessions:
            self.db.delete(session)

        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {
            "sessions_removed": len(sessions),
            "assessments_removed": len(assessments),
        }

    def is_scenario_archived(self, scenario_id: str) -> bool | None:
        row = self.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            return None
        return bool((row.content or {}).get("archived"))

    def count_scenarios(self, active_only: bool = True) -> int:
        if not active_only:
            return self.db.query(VoiceScenario).count()
        rows = self.db.query(VoiceScenario).all()
        return sum(1 for row in rows if not (row.content or {}).get("archived"))

    def save_session(self, session: dict) -> dict:
        row = self.db.query(VoiceSession).filter(VoiceSession.id == session["session_id"]).first()
        if row:
            row.content = session
            row.mode = session.get("mode", row.mode)
            row.status = session.get("status", row.status)
            row.updated_at = now_ist()
        else:
            row = VoiceSession(
                id=session["session_id"],
                user_id=session["user_id"],
                scenario_id=session["scenario_id"],
                mode=session.get("mode", "practice"),
                content=session,
                status=session.get("status", "active"),
            )
            self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row.content

    def get_session(self, session_id: str) -> dict | None:
        row = self.db.query(VoiceSession).filter(VoiceSession.id == session_id).first()
        return row.content if row else None

    def save_assessment(self, assessment: dict, pdf_s3_key: str | None = None) -> dict:
        row = self.db.query(VoiceAssessment).filter(
            VoiceAssessment.id == assessment["assessment_id"]
        ).first()
        final_pct = int(round(float(assessment.get("final_percentage", 0))))
        if row:
            row.content = assessment
            row.pdf_s3_key = pdf_s3_key or row.pdf_s3_key
            row.final_percentage = final_pct
            row.grade = assessment.get("grade")
        else:
            row = VoiceAssessment(
                id=assessment["assessment_id"],
                user_id=assessment["user_id"],
                session_id=assessment["session_id"],
                scenario_id=assessment["scenario_id"],
                content=assessment,
                pdf_s3_key=pdf_s3_key,
                final_percentage=final_pct,
                grade=assessment.get("grade"),
            )
            self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row.content

    def get_assessment(self, assessment_id: str) -> dict | None:
        row = self.db.query(VoiceAssessment).filter(VoiceAssessment.id == assessment_id).first()
        if not row:
            return None
        data = dict(row.content)
        data["assessment_id"] = row.id
        data["user_id"] = row.user_id
        if row.pdf_s3_key:
            data["pdf_s3_key"] = row.pdf_s3_key
        if row.created_at:
            data["created_at"] = row.created_at.isoformat()
        data["pdf_url"] = f"/voice-training/assessments/{row.id}/pdf"
        return data

    def get_assessment_by_session(self, session_id: str) -> dict | None:
        row = (
            self.db.query(VoiceAssessment)
            .filter(VoiceAssessment.session_id == session_id)
            .order_by(desc(VoiceAssessment.created_at))
            .first()
        )
        if not row:
            return None
        return self.get_assessment(row.id)

    def delete_assessment(self, assessment_id: str) -> bool:
        row = self.db.query(VoiceAssessment).filter(VoiceAssessment.id == assessment_id).first()
        if not row:
            return False
        pdf_key = row.pdf_s3_key or assessment_pdf_key(row.user_id, assessment_id)
        self.delete_s3_object(pdf_key)
        self.db.delete(row)
        self.db.commit()
        return True

    def list_assessments_for_user(self, user_id: str, limit: int = 50) -> list[dict]:
        rows = (
            self.db.query(VoiceAssessment)
            .filter(VoiceAssessment.user_id == user_id)
            .order_by(desc(VoiceAssessment.created_at))
            .limit(limit)
            .all()
        )
        items: list[dict] = []
        for row in rows:
            data = dict(row.content)
            data["assessment_id"] = row.id
            data["user_id"] = row.user_id
            data["scenario_id"] = row.scenario_id
            if row.pdf_s3_key:
                data["pdf_s3_key"] = row.pdf_s3_key
            if row.created_at:
                data["created_at"] = row.created_at.isoformat()
            data["pdf_url"] = f"/voice-training/assessments/{row.id}/pdf"
            items.append(data)
        return self.enrich_assessment_attempts(items)

    def enrich_assessment_attempts(self, items: list[dict]) -> list[dict]:
        """Add attempt_number, attempt_total, passed, and pass_percentage per assessment row."""
        if not items:
            return items
        from models.domain import VoiceScenario
        from utils.voice_training.publish import get_publish_config

        scenario_ids = {item.get("scenario_id") for item in items if item.get("scenario_id")}
        pass_map: dict[str, int] = {}
        for sid in scenario_ids:
            row = self.db.query(VoiceScenario).filter(VoiceScenario.id == sid).first()
            if row:
                pass_map[sid] = int(get_publish_config(row.content or {}).get("pass_percentage", 70))

        groups: dict[tuple[str, str], list[dict]] = {}
        for item in items:
            uid = item.get("user_id") or ""
            sid = item.get("scenario_id") or ""
            groups.setdefault((uid, sid), []).append(item)

        for (_uid, sid), group in groups.items():
            group.sort(key=lambda x: x.get("created_at") or "")
            pass_pct = pass_map.get(sid, 70)
            total = len(group)
            for index, item in enumerate(group, start=1):
                pct = float(item.get("final_percentage") or 0)
                item["attempt_number"] = index
                item["attempt_total"] = total
                item["pass_percentage"] = pass_pct
                item["passed"] = pct >= pass_pct
        return items

    def list_assessments_for_user_scenario(
        self,
        user_id: str,
        scenario_id: str,
    ) -> list[dict]:
        rows = (
            self.db.query(VoiceAssessment)
            .filter(
                VoiceAssessment.user_id == user_id,
                VoiceAssessment.scenario_id == scenario_id,
            )
            .order_by(VoiceAssessment.created_at.asc())
            .all()
        )
        items: list[dict] = []
        for row in rows:
            data = dict(row.content or {})
            data["assessment_id"] = row.id
            data["user_id"] = row.user_id
            data["scenario_id"] = row.scenario_id
            if row.pdf_s3_key:
                data["pdf_s3_key"] = row.pdf_s3_key
            if row.created_at:
                data["created_at"] = row.created_at.isoformat()
            data["pdf_url"] = f"/voice-training/assessments/{row.id}/pdf"
            items.append(data)
        return self.enrich_assessment_attempts(items)

    def list_assessments_for_trainees(
        self,
        trainee_ids: list[str] | None,
        limit: int = 200,
        scenario_id: str | None = None,
        author_id: str | None = None,
    ) -> list[dict]:
        from models.domain import User, VoiceScenario

        query = (
            self.db.query(VoiceAssessment, User, VoiceScenario)
            .join(User, VoiceAssessment.user_id == User.id)
            .join(VoiceScenario, VoiceAssessment.scenario_id == VoiceScenario.id)
            .filter(User.role == "trainee")
        )
        if trainee_ids is not None:
            if not trainee_ids:
                return []
            query = query.filter(VoiceAssessment.user_id.in_(trainee_ids))
        if scenario_id:
            query = query.filter(VoiceAssessment.scenario_id == scenario_id)
        if author_id:
            query = query.filter(VoiceScenario.author_id == author_id)
        rows = query.order_by(desc(VoiceAssessment.created_at)).limit(limit).all()
        items: list[dict] = []
        for row, user, scen in rows:
            data = dict(row.content or {})
            data["assessment_id"] = row.id
            data["user_id"] = user.id
            data["user_email"] = user.email
            data["user_role"] = user.role
            data["scenario_id"] = row.scenario_id
            data["scenario_title"] = data.get("scenario_title") or scen.title
            if row.pdf_s3_key:
                data["pdf_s3_key"] = row.pdf_s3_key
            if row.created_at:
                data["created_at"] = row.created_at.isoformat()
            data["pdf_url"] = f"/voice-training/assessments/{row.id}/pdf"
            items.append(data)
        return self.enrich_assessment_attempts(items)

    def build_scenario_trainee_assessment_results(
        self,
        scenario_id: str,
        active_trainees: list[dict],
        pass_percentage: int = 70,
    ) -> list[dict]:
        """Per-trainee assessment summary for a published scenario (all attempts)."""
        trainee_ids = [t.get("id") for t in active_trainees if t.get("id")]
        assessments = self.list_assessments_for_trainees(
            trainee_ids,
            scenario_id=scenario_id,
            limit=500,
        )
        by_user: dict[str, list[dict]] = {}
        for assessment in assessments:
            by_user.setdefault(assessment["user_id"], []).append(assessment)

        results: list[dict] = []
        for trainee in active_trainees:
            tid = trainee.get("id")
            if not tid:
                continue
            attempts = sorted(by_user.get(tid, []), key=lambda x: x.get("created_at") or "")
            best = max((float(a.get("final_percentage") or 0) for a in attempts), default=0.0)
            results.append(
                {
                    "trainee_id": tid,
                    "trainee_email": trainee.get("email") or "",
                    "attempts": [
                        {
                            "assessment_id": a.get("assessment_id"),
                            "attempt_number": a.get("attempt_number", 1),
                            "final_percentage": a.get("final_percentage"),
                            "grade": a.get("grade"),
                            "passed": bool(a.get("passed")),
                            "created_at": a.get("created_at"),
                        }
                        for a in attempts
                    ],
                    "completed_count": len(attempts),
                    "best_percentage": round(best, 1),
                    "has_passed": any(bool(a.get("passed")) for a in attempts),
                    "pass_percentage": pass_percentage,
                }
            )
        return results

    def count_practice_sessions(self, user_id: str, scenario_id: str | None = None) -> int:
        query = self.db.query(VoiceSession).filter(
            VoiceSession.user_id == user_id,
            VoiceSession.mode == "practice",
            VoiceSession.status == "completed",
        )
        if scenario_id:
            query = query.filter(VoiceSession.scenario_id == scenario_id)
        return query.count()

    def count_assessment_attempts(self, user_id: str, scenario_id: str) -> int:
        return (
            self.db.query(VoiceAssessment)
            .filter(
                VoiceAssessment.user_id == user_id,
                VoiceAssessment.scenario_id == scenario_id,
            )
            .count()
        )

    def get_assessment_pass_status(
        self,
        user_id: str,
        scenario_id: str,
        pass_percentage: int,
    ) -> dict[str, float | bool]:
        rows = (
            self.db.query(VoiceAssessment)
            .filter(
                VoiceAssessment.user_id == user_id,
                VoiceAssessment.scenario_id == scenario_id,
            )
            .order_by(desc(VoiceAssessment.created_at))
            .all()
        )
        best = 0.0
        has_passed = False
        for row in rows:
            content = row.content if isinstance(row.content, dict) else {}
            pct = float(row.final_percentage or content.get("final_percentage") or 0)
            best = max(best, pct)
            if pct >= pass_percentage:
                has_passed = True
        return {
            "has_passed": has_passed,
            "best_percentage": round(best, 1),
        }

    def list_all_assessments_admin(
        self,
        process_id: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        return self.get_admin_reports(process_id=process_id, limit=limit)["assessments"]

    def get_admin_reports(
        self,
        process_id: str | None = None,
        role: str | None = None,
        author_id: str | None = None,
        user_id: str | None = None,
        publish_status: str | None = None,
        limit: int = 200,
    ) -> dict:
        from models.domain import Process, User
        from sqlalchemy import or_
        from utils.voice_training.publish import get_publish_config, is_assessment_configured

        def _iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        def _role_filter(query, user_model):
            if role and role.strip():
                return query.filter(user_model.role == role.strip())
            return query

        scenario_query = (
            self.db.query(VoiceScenario, User, Process)
            .join(User, VoiceScenario.author_id == User.id)
            .outerjoin(Process, User.process_id == Process.id)
            .order_by(desc(VoiceScenario.created_at))
        )
        if process_id:
            scenario_query = scenario_query.filter(
                or_(VoiceScenario.process_id == process_id, User.process_id == process_id)
            )
        if author_id:
            scenario_query = scenario_query.filter(VoiceScenario.author_id == author_id)
        if role and role.strip() in ("trainer", "super_admin"):
            scenario_query = _role_filter(scenario_query, User)

        scenarios: list[dict] = []
        category_counts: dict[str, int] = {}
        role_counts: dict[str, int] = {}
        archived_count = 0
        published_count = 0
        publish_filter = (publish_status or "").strip().lower()
        for scen, author, proc in scenario_query.limit(limit * 3).all():
            content = scen.content if isinstance(scen.content, dict) else {}
            is_archived = bool(content.get("archived"))
            publish = get_publish_config(content)
            is_published = bool(publish.get("is_published"))
            if is_archived:
                archived_count += 1
            if is_published and not is_archived:
                published_count += 1
            if publish_filter == "published" and not is_published:
                continue
            if publish_filter == "unpublished" and is_published:
                continue
            cat = scen.category or "custom"
            category_counts[cat] = category_counts.get(cat, 0) + 1
            role_counts[author.role] = role_counts.get(author.role, 0) + 1
            scenarios.append(
                {
                    "id": scen.id,
                    "title": scen.title,
                    "category": scen.category,
                    "author_id": author.id,
                    "author_email": author.email,
                    "author_role": author.role,
                    "process_name": proc.name if proc else "—",
                    "process_id": proc.id if proc else None,
                    "turn_count": len(content.get("success_script") or []),
                    "sop_count": len(content.get("sop_activities") or []),
                    "archived": is_archived,
                    "is_published": is_published,
                    "assessment_configured": is_assessment_configured(content),
                    "created_at": _iso(scen.created_at),
                }
            )
            if len(scenarios) >= limit:
                break

        assessment_query = (
            self.db.query(VoiceAssessment, User, VoiceScenario, Process)
            .join(User, VoiceAssessment.user_id == User.id)
            .join(VoiceScenario, VoiceAssessment.scenario_id == VoiceScenario.id)
            .outerjoin(Process, User.process_id == Process.id)
            .order_by(desc(VoiceAssessment.created_at))
        )
        if process_id:
            assessment_query = assessment_query.filter(User.process_id == process_id)
        if user_id:
            assessment_query = assessment_query.filter(VoiceAssessment.user_id == user_id)
        assessment_query = _role_filter(assessment_query, User)

        assessments: list[dict] = []
        for assessment, user, scen, proc in assessment_query.limit(limit).all():
            item = dict(assessment.content)
            item["assessment_id"] = assessment.id
            item["user_id"] = user.id
            item["scenario_id"] = assessment.scenario_id
            item["user_email"] = user.email
            item["user_role"] = user.role
            item["scenario_title"] = item.get("scenario_title") or scen.title
            item["process_name"] = proc.name if proc else "—"
            item["pdf_s3_key"] = assessment.pdf_s3_key
            item["created_at"] = _iso(assessment.created_at)
            item["pdf_url"] = f"/voice-training/assessments/{assessment.id}/pdf"
            assessments.append(item)
        assessments = self.enrich_assessment_attempts(assessments)

        scenario_count_q = self.db.query(VoiceScenario)
        assessment_count_q = self.db.query(VoiceAssessment)
        if process_id:
            scenario_count_q = scenario_count_q.filter(
                or_(
                    VoiceScenario.process_id == process_id,
                    VoiceScenario.author_id.in_(
                        self.db.query(User.id).filter(User.process_id == process_id)
                    ),
                )
            )
            assessment_count_q = assessment_count_q.join(User).filter(User.process_id == process_id)
        if author_id:
            scenario_count_q = scenario_count_q.filter(VoiceScenario.author_id == author_id)
        if user_id:
            assessment_count_q = assessment_count_q.filter(VoiceAssessment.user_id == user_id)

        if role and role.strip():
            if not process_id:
                assessment_count_q = assessment_count_q.join(User, VoiceAssessment.user_id == User.id)
            assessment_count_q = assessment_count_q.filter(User.role == role.strip())
        if role and role.strip() in ("trainer", "super_admin"):
            scenario_count_q = scenario_count_q.join(User, VoiceScenario.author_id == User.id).filter(
                User.role == role.strip()
            )

        processes = [
            {"id": p.id, "name": p.name}
            for p in self.db.query(Process).order_by(Process.name).all()
        ]
        creators = (
            self.db.query(User.id, User.email, User.role)
            .join(VoiceScenario, VoiceScenario.author_id == User.id)
            .filter(User.role.in_(["trainer", "super_admin"]))
        )
        if process_id:
            creators = creators.filter(User.process_id == process_id)
        creator_list = [
            {"id": c.id, "email": c.email, "role": c.role}
            for c in creators.distinct().order_by(User.email).all()
        ]
        assessment_user_query = (
            self.db.query(User.id, User.email, User.role)
            .join(VoiceAssessment, VoiceAssessment.user_id == User.id)
            .distinct()
        )
        if process_id:
            assessment_user_query = assessment_user_query.filter(User.process_id == process_id)
        if role and role.strip():
            assessment_user_query = assessment_user_query.filter(User.role == role.strip())
        assessment_user_list = [
            {"id": u.id, "email": u.email, "role": u.role}
            for u in assessment_user_query.order_by(User.email).all()
        ]

        return {
            "summary": {
                "total_scenarios": scenario_count_q.count(),
                "archived_scenarios": archived_count,
                "published_scenarios": published_count,
                "unpublished_scenarios": max(0, scenario_count_q.count() - archived_count - published_count),
                "total_assessments": assessment_count_q.count(),
            },
            "analytics": {
                "by_category": category_counts,
                "by_creator_role": role_counts,
            },
            "filters": {
                "processes": processes,
                "creators": creator_list,
                "assessment_users": assessment_user_list,
                "roles": ["trainee", "trainer", "super_admin"],
                "author_roles": ["trainer", "super_admin"],
            },
            "scenarios": scenarios,
            "assessments": assessments,
        }

    def get_scenario_author(self, scenario_id: str) -> tuple[str, str] | None:
        row = self.db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
        if not row:
            return None
        return row.author_id, row.title

    @staticmethod
    def json_default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return str(obj)

    def dump_assessment_json(self, assessment: dict) -> str:
        return json.dumps(assessment, default=self.json_default)
