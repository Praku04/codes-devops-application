from __future__ import annotations

from core.runtime import *
from models.domain import JobStatus
from utils.security_helpers import log_safe

# Mirrors utils.video_analysis.status_store.JOB_TYPE, held as a literal rather than imported: that
# module's package imports the video analysis router, which reaches back into this one.
# scripts/check_video_report_indexing_recovers_frames.py asserts the two stay equal.
VIDEO_ANALYSIS_JOB_TYPE = "video_analysis"


def create_job_status(job_id: str, user_id: str, db: Session):
    """Create a new job status in database."""
    try:
        job_status = JobStatus(
            job_id=job_id,
            user_id=user_id,
            status="pending",
            progress_message="Job is queued...",
        )
        db.add(job_status)
        db.commit()

        job_statuses[job_id] = {
            "status": "pending",
            "result": None,
            "error": None,
            "progress_message": "Job is queued...",
            "user_id": user_id,
        }
        logger.info("Created persistent job status for %s", log_safe(job_id))
    except Exception as e:
        logger.error(
            "Failed to create job status for %s: %s",
            log_safe(job_id),
            log_safe(e),
        )
        db.rollback()


def update_job_status(
    job_id: str,
    status: str = None,
    progress_message: str = None,
    result: dict = None,
    error: str = None,
    token_usage: dict = None,
    cleanup_scheduled: bool = None,
):
    """Update job status in both memory and database."""
    try:
        if job_id in job_statuses:
            if status:
                job_statuses[job_id]["status"] = status
            if progress_message:
                job_statuses[job_id]["progress_message"] = progress_message
            if result:
                job_statuses[job_id]["result"] = result
            if error:
                job_statuses[job_id]["error"] = error
            if token_usage:
                job_statuses[job_id]["token_usage"] = token_usage
            if cleanup_scheduled is not None:
                job_statuses[job_id]["cleanup_scheduled"] = cleanup_scheduled

        db = SessionLocal()
        try:
            job_status = db.query(JobStatus).filter(JobStatus.job_id == job_id).first()
            if job_status:
                if status:
                    job_status.status = status
                    if status in ["completed", "failed", "cancelled"]:
                        job_status.completed_at = datetime.now(timezone.utc)
                if progress_message:
                    job_status.progress_message = progress_message
                if result:
                    job_status.result = result
                if error:
                    job_status.error = error
                if token_usage:
                    job_status.token_usage = token_usage
                if cleanup_scheduled is not None:
                    job_status.cleanup_scheduled = cleanup_scheduled
                job_status.updated_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error(
            "Failed to update job status for %s: %s",
            log_safe(job_id),
            log_safe(e),
        )


def get_job_status_data(job_id: str) -> dict:
    """Get job status from memory first, fallback to database."""
    if job_id in job_statuses:
        return job_statuses[job_id]

    db = SessionLocal()
    try:
        job_status = db.query(JobStatus).filter(JobStatus.job_id == job_id).first()
        if job_status:
            status_dict = {
                "status": job_status.status,
                "progress_message": job_status.progress_message,
                "result": job_status.result,
                "error": job_status.error,
                "token_usage": job_status.token_usage,
                "user_id": job_status.user_id,
                "cleanup_scheduled": job_status.cleanup_scheduled,
            }
            job_statuses[job_id] = status_dict
            return status_dict
    finally:
        db.close()

    return None


def cleanup_old_job_statuses():
    """
    Clean up old completed job statuses from database.

    Video analysis jobs are kept. Their result row is not progress chatter: it holds the S3 keys for
    the session's source video and every extracted figure frame, and the generated markdown before
    the report is exported. Reindexing a report and reopening a session from another worker both read
    it, so dropping it an hour after processing is what left "Index for Chat" with no frame source.
    """
    try:
        db = SessionLocal()
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=1)
            old_jobs = db.query(JobStatus).filter(
                JobStatus.status.in_(["completed", "failed"]),
                JobStatus.completed_at < cutoff_time,
            ).all()

            removed = 0
            for job in old_jobs:
                result = job.result if isinstance(job.result, dict) else {}
                if result.get("job_type") == VIDEO_ANALYSIS_JOB_TYPE:
                    continue
                db.delete(job)
                job_statuses.pop(job.job_id, None)
                removed += 1

            db.commit()
            if removed:
                logger.info("Cleaned up %s old job statuses", removed)
        finally:
            db.close()
    except Exception as e:
        logger.error("Failed to cleanup old job statuses: %s", log_safe(e))


class JobCancelledError(Exception):
    """Background job was cancelled by the user."""


def is_job_cancelled(job_id: str) -> bool:
    data = get_job_status_data(job_id)
    return bool(data and data.get("status") == "cancelled")


def raise_if_job_cancelled(job_id: str) -> None:
    if is_job_cancelled(job_id):
        raise JobCancelledError("Job cancelled by user")


def request_job_cancellation(job_id: str, user_id: str) -> bool:
    """Mark a pending/running job as cancelled. Returns True if already cancelled or newly cancelled."""
    data = get_job_status_data(job_id)
    if not data:
        return False
    if data.get("user_id") != user_id:
        return False
    current = data.get("status")
    if current == "cancelled":
        return True
    if current in ("completed", "failed"):
        # Job already finished — UI reset is still valid (e.g. discard uncommitted curation).
        return True
    if current not in ("pending", "running"):
        return False
    update_job_status(
        job_id,
        status="cancelled",
        progress_message="Cancelled by user.",
        error="Cancelled by user.",
    )
    logger.info(
        "Job %s cancelled by user %s",
        log_safe(job_id),
        log_safe(user_id),
    )
    return True


__all__ = [name for name in globals() if not name.startswith("_")]
