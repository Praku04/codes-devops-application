"""Video Analysis knowledge-base indexing and scoped RAG search."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional

from utils.timezone_helpers import now_ist

from agno.tools import tool
from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from core.runtime import (
    DEFAULT_RAG_CHUNK_OVERLAP_WORDS,
    DEFAULT_RAG_CHUNK_SIZE_WORDS,
    S3_BUCKET_NAME,
    embedding_model,
    logger,
    pinecone_index,
    s3_client,
)
from models.domain import JobStatus, VideoAnalysisHistory
from services.knowledge_base import (
    analyze_image_content_with_ai,
    delete_vectors_for_source_document,
    filter_images_by_query_relevance,
    filter_non_existent_images,
    is_plausible_s3_image_key,
    load_image_analysis_from_s3,
    save_image_analysis_to_s3,
    split_text_into_chunks,
    validate_and_clean_image_tags,
)
from utils.security_helpers import extract_image_tags, iter_image_tag_spans, log_safe
from utils.video_processor import extract_frame_at_time, normalize_video_timestamp

IMAGE_TS_PATTERN = re.compile(
    r"\[IMAGE:\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]",
    re.IGNORECASE,
)
FIGURE_TIMESTAMP_PATTERN = re.compile(
    r"\*\*Timestamp:\*\*\s*`?(\d{1,2}:\d{2}(?::\d{2})?)`?",
    re.IGNORECASE,
)
# Recovers the timestamp from a stored frame object, so a re-index reuses the frames a previous
# run already uploaded instead of needing the source video a second time.
FRAME_KEY_TIMESTAMP_PATTERN = re.compile(
    r"frame_(\d{1,2}-\d{2}(?:-\d{2})?)\.(?:jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)

from utils.video_analysis.chat_helpers import (
    VA_CHAT_MIN_RELEVANCE_SCORE,
    context_supports_query,
    query_overlaps_report_titles,
    video_analysis_search_is_miss,
)
from utils.video_analysis.status_store import JOB_TYPE as VIDEO_ANALYSIS_JOB_TYPE

# The two job-result fields the analysis worker writes (see status_store._result_payload) and the
# keys load_session_frame_assets returns them under, named once so reader and writer cannot drift.
SESSION_FRAMES_FIELD = "frame_s3_keys"
SESSION_VIDEO_FIELD = "video_s3_key"

VA_CHAT_MAX_IMAGES_PER_QUERY = 3


def video_analysis_source_doc(history_id: str) -> str:
    return f"va_{history_id}"


def _timestamp_to_seconds(timestamp: str) -> int:
    parts = [int(p) for p in timestamp.strip().split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


def _collect_markdown_timestamps(markdown: str) -> List[str]:
    """Gather unique MM:SS timestamps from [IMAGE: ...] and ### Figure blocks."""
    ordered: List[str] = []
    seen: set = set()
    for pattern in (IMAGE_TS_PATTERN, FIGURE_TIMESTAMP_PATTERN):
        for match in pattern.finditer(markdown):
            ts = match.group(1)
            if ts not in seen:
                seen.add(ts)
                ordered.append(ts)
    return ordered


def _normalized_ts(ts: str) -> str:
    """Zero-padded MM:SS, so "7:30" in the markdown and "07:30" on a stored frame agree."""
    return normalize_video_timestamp(ts) or str(ts or "").strip()


def report_frame_s3_key(user_id: str, source_doc: str, ts: str) -> str:
    """The one place a report's frame object name is built, so writers and readers cannot drift."""
    return f"{user_id}/extracted_images/{source_doc}/frame_{_normalized_ts(ts).replace(':', '-')}.jpg"


def _copy_s3_object(source_key: str, dest_key: str) -> bool:
    """Server-side copy, so adopting an existing frame never downloads and re-uploads it."""
    if not source_key or not dest_key:
        return False
    if source_key == dest_key:
        return True
    try:
        s3_client.copy_object(
            Bucket=S3_BUCKET_NAME,
            CopySource={"Bucket": S3_BUCKET_NAME, "Key": source_key},
            Key=dest_key,
        )
        return True
    except Exception as exc:
        logger.warning("[VA KB] Failed to copy stored frame: %s", log_safe(exc))
        return False


def _existing_report_frames(user_id: str, source_doc: str) -> Dict[str, str]:
    """Timestamp -> S3 key for frames an earlier index already stored under this report."""
    found: Dict[str, str] = {}
    for key in _list_frame_s3_keys(user_id, source_doc):
        match = FRAME_KEY_TIMESTAMP_PATTERN.search(key.replace("\\", "/"))
        if match:
            found.setdefault(_normalized_ts(match.group(1).replace("-", ":")), key)
    return found


def load_session_frame_assets(db: Session, history: VideoAnalysisHistory) -> Dict[str, Any]:
    """
    The durable frames and source video the analysis worker recorded for this report's session.

    Processing uploads the compressed video and every figure frame to S3 and stores the keys on the
    job row. That copy outlives the local temp directory - which the export path deletes right after
    the first download - and is reachable from any worker. Without it, indexing has no frame source
    once that temp copy is gone, which is why Reindex reported "no frames were stored".
    """
    empty: Dict[str, Any] = {SESSION_FRAMES_FIELD: {}, SESSION_VIDEO_FIELD: ""}
    session_id = getattr(history, "session_id", None)
    if not session_id:
        return empty
    try:
        job = db.query(JobStatus).filter(JobStatus.job_id == session_id).first()
    except Exception as exc:
        logger.warning("[VA KB] Could not read session frame assets: %s", log_safe(exc))
        return empty

    result = job.result if job is not None and isinstance(job.result, dict) else {}
    if result.get("job_type") != VIDEO_ANALYSIS_JOB_TYPE:
        return empty

    raw_frames = result.get(SESSION_FRAMES_FIELD)
    frames: Dict[str, str] = {}
    if isinstance(raw_frames, dict):
        for ts, key in raw_frames.items():
            if isinstance(ts, str) and isinstance(key, str) and key.strip():
                frames[_normalized_ts(ts)] = key.strip()
    return {
        SESSION_FRAMES_FIELD: frames,
        SESSION_VIDEO_FIELD: str(result.get(SESSION_VIDEO_FIELD) or "").strip(),
    }


def _upload_frame_file_to_s3(
    local_path: str,
    user_id: str,
    source_doc: str,
    ts: str,
    *,
    analyze: bool = True,
) -> Optional[str]:
    s3_key = report_frame_s3_key(user_id, source_doc, ts)
    try:
        with open(local_path, "rb") as frame_file:
            frame_bytes = frame_file.read()
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=frame_bytes,
            ContentType="image/jpeg",
        )
        if analyze:
            analysis = analyze_image_content_with_ai(
                frame_bytes,
                context_text=f"Video frame at {ts}",
                s3_key=s3_key,
            )
            if analysis:
                save_image_analysis_to_s3(user_id, source_doc, analysis)
        return s3_key
    except Exception as exc:
        logger.warning("[VA KB] Failed to upload frame %s: %s", log_safe(ts), log_safe(exc))
        return None


def _local_frame_for_timestamp(frame_paths: dict, raw_ts: str) -> Optional[str]:
    """A caller-supplied local frame, looked up under both the raw and zero-padded timestamp."""
    for candidate_key in (raw_ts, _normalized_ts(raw_ts)):
        candidate = frame_paths.get(candidate_key)
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _embed_video_frames_in_markdown(
    markdown: str,
    video_path: str,
    user_id: str,
    source_doc: str,
    frame_paths: Optional[dict] = None,
    *,
    analyze_frames: bool = True,
    frame_s3_keys: Optional[dict] = None,
    video_path_provider: Optional[Callable[[], Optional[str]]] = None,
) -> tuple:
    """
    Upload frames for all figure/image timestamps to S3 and inject [IMAGE: s3_key] tags.
    Supports ### Figure N / **Timestamp:** blocks (post-processing format) and [IMAGE: MM:SS].

    Frames are resolved from the cheapest source that still has them: a frame this report already
    stored, then a local file from the caller, then the session's durable S3 frame, and only then by
    decoding the source video. `video_path_provider` is called at most once and only if a timestamp
    gets that far, so a reindex that is fully covered by stored frames never downloads the video.

    Returns (markdown, {timestamp: s3_key}, frames_recovered_from_session_storage) so the caller can
    report what was actually stored rather than guessing from the markdown.
    """
    if not markdown:
        return markdown, {}, 0

    timestamps = _collect_markdown_timestamps(markdown)
    if not timestamps:
        logger.info("[VA KB] No timestamps found in markdown for %s", log_safe(source_doc))
        return markdown, {}, 0

    frame_paths = frame_paths or {}
    session_frames = frame_s3_keys or {}
    stored_frames = _existing_report_frames(user_id, source_doc)
    ts_to_s3: dict[str, str] = {}
    adopted = 0
    video_source_resolved = video_path_provider is None

    with tempfile.TemporaryDirectory() as tmp_dir:
        for ts in timestamps:
            if ts in ts_to_s3:
                continue
            normalized = _normalized_ts(ts)

            s3_key = stored_frames.get(normalized)
            if s3_key:
                ts_to_s3[ts] = s3_key
                continue

            local_path = _local_frame_for_timestamp(frame_paths, ts)
            if not local_path:
                session_key = session_frames.get(normalized)
                if session_key:
                    dest_key = report_frame_s3_key(user_id, source_doc, ts)
                    if _copy_s3_object(session_key, dest_key):
                        ts_to_s3[ts] = dest_key
                        stored_frames[normalized] = dest_key
                        adopted += 1
                        continue

            if not local_path:
                if not video_source_resolved:
                    video_source_resolved = True
                    video_path = video_path_provider() or ""
                if video_path and os.path.isfile(video_path):
                    local_path = extract_frame_at_time(
                        video_path,
                        ts,
                        tmp_dir,
                        crop_participants=False,
                    )

            if not local_path or not os.path.isfile(local_path):
                logger.warning("[VA KB] No frame source for timestamp %s", log_safe(ts))
                continue
            s3_key = _upload_frame_file_to_s3(
                local_path, user_id, source_doc, ts, analyze=analyze_frames
            )
            if s3_key:
                ts_to_s3[ts] = s3_key
                stored_frames[normalized] = s3_key

    if adopted:
        logger.info(
            "[VA KB] Recovered %s frame(s) from durable session storage for %s",
            adopted,
            log_safe(source_doc),
        )

    if not ts_to_s3:
        logger.warning(
            "[VA KB] Could not embed any of %s timestamp(s) for %s",
            len(timestamps),
            log_safe(source_doc),
        )
        return markdown, {}, adopted

    def image_tag_repl(match: re.Match) -> str:
        ts = match.group(1)
        if ts in ts_to_s3:
            return f"[IMAGE: {ts_to_s3[ts]}]"
        return match.group(0)

    markdown = IMAGE_TS_PATTERN.sub(image_tag_repl, markdown)

    def inject_after_figure_timestamp(match: re.Match) -> str:
        ts = match.group(1)
        if ts not in ts_to_s3:
            return match.group(0)
        tag = f"[IMAGE: {ts_to_s3[ts]}]"
        tail = markdown[match.end() : match.end() + 160]
        if tag in tail:
            return match.group(0)
        return f"{match.group(0)}\n\n{tag}"

    markdown = FIGURE_TIMESTAMP_PATTERN.sub(inject_after_figure_timestamp, markdown)
    logger.info(
        "[VA KB] Embedded %s/%s frame(s) for %s",
        len(ts_to_s3),
        len(timestamps),
        log_safe(source_doc),
    )
    return markdown, ts_to_s3, adopted


def _list_frame_s3_keys(user_id: str, source_doc: str) -> List[str]:
    prefix = f"{user_id}/extracted_images/{source_doc}/"
    keys: List[str] = []
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    keys.append(key)
    except Exception as exc:
        logger.warning("[VA KB] Failed to list frames for %s: %s", log_safe(source_doc), log_safe(exc))
    return keys


def _sync_frame_cache_from_s3(
    user_id: str, source_doc: str, *, max_frames: int = 12
) -> int:
    """Backfill vision-analysis cache only for frames missing analysis metadata."""
    cached = load_image_analysis_from_s3(user_id, source_doc)
    synced = 0
    for s3_key in _list_frame_s3_keys(user_id, source_doc)[:max_frames]:
        if s3_key in cached:
            continue
        try:
            resp = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
            frame_bytes = resp["Body"].read()
            analysis = analyze_image_content_with_ai(
                frame_bytes,
                context_text=f"Video analysis frame {s3_key.split('/')[-1]}",
                s3_key=s3_key,
            )
            if analysis:
                save_image_analysis_to_s3(user_id, source_doc, analysis)
                cached[s3_key] = analysis
                synced += 1
        except Exception as exc:
            logger.warning("[VA KB] Cache sync failed for %s: %s", log_safe(s3_key), log_safe(exc))
    if synced:
        logger.info("[VA KB] Synced %s frame analysis cache entries for %s", synced, log_safe(source_doc))
    return synced


def save_history_video_to_s3(
    user_id: str, history_id: str, local_video_path: str
) -> Optional[str]:
    """Persist source video so Reindex can extract frames later."""
    if not local_video_path or not os.path.isfile(local_video_path):
        return None
    ext = os.path.splitext(local_video_path)[1] or ".mp4"
    key = f"video-analysis/videos/{user_id}/{history_id}{ext}"
    try:
        with open(local_video_path, "rb") as video_file:
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=key,
                Body=video_file,
                ContentType="video/mp4",
            )
        logger.info("[VA KB] Saved source video to S3: %s", log_safe(key))
        return key
    except Exception as exc:
        logger.warning("[VA KB] Failed to save source video: %s", log_safe(exc))
        return None


def adopt_session_video_for_history(
    user_id: str, history_id: str, session_video_key: str
) -> Optional[str]:
    """
    Give a report its own copy of the session's stored video.

    Copied rather than referenced: deleting a report deletes its video_s3_key, and two reports can
    be exported from one session, so sharing the session object would let one deletion blind the
    other report's Reindex.
    """
    if not session_video_key:
        return None
    ext = os.path.splitext(session_video_key)[1] or ".mp4"
    dest_key = f"video-analysis/videos/{user_id}/{history_id}{ext}"
    if not _copy_s3_object(session_video_key, dest_key):
        return None
    logger.info("[VA KB] Adopted session video for report %s", log_safe(history_id))
    return dest_key


def _download_s3_video_temp(key: str) -> Optional[str]:
    """Download an S3 video object to a temp file for frame extraction."""
    if not key:
        return None
    suffix = os.path.splitext(key)[1] or ".mp4"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()
    try:
        s3_client.download_file(S3_BUCKET_NAME, key, tmp_path)
        return tmp_path
    except Exception as exc:
        logger.warning("[VA KB] Could not download source video %s: %s", log_safe(key), log_safe(exc))
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None


def download_history_video_temp(history: VideoAnalysisHistory) -> Optional[str]:
    """Download the report's own stored source video to a temp file."""
    return _download_s3_video_temp(getattr(history, "video_s3_key", None) or "")


def _resolve_mm_ss_tags_in_content(
    text: str, user_id: str, source_doc: str
) -> str:
    """Convert [IMAGE: MM:SS] in chunk text to S3 frame keys when frames exist."""

    def repl(match: re.Match) -> str:
        s3_key = report_frame_s3_key(user_id, source_doc, match.group(1))
        try:
            s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
            return f"[IMAGE: {s3_key}]"
        except ClientError:
            return ""

    return IMAGE_TS_PATTERN.sub(repl, text)


def _doc_image_keys(source_doc: str, cached_analyses: dict) -> List[str]:
    return [
        k
        for k in cached_analyses
        if source_doc in k.replace("\\", "/")
    ]


def _save_report_to_generated_reports(
    user_id: str,
    history: VideoAnalysisHistory,
    markdown: str,
    source_doc: str,
) -> None:
    report_id = history.id
    md_key = f"{user_id}/generated_reports/{report_id}.md"
    json_key = f"{user_id}/generated_reports/{report_id}.json"
    meta = {
        "source_doc": source_doc,
        "title": history.pdf_title or "Video Analysis Report",
        "doc_type": "video_analysis",
        "history_id": history.id,
        "video_filename": history.video_filename,
    }
    s3_client.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=md_key,
        Body=markdown.encode("utf-8"),
        ContentType="text/markdown",
    )
    s3_client.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=json_key,
        Body=json.dumps(meta).encode("utf-8"),
        ContentType="application/json",
    )


def _delete_generated_report_artifacts(user_id: str, history_id: str) -> None:
    for suffix in (".md", ".json"):
        key = f"{user_id}/generated_reports/{history_id}{suffix}"
        try:
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=key)
        except Exception:
            pass


def _delete_s3_prefix(prefix: str) -> int:
    """Delete all objects under an S3 prefix. Returns count deleted."""
    deleted = 0
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
            keys = [obj["Key"] for obj in page.get("Contents", []) if obj.get("Key")]
            if not keys:
                continue
            for i in range(0, len(keys), 1000):
                batch = keys[i : i + 1000]
                s3_client.delete_objects(
                    Bucket=S3_BUCKET_NAME,
                    Delete={"Objects": [{"Key": k} for k in batch]},
                )
                deleted += len(batch)
    except Exception as exc:
        logger.warning(
            "[VA KB] Failed to delete S3 prefix %s: %s",
            log_safe(prefix),
            log_safe(exc),
        )
    return deleted


def delete_video_analysis_report_assets(
    user_id: str,
    history: VideoAnalysisHistory,
) -> None:
    """Remove Pinecone vectors, generated report files, frames, analysis cache, and source video."""
    source_doc = history.source_doc_name or video_analysis_source_doc(history.id)
    delete_video_analysis_vectors(user_id, history)

    frames_deleted = _delete_s3_prefix(f"{user_id}/extracted_images/{source_doc}/")
    cache_deleted = _delete_s3_prefix(f"{user_id}/image_analysis/{source_doc}/")

    video_key = getattr(history, "video_s3_key", None)
    if video_key:
        try:
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=video_key)
            logger.info("[VA KB] Deleted source video: %s", log_safe(video_key))
        except Exception as exc:
            logger.warning("[VA KB] Failed to delete source video %s: %s", log_safe(video_key), log_safe(exc))

    if frames_deleted or cache_deleted:
        logger.info(
            "[VA KB] Deleted %s frame(s) and %s analysis file(s) for %s",
            frames_deleted,
            cache_deleted,
            log_safe(source_doc),
        )


def index_video_analysis_report(
    db: Session,
    history: VideoAnalysisHistory,
    video_path: Optional[str] = None,
    frame_paths: Optional[dict] = None,
) -> dict:
    """
    Index a video analysis report into the owner's Pinecone namespace.
    Each trainer/admin uses namespace=user_id (same as My Modules).
    """
    if not history.markdown_content:
        return {"success": False, "error": "No markdown content to index"}

    user_id = history.user_id
    source_doc = history.source_doc_name or video_analysis_source_doc(history.id)
    markdown = history.markdown_content
    requested_ts = _collect_markdown_timestamps(markdown)
    temp_video_path: Optional[str] = None

    try:
        history.embedding_status = "indexing"
        db.commit()

        session_assets = load_session_frame_assets(db, history)

        def resolve_video_path() -> Optional[str]:
            """Materialise a source video only if a timestamp has no stored frame to reuse."""
            nonlocal temp_video_path
            video_key = getattr(history, SESSION_VIDEO_FIELD, None) or ""
            if not video_key and session_assets[SESSION_VIDEO_FIELD]:
                video_key = adopt_session_video_for_history(
                    user_id, history.id, session_assets[SESSION_VIDEO_FIELD]
                ) or ""
                if video_key:
                    history.video_s3_key = video_key
                    db.commit()
            temp_video_path = _download_s3_video_temp(video_key)
            return temp_video_path

        # Fast index: upload frames only; vision analysis runs on first chat search if needed.
        markdown, frame_map, frames_recovered = _embed_video_frames_in_markdown(
            markdown,
            video_path or "",
            user_id,
            source_doc,
            frame_paths=frame_paths,
            analyze_frames=False,
            frame_s3_keys=session_assets[SESSION_FRAMES_FIELD],
            video_path_provider=None if video_path else resolve_video_path,
        )
        _save_report_to_generated_reports(user_id, history, markdown, source_doc)

        # Prefix delete, not filter delete: filter deletes are unsupported on a serverless
        # index, so a re-index that produces fewer chunks left the surplus vectors behind.
        delete_vectors_for_source_document(user_id, source_doc)

        chunks = split_text_into_chunks(
            markdown,
            DEFAULT_RAG_CHUNK_SIZE_WORDS,
            DEFAULT_RAG_CHUNK_OVERLAP_WORDS,
        )
        if not chunks:
            history.embedding_status = "failed"
            db.commit()
            return {"success": False, "error": "No chunks generated from report"}

        vectors = embedding_model.embed_documents(chunks)
        report_title = history.pdf_title or "Video Analysis Report"
        vectors_to_upsert = []
        for i, chunk in enumerate(chunks):
            vectors_to_upsert.append(
                {
                    "id": f"{source_doc}-{i}",
                    "values": vectors[i],
                    "metadata": {
                        "source": source_doc,
                        "text": chunk,
                        "doc_type": "video_analysis",
                        "history_id": history.id,
                        "report_title": report_title,
                    },
                }
            )

        pinecone_index.upsert(vectors=vectors_to_upsert, namespace=user_id)

        history.source_doc_name = source_doc
        history.embedding_status = "indexed"
        history.embedded_at = now_ist()
        history.chunk_count = len(chunks)
        history.markdown_content = markdown
        db.commit()

        embedded_frames = len(_list_frame_s3_keys(user_id, source_doc))
        # Counted against the timestamps the report asked for, not against the indexed markdown:
        # the "**Timestamp:** MM:SS" line stays in place even once its [IMAGE: ...] tag is injected,
        # so re-scanning the output made a fully successful index look like a total failure.
        missing_ts = [ts for ts in requested_ts if ts not in frame_map]
        logger.info(
            "[VA KB] Indexed report %s for user %s (%s chunks, %s S3 frames)",
            log_safe(history.id),
            log_safe(user_id),
            len(chunks),
            embedded_frames,
        )
        result: dict = {
            "success": True,
            "chunk_count": len(chunks),
            "source_doc_name": source_doc,
            "frame_cache_synced": frames_recovered,
            "frames_embedded": embedded_frames,
        }
        if missing_ts and not frame_map:
            result["warning"] = (
                f"Indexed the text, but none of the {len(missing_ts)} figure image(s) could be "
                "recovered - the source video for this report is no longer in storage, so chat "
                "answers will be text only."
            )
        elif missing_ts:
            result["warning"] = (
                f"Indexed with {len(frame_map)} figure image(s); {len(missing_ts)} timestamp(s) had "
                "no frame available and will appear as text only in chat."
            )
        return result
    except Exception as exc:
        logger.error(
            "[VA KB] Index failed for %s: %s",
            log_safe(history.id),
            log_safe(exc),
            exc_info=True,
        )
        history.embedding_status = "failed"
        db.commit()
        return {"success": False, "error": "Indexing failed"}
    finally:
        if temp_video_path and os.path.isfile(temp_video_path):
            try:
                os.remove(temp_video_path)
            except OSError:
                pass


def delete_video_analysis_vectors(
    user_id: str,
    history: VideoAnalysisHistory,
) -> None:
    source_doc = history.source_doc_name or video_analysis_source_doc(history.id)
    delete_vectors_for_source_document(user_id, source_doc)
    _delete_generated_report_artifacts(user_id, history.id)


def build_video_analysis_search_query(
    user_query: str,
    history: Optional[List[Dict[str, str]]] = None,
    report_titles: Optional[List[str]] = None,
) -> str:
    """Build a richer search string for Pinecone (handles short follow-up questions)."""
    q = (user_query or "").strip()
    if not q:
        return q

    history = history or []
    prev_user_msgs = [
        str(m.get("content", "")).strip()
        for m in history
        if m.get("role") == "user" and m.get("content")
    ]

    q_lower = q.lower()
    word_count = len(q.split())
    explicit_followup = any(
        phrase in q_lower
        for phrase in (
            "i am asking",
            "i'm asking",
            "i meant",
            "about that",
            "clarify",
            "specifically",
            "what about",
            "tell me about",
            "tell me more",
            "more detail",
            "not that",
            "referring to",
            "in short",
            "same report",
            "that one",
            "this one",
        )
    )
    # Same chat session: short messages are usually follow-ups — merge prior user question.
    is_short_followup = prev_user_msgs and (
        explicit_followup or word_count <= 10
    )
    if is_short_followup:
        prev = prev_user_msgs[-1]
        if prev.lower() not in q_lower:
            q = f"{prev} — {q}"

    if report_titles:
        titles = [t.strip() for t in report_titles if t and str(t).strip()]
        if titles:
            title_blob = " | ".join(titles[:8])
            # Pull product/topic tokens from titles (HexaMedIQ, medical coding, etc.)
            title_terms = set()
            for title in titles:
                for part in re.split(r"[^\w]+", title):
                    if len(part) >= 4:
                        title_terms.add(part)
            overlap = [t for t in title_terms if t.lower() in q_lower]
            if len(q.split()) < 8 or not overlap:
                q = f"{q} — indexed reports: {title_blob}"

    return q[:4000]


def prefetch_video_analysis_search_context(
    user_query: str,
    namespace_user_id: str,
    history_ids: Optional[List[str]] = None,
    *,
    history: Optional[List[Dict[str, str]]] = None,
    report_titles: Optional[List[str]] = None,
    top_k: int = 10,
) -> str:
    """
    Always run semantic search before the chat agent answers.
    Retries with report titles when the first pass finds nothing.
    """
    search_q = build_video_analysis_search_query(
        user_query, history=history, report_titles=report_titles
    )
    ctx = search_video_analysis_kb(
        search_q, namespace_user_id, history_ids, top_k=top_k
    )
    if not video_analysis_search_is_miss(ctx):
        return ctx

    if report_titles and query_overlaps_report_titles(user_query, report_titles):
        fallback_q = " ".join(t for t in report_titles[:6] if t) + " " + user_query
        ctx = search_video_analysis_kb(
            fallback_q, namespace_user_id, history_ids, top_k=top_k
        )
    return ctx


def search_video_analysis_kb(
    query: str,
    namespace_user_id: str,
    history_ids: Optional[List[str]] = None,
    top_k: int = 10,
) -> str:
    """Scoped semantic search over video-analysis vectors in a user's namespace."""
    query_vector = embedding_model.embed_query(query)
    pinecone_filter: dict = {"doc_type": {"$eq": "video_analysis"}}
    if history_ids:
        pinecone_filter = {
            "$and": [
                {"doc_type": {"$eq": "video_analysis"}},
                {"history_id": {"$in": history_ids}},
            ]
        }

    try:
        search_response = pinecone_index.query(
            namespace=namespace_user_id,
            vector=query_vector,
            top_k=top_k,
            include_metadata=True,
            filter=pinecone_filter,
        )
    except Exception as exc:
        logger.error(
            "[VA KB] Search failed: %s",
            log_safe(exc),
            exc_info=True,
        )
        video_analysis_tool_context.last_search_sources = []
        return "No relevant information found in video analysis reports."

    matches = search_response.get("matches") or []
    if not matches:
        video_analysis_tool_context.last_search_sources = []
        return "No relevant information found in the selected video analysis reports."

    scored = [
        m for m in matches
        if (m.get("score") or 0) >= VA_CHAT_MIN_RELEVANCE_SCORE
    ]
    if not scored:
        video_analysis_tool_context.last_search_sources = []
        return "No relevant information found in video analysis reports."
    matches = scored

    doc_names = {
        m["metadata"].get("source", "")
        for m in matches
        if m.get("metadata", {}).get("source")
    }
    cached_analyses = {}
    frames_by_source: dict[str, List[str]] = {}
    for doc_name in doc_names:
        frames_by_source[doc_name] = _list_frame_s3_keys(namespace_user_id, doc_name)
        cached = load_image_analysis_from_s3(namespace_user_id, doc_name)
        cached_analyses.update(cached)

    context_parts = []
    images_added_for_source = set()
    video_analysis_tool_context.last_search_sources = []
    for match in matches:
        meta = match.get("metadata") or {}
        source = meta.get("source", "Unknown")
        # source is "va_<history id>" or a randomized storage name - never shown to the user.
        report_title = meta.get("report_title") or "Video Analysis Report"
        text_content = meta.get("text", "")
        text_content = _resolve_mm_ss_tags_in_content(
            text_content, namespace_user_id, source
        )

        if source not in images_added_for_source:
            frame_keys = frames_by_source.get(source) or _list_frame_s3_keys(
                namespace_user_id, source
            )
            chunk_images = extract_image_tags(text_content)
            if frame_keys and cached_analyses:
                candidates = list(dict.fromkeys(chunk_images + frame_keys))
                if candidates:
                    enriched = (
                        f"{text_content}\n\n"
                        + "\n\n".join(f"[IMAGE: {k}]" for k in candidates)
                    )
                    text_content = filter_images_by_query_relevance(
                        enriched,
                        query,
                        cached_analyses,
                        namespace_user_id,
                        max_images_override=VA_CHAT_MAX_IMAGES_PER_QUERY,
                    )
            elif frame_keys:
                candidates = list(dict.fromkeys(chunk_images + frame_keys))
                limited = candidates[:VA_CHAT_MAX_IMAGES_PER_QUERY]
                if limited:
                    appendix = "\n\n".join(f"[IMAGE: {k}]" for k in limited)
                    if appendix not in text_content:
                        text_content = (
                            f"{text_content}\n\n{appendix}" if text_content else appendix
                        )
            images_added_for_source.add(source)

        if cached_analyses and "[IMAGE:" in text_content:
            text_content = filter_images_by_query_relevance(
                text_content,
                query,
                cached_analyses,
                namespace_user_id,
                max_images_override=VA_CHAT_MAX_IMAGES_PER_QUERY,
            )
        text_content = validate_and_clean_image_tags(
            text_content, namespace_user_id
        )
        context_parts.append(
            f"Source: {report_title}\nContent: {text_content}"
        )
        video_analysis_tool_context.last_search_sources.append(
            {
                "content": (text_content or "")[:800],
                "source": report_title,
            }
        )

    context = "\n\n---\n\n".join(context_parts)
    context = filter_non_existent_images(context)

    valid_keys = list(
        dict.fromkeys(extract_image_tags(context))
    )[:VA_CHAT_MAX_IMAGES_PER_QUERY]
    video_analysis_tool_context.valid_image_keys = valid_keys
    if valid_keys:
        catalog = "\n".join(f"[IMAGE: {k}]" for k in valid_keys)
        context = (
            f"{context}\n\n--- AVAILABLE IMAGES (copy these exact tags only; "
            f"never invent filenames) ---\n{catalog}"
        )
        logger.info(
            "[VA KB] Search context includes %s valid image tag(s) for query %s",
            len(valid_keys),
            log_safe(query[:60] if query else ""),
        )
    else:
        video_analysis_tool_context.valid_image_keys = []
        logger.info(
            "[VA KB] No valid S3 images in search context for %s",
            log_safe(query[:60] if query else ""),
        )

    if not context_supports_query(query, context):
        video_analysis_tool_context.last_search_sources = []
        video_analysis_tool_context.valid_image_keys = []
        return "No relevant information found in video analysis reports."

    return f"Found the following information from video analysis reports:\n{context}"


def ensure_visual_images_in_response(content: str, max_images: int = 3) -> str:
    """Append real S3 frame tags when the model answered without images."""
    from utils.video_analysis.chat_helpers import is_video_analysis_no_match_reply

    if not content or is_video_analysis_no_match_reply(content):
        return content or ""
    if "[IMAGE:" in content:
        return content
    keys = list(video_analysis_tool_context.valid_image_keys or [])
    if not keys:
        return content
    appendix = "\n\n### Related screenshots\n\n" + "\n\n".join(
        f"[IMAGE: {key}]" for key in keys[:max_images]
    )
    return content.rstrip() + appendix


def _strip_user_id_markdown_images(text: str) -> str:
    """Remove ![alt](...user_id/...) markdown images with a linear scan (no ReDoS)."""
    if not text or "user_id/" not in text.lower():
        return text
    out: List[str] = []
    i = 0
    while True:
        start = text.find("![", i)
        if start < 0:
            out.append(text[i:])
            break
        alt_end = text.find("](", start + 2)
        if alt_end < 0:
            out.append(text[i:])
            break
        url_start = alt_end + 2
        url_end = text.find(")", url_start)
        if url_end < 0:
            out.append(text[i:])
            break
        url = text[url_start:url_end]
        out.append(text[i:start])
        if "user_id/" not in url.lower():
            out.append(text[start : url_end + 1])
        i = url_end + 1
    return "".join(out)


def _strip_user_id_image_url_tags(text: str) -> str:
    """Remove [IMAGE_URL: ...user_id/...] tags with a linear scan (no ReDoS)."""
    if not text or "[IMAGE_URL:" not in text:
        return text
    prefix = "[IMAGE_URL:"
    out: List[str] = []
    i = 0
    plen = len(prefix)
    while True:
        start = text.find(prefix, i)
        if start < 0:
            out.append(text[i:])
            break
        j = start + plen
        while j < len(text) and text[j].isspace():
            j += 1
        end = text.find("]", j)
        if end < 0:
            out.append(text[i:])
            break
        inner = text[j:end]
        out.append(text[i:start])
        if "user_id/" not in inner.lower():
            out.append(text[start : end + 1])
        i = end + 1
    return "".join(out)


def sanitize_video_analysis_response(content: str, namespace_user_id: str) -> str:
    """Remove invented image paths and keep only real S3 keys for video analysis chat."""
    del namespace_user_id  # API-compatible; former rewrite path was unreachable after user_id strip
    if not content:
        return content
    text = _strip_user_id_markdown_images(content)
    text = _strip_user_id_image_url_tags(text)

    if "[IMAGE:" in text:
        # Linear pass (no ReDoS): drop user_id/ and implausible tags; keep real keys.
        pieces: List[str] = []
        last = 0
        for start, end, key in iter_image_tag_spans(text):
            pieces.append(text[last:start])
            key_stripped = (key or "").strip()
            if key_stripped.lower().startswith("user_id/"):
                # Former regex removed these tags entirely.
                pass
            elif is_plausible_s3_image_key(key_stripped):
                pieces.append(text[start:end])
            last = end
        pieces.append(text[last:])
        text = "".join(pieces)

    text = filter_non_existent_images(text)
    return _cap_va_chat_image_tags(text, VA_CHAT_MAX_IMAGES_PER_QUERY)


def _cap_va_chat_image_tags(content: str, max_images: int) -> str:
    """Keep only the first N [IMAGE: ...] tags in a chat response."""
    if not content or "[IMAGE:" not in content:
        return content
    seen: List[str] = []
    for _start, _end, key in iter_image_tag_spans(content):
        if key not in seen:
            seen.append(key)
    if len(seen) <= max_images:
        return content
    keep = set(seen[:max_images])
    pieces: List[str] = []
    last = 0
    for start, end, key in iter_image_tag_spans(content):
        pieces.append(content[last:start])
        if key in keep:
            pieces.append(content[start:end])
        last = end
    pieces.append(content[last:])
    return "".join(pieces)


class VideoAnalysisToolContext:
    """The fields a video-analysis tool may read about the request it is serving.

    Kept as the declaration of those fields and their defaults; the values the tools read are
    per-request and live in the ContextVar below. See the note in services/knowledge_base.py for
    why this is not one shared object: the namespace these tools search comes from here, so a
    second chat overwriting it mid-stream sends one learner another learner's reports.
    """

    def __init__(self):
        self.user_id = ""
        self.namespace_user_id = ""
        self.history_ids = None
        self.db = None
        self.valid_image_keys: List[str] = []
        self.last_search_sources: List[dict] = []


_VIDEO_TOOL_CONTEXT_FIELDS = frozenset(vars(VideoAnalysisToolContext()))
_video_tool_context_state: ContextVar[Dict[str, Any]] = ContextVar("video_analysis_tool_state")


def _new_video_tool_state() -> Dict[str, Any]:
    # Built from the class so the defaults live in one place, and so each request gets its own
    # valid_image_keys / last_search_sources lists instead of appending to a shared one.
    return dict(vars(VideoAnalysisToolContext()))


def _video_tool_state() -> Dict[str, Any]:
    """This request's tool state, created on demand when there is no request."""
    try:
        return _video_tool_context_state.get()
    except LookupError:
        state = _new_video_tool_state()
        _video_tool_context_state.set(state)
        return state


class _RequestScopedVideoToolContext:
    """An attribute view over the calling request's video-analysis tool state."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return _video_tool_state()[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in _VIDEO_TOOL_CONTEXT_FIELDS:
            raise AttributeError(name)
        _video_tool_state()[name] = value


video_analysis_tool_context = _RequestScopedVideoToolContext()


def begin_video_analysis_tool_context(
    user_id: str,
    namespace_user_id: str,
    history_ids,
) -> None:
    """Start a fresh tool context for one video-analysis chat request.

    ``db`` stays None: tools open their own short-lived sessions, so the request connection is
    never pinned here. Call this from the request's own task, before streaming starts.
    """
    state = _new_video_tool_state()
    state["user_id"] = user_id
    state["namespace_user_id"] = namespace_user_id
    state["history_ids"] = history_ids
    _video_tool_context_state.set(state)


@tool
def search_video_analysis_reports(query: str) -> str:
    """Search indexed video analysis reports (supplements pre-loaded RETRIEVED CONTEXT in the prompt)."""
    ns = (
        video_analysis_tool_context.namespace_user_id
        or video_analysis_tool_context.user_id
    )
    return search_video_analysis_kb(
        query,
        ns,
        video_analysis_tool_context.history_ids,
    )


def history_embedding_payload(history: VideoAnalysisHistory) -> dict:
    status = getattr(history, "embedding_status", None) or "pending"
    return {
        "source_doc_name": getattr(history, "source_doc_name", None),
        "embedding_status": status,
        "has_embeddings": status == "indexed",
        "embedded_at": (
            history.embedded_at.isoformat()
            if getattr(history, "embedded_at", None)
            else None
        ),
        "chunk_count": getattr(history, "chunk_count", None),
    }
