"""Shared S3-backed frame storage for video analysis (multi-worker safe)."""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Dict, Optional, Tuple

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from core.runtime import S3_BUCKET_NAME, SessionLocal, logger, s3_client
from models.domain import JobStatus
from utils.security_helpers import log_safe
from utils.video_processor import extract_frame_with_fallback, normalize_video_timestamp

from .session import VideoAnalysisSession, video_analysis_sessions
from .status_store import JOB_TYPE, persist_video_analysis_status

_FRAME_CACHE_HEADERS = {"Cache-Control": "private, max-age=3600"}


def session_video_s3_key(user_id: str, session_id: str, local_path: str) -> str:
    ext = os.path.splitext(local_path or "")[1] or ".mp4"
    return f"video-analysis/sessions/{user_id}/{session_id}{ext}"


def session_frame_s3_key(user_id: str, session_id: str, normalized_ts: str) -> str:
    safe_ts = normalized_ts.replace(":", "-")
    return f"video-analysis/sessions/{user_id}/{session_id}/frames/frame_{safe_ts}.jpg"


def _read_file_bytes(path: str) -> Optional[bytes]:
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return None
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _s3_object_bytes(s3_key: str) -> Optional[bytes]:
    if not s3_key:
        return None
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        body = response.get("Body")
        if not body:
            return None
        data = body.read()
        return data if data else None
    except ClientError:
        return None
    except Exception as exc:
        logger.warning("Failed to read S3 frame object: %s", log_safe(exc))
        return None


def _upload_bytes_to_s3(s3_key: str, payload: bytes, content_type: str) -> bool:
    if not s3_key or not payload:
        return False
    try:
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=payload,
            ContentType=content_type,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to upload to S3 key %s: %s", log_safe(s3_key), log_safe(exc))
        return False


def upload_session_video_file(session: VideoAnalysisSession) -> Optional[str]:
    user_id = getattr(session, "user_id", None)
    session_id = getattr(session, "session_id", None)
    video_path = getattr(session, "video_path", None)
    if not user_id or not session_id or not video_path or not os.path.isfile(video_path):
        return None

    existing = getattr(session, "video_s3_key", None)
    if existing:
        return existing

    s3_key = session_video_s3_key(user_id, session_id, video_path)
    try:
        with open(video_path, "rb") as video_file:
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=s3_key,
                Body=video_file,
                ContentType="video/mp4",
            )
        session.video_s3_key = s3_key
        logger.info("[VIDEO ANALYSIS] Uploaded session video to S3 for %s", log_safe(session_id))
        return s3_key
    except Exception as exc:
        logger.warning(
            "[VIDEO ANALYSIS] Failed to upload session video for %s: %s",
            log_safe(session_id),
            log_safe(exc),
        )
        return None


def register_session_frame_s3(
    session: VideoAnalysisSession,
    normalized_ts: str,
    local_path: str,
) -> Optional[str]:
    """Upload a local frame JPEG to S3 and register it on the session."""
    user_id = getattr(session, "user_id", None)
    session_id = getattr(session, "session_id", None)
    if not user_id or not session_id or not normalized_ts:
        return None

    if not hasattr(session, "frame_s3_keys") or session.frame_s3_keys is None:
        session.frame_s3_keys = {}

    existing_key = session.frame_s3_keys.get(normalized_ts)
    if existing_key and _s3_object_bytes(existing_key):
        return existing_key

    frame_bytes = _read_file_bytes(local_path)
    if not frame_bytes:
        return None

    s3_key = session_frame_s3_key(user_id, session_id, normalized_ts)
    if not _upload_bytes_to_s3(s3_key, frame_bytes, "image/jpeg"):
        return None

    session.frame_s3_keys[normalized_ts] = s3_key
    return s3_key


def _load_job_assets(
    db: Session,
    session_id: str,
    user_id: str,
    *,
    is_super_admin: bool = False,
) -> Optional[Tuple[str, Dict[str, str], str]]:
    job = db.query(JobStatus).filter(JobStatus.job_id == session_id).first()
    if not job:
        return None
    result = job.result if isinstance(job.result, dict) else {}
    if result.get("job_type") != JOB_TYPE:
        return None
    if job.user_id != user_id and not is_super_admin:
        return None
    frame_s3_keys = result.get("frame_s3_keys") or {}
    if not isinstance(frame_s3_keys, dict):
        frame_s3_keys = {}
    return job.user_id, frame_s3_keys, result.get("video_s3_key") or ""


def _local_frame_path_if_valid(session: VideoAnalysisSession, normalized_ts: str) -> Optional[str]:
    extracted = getattr(session, "extracted_frames", None) or {}
    temp_dir = getattr(session, "temp_dir", None)
    if not temp_dir:
        return None

    candidates = []
    for candidate in (extracted.get(normalized_ts),):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for key, path in extracted.items():
        if normalize_video_timestamp(key) == normalized_ts and path not in candidates:
            candidates.append(path)

    for candidate in candidates:
        try:
            base = os.path.realpath(temp_dir)
            full = os.path.realpath(candidate)
            if full != base and not full.startswith(base + os.sep):
                continue
            if os.path.isfile(full) and os.path.getsize(full) > 0:
                return full
        except OSError:
            continue

    video_path = getattr(session, "video_path", None)
    if video_path and os.path.isfile(video_path):
        frame_path = extract_frame_with_fallback(
            video_path,
            normalized_ts,
            temp_dir,
            crop_participants=False,
        )
        if frame_path:
            try:
                base = os.path.realpath(temp_dir)
                full = os.path.realpath(frame_path)
                if full == base or full.startswith(base + os.sep):
                    extracted[normalized_ts] = full
                    session.extracted_frames = extracted
                    register_session_frame_s3(session, normalized_ts, full)
                    return full
            except OSError:
                pass
    return None


def _download_video_to_dir(video_s3_key: str, output_dir: str) -> Optional[str]:
    if not video_s3_key:
        return None
    suffix = os.path.splitext(video_s3_key)[1] or ".mp4"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=output_dir)
    tmp_path = tmp.name
    tmp.close()
    try:
        s3_client.download_file(S3_BUCKET_NAME, video_s3_key, tmp_path)
        if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
            return tmp_path
    except Exception as exc:
        logger.warning("Failed to download session video: %s", log_safe(exc))
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return None


def _extract_and_upload_frame_from_s3_video(
    owner_user_id: str,
    session_id: str,
    video_s3_key: str,
    normalized_ts: str,
) -> Optional[bytes]:
    temp_dir = tempfile.mkdtemp(prefix="va_frame_")
    try:
        video_path = _download_video_to_dir(video_s3_key, temp_dir)
        if not video_path:
            return None
        frame_path = extract_frame_with_fallback(
            video_path,
            normalized_ts,
            temp_dir,
            crop_participants=False,
        )
        frame_bytes = _read_file_bytes(frame_path or "")
        if not frame_bytes:
            return None

        s3_key = session_frame_s3_key(owner_user_id, session_id, normalized_ts)
        if _upload_bytes_to_s3(s3_key, frame_bytes, "image/jpeg"):
            db = SessionLocal()
            try:
                job = db.query(JobStatus).filter(JobStatus.job_id == session_id).first()
                if job and isinstance(job.result, dict) and job.result.get("job_type") == JOB_TYPE:
                    merged = dict(job.result.get("frame_s3_keys") or {})
                    merged[normalized_ts] = s3_key
                    job.result = {**job.result, "frame_s3_keys": merged}
                    db.commit()
            except Exception as exc:
                logger.warning(
                    "Failed to persist frame S3 key for %s: %s",
                    log_safe(session_id),
                    log_safe(exc),
                )
                db.rollback()
            finally:
                db.close()
        return frame_bytes
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def resolve_session_frame_bytes(
    db: Session,
    session_id: str,
    user_id: str,
    normalized_ts: str,
    *,
    is_super_admin: bool = False,
) -> Optional[bytes]:
    """Resolve a preview frame as bytes from local session cache or shared S3 storage."""
    session = video_analysis_sessions.get(session_id)
    if session:
        owner_id = getattr(session, "user_id", None)
        if owner_id and owner_id != user_id and not is_super_admin:
            return None
        local_path = _local_frame_path_if_valid(session, normalized_ts)
        if local_path:
            register_session_frame_s3(session, normalized_ts, local_path)
            persist_video_analysis_status(session)
            return _read_file_bytes(local_path)

    assets = _load_job_assets(db, session_id, user_id, is_super_admin=is_super_admin)
    if not assets:
        return None

    owner_user_id, frame_s3_keys, video_s3_key = assets
    frame_key = frame_s3_keys.get(normalized_ts)
    if frame_key:
        frame_bytes = _s3_object_bytes(frame_key)
        if frame_bytes:
            return frame_bytes

    if video_s3_key:
        return _extract_and_upload_frame_from_s3_video(
            owner_user_id,
            session_id,
            video_s3_key,
            normalized_ts,
        )
    return None


def ensure_local_video_path(session: VideoAnalysisSession) -> Optional[str]:
    """Download session video from S3 when the processing worker is unavailable."""
    video_path = getattr(session, "video_path", None)
    if video_path and os.path.isfile(video_path):
        return video_path

    video_s3_key = getattr(session, "video_s3_key", None)
    if not video_s3_key:
        return None

    temp_dir = getattr(session, "temp_dir", None)
    if not temp_dir or not os.path.isdir(temp_dir):
        temp_dir = tempfile.mkdtemp(prefix="va_video_")
        session.temp_dir = temp_dir

    path = _download_video_to_dir(video_s3_key, temp_dir)
    if path:
        session.video_path = path
    return path


def get_frame_cache_headers() -> dict:
    return dict(_FRAME_CACHE_HEADERS)
