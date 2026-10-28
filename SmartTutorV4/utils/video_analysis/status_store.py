"""Database-backed video analysis status for multi-worker deployments."""
from __future__ import annotations

from utils.timezone_helpers import now_ist
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy.orm import Session

from core.runtime import SessionLocal, logger
from models.domain import JobStatus
from utils.security_helpers import log_safe

if TYPE_CHECKING:
    # Import-time only: session.py imports from this module, so a real import here
    # would be circular. `from __future__ import annotations` keeps the return
    # annotation below a string at runtime; this block just lets type checkers and
    # static analysers resolve the name instead of reporting it undefined.
    from .session import VideoAnalysisSession

JOB_TYPE = "video_analysis"


def _result_payload(session, include_markdown: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "job_type": JOB_TYPE,
        "progress": int(getattr(session, "progress", 0) or 0),
    }
    if include_markdown:
        markdown = getattr(session, "generated_markdown", "") or ""
        if markdown:
            payload["markdown"] = markdown
    video_s3_key = getattr(session, "video_s3_key", None)
    if video_s3_key:
        payload["video_s3_key"] = video_s3_key
    frame_s3_keys = getattr(session, "frame_s3_keys", None) or {}
    if frame_s3_keys:
        payload["frame_s3_keys"] = dict(frame_s3_keys)
    video_original = getattr(session, "video_original_filename", None)
    if video_original:
        payload["video_original_filename"] = str(video_original)[:180]
    transcript_original = getattr(session, "transcript_original_filename", None)
    if transcript_original:
        payload["transcript_original_filename"] = str(transcript_original)[:180]
    return payload


def _merge_job_result(existing: Optional[Dict[str, Any]], new_result: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(new_result)
    if not isinstance(existing, dict):
        return merged
    prev_frames = existing.get("frame_s3_keys") or {}
    next_frames = merged.get("frame_s3_keys") or {}
    if isinstance(prev_frames, dict) or isinstance(next_frames, dict):
        combined = dict(prev_frames if isinstance(prev_frames, dict) else {})
        combined.update(next_frames if isinstance(next_frames, dict) else {})
        if combined:
            merged["frame_s3_keys"] = combined
    if not merged.get("video_s3_key") and existing.get("video_s3_key"):
        merged["video_s3_key"] = existing["video_s3_key"]
    if not merged.get("markdown") and existing.get("markdown"):
        merged["markdown"] = existing["markdown"]
    if not merged.get("video_original_filename") and existing.get("video_original_filename"):
        merged["video_original_filename"] = existing["video_original_filename"]
    if not merged.get("transcript_original_filename") and existing.get(
        "transcript_original_filename"
    ):
        merged["transcript_original_filename"] = existing["transcript_original_filename"]
    return merged


def create_video_analysis_status(db: Session, session_id: str, user_id: str) -> None:
    """Create a durable status row when a session is uploaded."""
    try:
        existing = db.query(JobStatus).filter(JobStatus.job_id == session_id).first()
        if existing:
            return
        db.add(
            JobStatus(
                job_id=session_id,
                user_id=user_id,
                status="uploaded",
                progress_message="Files uploaded successfully",
                result={"job_type": JOB_TYPE, "progress": 0},
            )
        )
        db.commit()
    except Exception as exc:
        logger.error(
            "Failed to create video analysis status for %s: %s",
            log_safe(session_id),
            log_safe(exc),
        )
        db.rollback()


def persist_video_analysis_status(session) -> None:
    """Persist in-memory session progress so any worker can serve /status."""
    session_id = getattr(session, "session_id", None)
    user_id = getattr(session, "user_id", None)
    if not session_id or not user_id:
        return

    db = SessionLocal()
    try:
        job = db.query(JobStatus).filter(JobStatus.job_id == session_id).first()
        include_markdown = getattr(session, "status", "") == "completed"
        result = _result_payload(session, include_markdown=include_markdown)
        message = getattr(session, "message", "") or ""
        status = getattr(session, "status", "processing") or "processing"
        terminal = status in {"completed", "error", "cancelled"}

        if job:
            existing_result = job.result if isinstance(job.result, dict) else {}
            result = _merge_job_result(existing_result, result)
            job.status = status
            job.progress_message = message
            job.result = result
            job.updated_at = now_ist()
            if terminal:
                job.completed_at = now_ist()
        else:
            job = JobStatus(
                job_id=session_id,
                user_id=user_id,
                status=status,
                progress_message=message,
                result=result,
            )
            if terminal:
                job.completed_at = now_ist()
            db.add(job)
        db.commit()
    except Exception as exc:
        logger.error(
            "Failed to persist video analysis status for %s: %s",
            log_safe(session_id),
            log_safe(exc),
        )
        db.rollback()
    finally:
        db.close()


def _job_to_status_response(job: JobStatus) -> Dict[str, Any]:
    result = job.result if isinstance(job.result, dict) else {}
    progress = int(result.get("progress", 0) or 0)
    markdown = result.get("markdown") or ""
    return {
        "session_id": job.job_id,
        "status": job.status,
        "progress": progress,
        "message": job.progress_message or "",
        "markdown": markdown if job.status == "completed" else "",
        "word_count": len(markdown.split()) if markdown else 0,
    }


def _get_owned_video_analysis_job(
    db: Session,
    session_id: str,
    user_id: str,
    *,
    is_super_admin: bool = False,
) -> Optional[JobStatus]:
    job = db.query(JobStatus).filter(JobStatus.job_id == session_id).first()
    if not job:
        return None
    result = job.result if isinstance(job.result, dict) else {}
    if result.get("job_type") != JOB_TYPE:
        return None
    if job.user_id != user_id and not is_super_admin:
        return None
    return job


def save_video_analysis_markdown_to_db(
    db: Session,
    session_id: str,
    user_id: str,
    markdown: str,
    *,
    is_super_admin: bool = False,
) -> bool:
    """Persist edited markdown so save works across workers."""
    job = _get_owned_video_analysis_job(
        db,
        session_id,
        user_id,
        is_super_admin=is_super_admin,
    )
    if not job:
        return False

    try:
        result = job.result if isinstance(job.result, dict) else {}
        merged = _merge_job_result(
            result,
            {
                "job_type": JOB_TYPE,
                "progress": int(result.get("progress", 100) or 100),
                "markdown": markdown,
            },
        )
        job.result = merged
        if job.status not in {"completed", "cancelled", "error"}:
            job.status = "completed"
        job.progress_message = "Markdown saved"
        job.updated_at = now_ist()
        db.commit()
        return True
    except Exception as exc:
        logger.error(
            "Failed to save markdown for session %s: %s",
            log_safe(session_id),
            log_safe(exc),
        )
        db.rollback()
        return False


def hydrate_video_analysis_session_from_db(
    db: Session,
    session_id: str,
    user_id: str,
    *,
    is_super_admin: bool = False,
) -> Optional["VideoAnalysisSession"]:
    """Build an in-memory session snapshot from durable job state."""
    from .session import VideoAnalysisSession

    job = _get_owned_video_analysis_job(
        db,
        session_id,
        user_id,
        is_super_admin=is_super_admin,
    )
    if not job:
        return None

    result = job.result if isinstance(job.result, dict) else {}
    session = VideoAnalysisSession(session_id)
    session.user_id = job.user_id
    session.status = job.status
    session.message = job.progress_message or ""
    session.progress = int(result.get("progress", 0) or 0)
    session.generated_markdown = result.get("markdown") or ""
    session.video_s3_key = result.get("video_s3_key")
    session.frame_s3_keys = dict(result.get("frame_s3_keys") or {})
    session.video_original_filename = result.get("video_original_filename")
    session.transcript_original_filename = result.get("transcript_original_filename")
    session.extracted_frames = {}
    return session


def get_video_analysis_status_from_db(
    db: Session,
    session_id: str,
    current_user_id: str,
    *,
    allow_super_admin: bool = False,
    is_super_admin: bool = False,
) -> Optional[Dict[str, Any]]:
    """Load status from DB when the in-memory session is on another worker."""
    job = db.query(JobStatus).filter(JobStatus.job_id == session_id).first()
    if not job:
        return None

    result = job.result if isinstance(job.result, dict) else {}
    if result.get("job_type") != JOB_TYPE:
        return None

    if job.user_id != current_user_id and not (allow_super_admin and is_super_admin):
        return None

    return _job_to_status_response(job)
