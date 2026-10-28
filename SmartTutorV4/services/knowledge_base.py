from __future__ import annotations

import os.path
from contextvars import ContextVar

from agno.tools import tool

from core.runtime import *
from models.domain import (
    ActivityLog,
    ChatMessage,
    ChatSession,
    Course,
    CourseProgress,
    Notification,
    SharedReport,
    SharedReportEdit,
    TraineeAttempt,
    TutorResponseCache,
)
from utils.security_helpers import (
    count_image_tags,
    extract_image_tags,
    iter_image_tag_spans,
    log_safe,
    strip_invalid_image_url_tags,
    strip_non_api_markdown_images,
)
from utils.aws_helper import invoke_nova_pro_vision
# Imported explicitly rather than picked up from `core.runtime`'s star export, which is how
# the `nltk` name used to reach this module. See utils/text_tokenize.py for why NLTK went.
from utils.text_tokenize import count_words, sent_tokenize
from services.jobs import JobCancelledError, raise_if_job_cancelled
# Report metadata is read through the per-request S3 index so a module list costs one listing
# and one concurrent fetch pass instead of a blocking GET per module. See services/module_index.py.
from services.module_index import author_report_last_modified, author_report_metadata


def get_user_vector_store(user_id: str) -> Pinecone:
    return Pinecone(pinecone_index, embedding_model, "text", namespace=user_id)


# --- Pinecone vector deletion (serverless-safe) -------------------------------
# A SERVERLESS index does not support delete-by-metadata-filter:
# `pinecone_index.delete(filter={"source": doc})` raises, so every call that relied
# on it silently left the document's embeddings behind - which is why deleting a
# course removed everything except its vectors. The old fallback could not enumerate
# them either: it queried with an all-zero vector (invalid for a cosine index) and
# `top_k=10000` (rejected once metadata is included).
#
# What DOES work on serverless is listing vector IDs by prefix. Every vector this
# application writes is keyed `f"{source_doc_name}-{i}"` (see _reindex_source_document
# below and services/video_analysis_kb.index_video_analysis_report), and nothing else
# writes vectors - get_user_vector_store() is query-only - so the prefix enumerates the
# document's vectors completely.
_PINECONE_ID_PAGE_SIZE = 100  # list_paginated caps a page at 100 IDs
_PINECONE_DELETE_BATCH_SIZE = 100  # delete-by-IDs caps a request at 1000; 100 stays well under
_PINECONE_MAX_IDS_PER_DOCUMENT = 200_000  # runaway-pagination guard, far above any real document


def _vector_id_prefix(doc_name: str) -> str:
    return f"{doc_name}-"


def list_vector_ids_for_source_document(user_id: str, doc_name: str) -> List[str]:
    """Every Pinecone vector ID belonging to one source document in one namespace."""
    if not doc_name:
        return []
    prefix = _vector_id_prefix(doc_name)
    # Only accept the exact `<doc_name>-<chunk index>` shape, so a document whose name
    # happens to start with another document's name can never be deleted by accident.
    id_pattern = re.compile(rf"^{re.escape(prefix)}\d+$")
    ids: List[str] = []
    pagination_token = None
    while True:
        page = pinecone_index.list_paginated(
            prefix=prefix,
            limit=_PINECONE_ID_PAGE_SIZE,
            pagination_token=pagination_token,
            namespace=user_id,
        )
        for vector in getattr(page, "vectors", None) or []:
            vector_id = getattr(vector, "id", None)
            if vector_id is None and isinstance(vector, dict):
                vector_id = vector.get("id")
            if vector_id and id_pattern.match(str(vector_id)):
                ids.append(str(vector_id))
        pagination = getattr(page, "pagination", None)
        pagination_token = getattr(pagination, "next", None) if pagination else None
        if not pagination_token or len(ids) >= _PINECONE_MAX_IDS_PER_DOCUMENT:
            break
    return ids


def delete_vectors_for_source_document(user_id: str, doc_name: str) -> int:
    """Delete a source document's embeddings by ID prefix. Returns the number deleted.

    Raises nothing: a missing namespace or a Pinecone outage must not abort the rest of
    the delete, exactly as the previous implementation behaved.
    """
    try:
        vector_ids = list_vector_ids_for_source_document(user_id, doc_name)
    except NotFoundException:
        logger.info(
            "Namespace %s not found while listing vectors for %s - nothing to delete.",
            log_safe(user_id),
            log_safe(doc_name),
        )
        return 0
    except Exception as e:
        logger.error(
            "Failed to list Pinecone vector IDs for %s in namespace %s: %s",
            log_safe(doc_name),
            log_safe(user_id),
            log_safe(e),
            exc_info=True,
        )
        return 0

    if not vector_ids:
        logger.info(
            "No Pinecone vectors found for %s in namespace %s - already clean.",
            log_safe(doc_name),
            log_safe(user_id),
        )
        return 0

    deleted = 0
    for start in range(0, len(vector_ids), _PINECONE_DELETE_BATCH_SIZE):
        batch = vector_ids[start : start + _PINECONE_DELETE_BATCH_SIZE]
        try:
            pinecone_index.delete(ids=batch, namespace=user_id)
            deleted += len(batch)
        except NotFoundException:
            logger.info(
                "Namespace %s disappeared mid-delete for %s - treating as deleted.",
                log_safe(user_id),
                log_safe(doc_name),
            )
            break
        except Exception as e:
            logger.error(
                "Failed to delete a Pinecone vector batch for %s in namespace %s: %s",
                log_safe(doc_name),
                log_safe(user_id),
                log_safe(e),
                exc_info=True,
            )
    logger.info(
        "Deleted %s Pinecone vector(s) for %s in namespace %s.",
        deleted,
        log_safe(doc_name),
        log_safe(user_id),
    )
    return deleted


def count_remaining_vectors_for_source_document(user_id: str, doc_name: str) -> int:
    """Vectors still present for a document — used to verify a delete really happened."""
    try:
        return len(list_vector_ids_for_source_document(user_id, doc_name))
    except NotFoundException:
        return 0
    except Exception as e:
        logger.warning(
            "Could not verify remaining Pinecone vectors for %s: %s",
            log_safe(doc_name),
            log_safe(e),
        )
        return 0


# Every vector this application writes is keyed "<source doc>-<chunk index>".
_VECTOR_ID_SOURCE = re.compile(r"^(?P<source>.+)-\d+$")


def source_document_from_vector_id(vector_id: str) -> str:
    """The source document a vector ID belongs to, or "" if it is not one of ours."""
    match = _VECTOR_ID_SOURCE.match(str(vector_id or ""))
    return match.group("source") if match else ""


def list_all_vector_ids_in_namespace(user_id: str) -> List[str]:
    """
    Every vector ID in one user's namespace.

    describe_index_stats() is an aggregate that lags well behind a bulk delete on a
    serverless index - which is why the "Indexed Chunks" tile kept reporting embeddings for a
    course that had already been fully deleted. Enumerating IDs reports what is actually in
    the namespace and is the same view the delete path acts on.
    """
    if not user_id:
        return []
    ids: List[str] = []
    pagination_token = None
    while True:
        page = pinecone_index.list_paginated(
            limit=_PINECONE_ID_PAGE_SIZE,
            pagination_token=pagination_token,
            namespace=user_id,
        )
        for vector in getattr(page, "vectors", None) or []:
            vector_id = getattr(vector, "id", None)
            if vector_id is None and isinstance(vector, dict):
                vector_id = vector.get("id")
            if vector_id:
                ids.append(str(vector_id))
        pagination = getattr(page, "pagination", None)
        pagination_token = getattr(pagination, "next", None) if pagination else None
        if not pagination_token or len(ids) >= _PINECONE_MAX_IDS_PER_DOCUMENT:
            break
    return ids


def count_vectors_in_namespace(user_id: str) -> int:
    """Indexed-chunk count for one user, or -1 when Pinecone could not be enumerated."""
    try:
        return len(list_all_vector_ids_in_namespace(user_id))
    except NotFoundException:
        # No namespace at all: the user has never indexed anything, or everything is gone.
        return 0
    except Exception as e:
        logger.warning(
            "Could not enumerate Pinecone vectors for namespace %s: %s",
            log_safe(user_id),
            log_safe(e),
        )
        return -1


def _delete_vector_ids(user_id: str, vector_ids: List[str]) -> int:
    """Delete an explicit list of vector IDs in batches. Never raises."""
    deleted = 0
    for start in range(0, len(vector_ids), _PINECONE_DELETE_BATCH_SIZE):
        batch = vector_ids[start : start + _PINECONE_DELETE_BATCH_SIZE]
        try:
            pinecone_index.delete(ids=batch, namespace=user_id)
            deleted += len(batch)
        except NotFoundException:
            break
        except Exception as e:
            logger.error(
                "Failed to delete a Pinecone vector batch in namespace %s: %s",
                log_safe(user_id),
                log_safe(e),
                exc_info=True,
            )
    return deleted


def _live_source_documents_for_user(user_id: str, db: Session) -> Set[str]:
    """
    Every source document that still has content behind it, in one set.

    Deliberately generous - a document is treated as live if ANY of these still reference it,
    because a false "orphan" would delete embeddings that a course still answers from:
      * a Course row of this user (both the primary name and the multi-document JSON list),
      * a generated module still in S3 (the record of what was actually indexed),
      * a video-analysis report row ("va_<history id>", a separate feature with its own
        delete path - a course delete must never take those out).
    """
    live: Set[str] = set()

    def remember(name) -> None:
        text_name = str(name or "").strip()
        if not text_name:
            return
        live.add(text_name)
        live.add(secure_filename(text_name) or text_name)
        normalized = normalize_document_name(text_name)
        if normalized:
            live.add(normalized)

    try:
        for course in db.query(Course).filter(Course.author_id == user_id).all():
            remember(course.source_doc_name)
            for doc in course_source_document_names(course):
                remember(doc)
    except Exception as exc:
        logger.warning("Could not list courses while finding live documents: %s", log_safe(exc))
        raise

    try:
        from models.domain import VideoAnalysisHistory

        for row in (
            db.query(VideoAnalysisHistory)
            .filter(VideoAnalysisHistory.user_id == user_id)
            .all()
        ):
            remember(row.source_doc_name)
            remember(f"va_{row.id}")
    except Exception as exc:
        logger.warning(
            "Could not list video-analysis reports while finding live documents: %s",
            log_safe(exc),
        )
        raise

    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=S3_BUCKET_NAME, Prefix=f"{user_id}/generated_reports/"
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                try:
                    meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                    meta_data = json.loads(meta_obj["Body"].read().decode("utf-8"))
                except (ClientError, json.JSONDecodeError, KeyError):
                    continue
                remember(meta_data.get("source_doc"))
    except (BotoCoreError, ClientError) as exc:
        logger.warning("Could not list modules while finding live documents: %s", log_safe(exc))
        raise

    return live


def delete_orphan_vectors_for_user(user_id: str, db: Session) -> int:
    """
    Delete embeddings whose source document no longer has a course, a module or a report.

    delete_source_document_completely() only knows the documents the Course row listed, so a
    row written without its source_documents JSON (or a document dropped from that list) left
    its embeddings in the index for good: the course was gone from every screen while
    "Indexed Chunks" still counted its chunks and the chat agent could still answer from them.
    This sweep is the backstop that makes a course delete really empty the knowledge base.

    Returns the number of vectors deleted. Never raises: it runs after the delete has already
    succeeded, and it must not turn a completed delete into an error response.
    """
    try:
        vector_ids = list_all_vector_ids_in_namespace(user_id)
    except NotFoundException:
        return 0
    except Exception as exc:
        logger.warning(
            "[ORPHAN SWEEP] Could not enumerate namespace %s: %s", log_safe(user_id), log_safe(exc)
        )
        return 0
    if not vector_ids:
        return 0

    try:
        live = _live_source_documents_for_user(user_id, db)
    except Exception:
        # An incomplete live-set would delete embeddings that are still in use, so a failure
        # here must abandon the sweep rather than guess.
        logger.error("[ORPHAN SWEEP] Skipped: could not determine which documents are live.")
        return 0

    orphan_ids = []
    orphan_sources = set()
    for vector_id in vector_ids:
        source = source_document_from_vector_id(vector_id)
        if not source:
            # Not "<source>-<n>": not written by this application. Leave it alone.
            continue
        if source in live or (secure_filename(source) or source) in live:
            continue
        if normalize_document_name(source) in live:
            continue
        orphan_ids.append(vector_id)
        orphan_sources.add(source)

    if not orphan_ids:
        logger.info(
            "[ORPHAN SWEEP] Namespace %s is clean (%s vector(s), all still in use).",
            log_safe(user_id),
            len(vector_ids),
        )
        return 0

    deleted = _delete_vector_ids(user_id, orphan_ids)
    logger.info(
        "[ORPHAN SWEEP] Deleted %s orphan vector(s) across %s document(s) in namespace %s.",
        deleted,
        len(orphan_sources),
        log_safe(user_id),
    )
    return deleted


def clear_all_data_for_user(user_id: str, db: Session = None):
    """Clear database, Pinecone, and S3 data for a user."""
    if db:
        try:
            logger.info("Deleting database records for user: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
            db.query(ActivityLog).filter(ActivityLog.user_id == user_id).delete()
            db.query(CourseProgress).filter(CourseProgress.user_id == user_id).delete()
            db.query(TraineeAttempt).filter(TraineeAttempt.trainee_id == user_id).delete()

            user_sessions = db.query(ChatSession).filter(ChatSession.user_id == user_id).all()
            for session in user_sessions:
                db.query(ChatMessage).filter(ChatMessage.session_id == session.id).delete()
            db.query(ChatSession).filter(ChatSession.user_id == user_id).delete()

            db.query(SharedReportEdit).filter(SharedReportEdit.editor_id == user_id).delete()
            sharer_reports = db.query(SharedReport).filter(SharedReport.sharer_id == user_id).all()
            for report in sharer_reports:
                db.query(SharedReportEdit).filter(SharedReportEdit.shared_report_id == report.id).delete()
            db.query(SharedReport).filter(SharedReport.sharer_id == user_id).delete()

            recipient_reports = db.query(SharedReport).filter(SharedReport.recipient_id == user_id).all()
            for report in recipient_reports:
                db.query(SharedReportEdit).filter(SharedReportEdit.shared_report_id == report.id).delete()
            db.query(SharedReport).filter(SharedReport.recipient_id == user_id).delete()

            db.query(Notification).filter(Notification.user_id == user_id).delete()

            user_courses = db.query(Course).filter(Course.author_id == user_id).all()
            for course in user_courses:
                cache_entries = db.query(TutorResponseCache).filter(TutorResponseCache.course_id == course.id).all()
                for cache_entry in cache_entries:
                    try:
                        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
                        logger.info(
                            "[CACHE CLEANUP] Deleted S3 cache: %s",
                            ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")),
                        )
                    except Exception as e:
                        logger.warning(
                            "[CACHE CLEANUP] Failed to delete S3 cache %s: %s",
                            ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")),
                            e,
                        )
                db.query(TutorResponseCache).filter(TutorResponseCache.course_id == course.id).delete()
                logger.info("[CACHE CLEANUP] Deleted tutor cache for course: %s", course.id)

            db.commit()
            logger.info("Successfully deleted database records for user: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
        except Exception as e:
            db.rollback()
            logger.error(
                "Failed to delete database records for user %s: %s",
                ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
                e,
                exc_info=True,
            )
            raise IOError("Failed to clear database records.")

    try:
        logger.info("Deleting Pinecone vectors in namespace: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
        pinecone_index.delete(delete_all=True, namespace=user_id)
    except NotFoundException:
        logger.warning(
            "Namespace %s not found in Pinecone during deletion. Nothing to delete.",
            ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
        )
    except Exception as e:
        logger.error("Failed to delete Pinecone data for user %s: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise IOError("Failed to clear vector data.")

    try:
        logger.info("Deleting S3 objects under prefix: %s/", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
        s3 = boto3.resource("s3")
        bucket = s3.Bucket(S3_BUCKET_NAME)
        bucket.objects.filter(Prefix=f"{user_id}/").delete()
    except Exception as e:
        logger.error("Failed to delete S3 data for user %s: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise IOError("Failed to clear file storage data.")


def clean_text(text: str) -> str:
    text = re.sub(r"(\w+)-\s*\n\s*(\w+)", r"\1\2", text)
    text = re.sub(r"\n(?!\n)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def count_images_in_module(content: str) -> int:
    return count_image_tags(content)


def extract_image_paths_from_module(content: str) -> List[str]:
    return extract_image_tags(content)


def get_s3_signed_url(s3_key: str, expiration: int = 3600) -> Optional[str]:
    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key},
            ExpiresIn=expiration,
        )
        logger.info("Generated signed URL for: %s (expires in %ss)", s3_key, expiration)
        return url
    except Exception as e:
        logger.error("Failed to generate signed URL for %s: %s", s3_key, e)
        return None


def get_image_from_s3_as_bytes(s3_key: str) -> Optional[bytes]:
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        image_bytes = response["Body"].read()
        logger.info("Downloaded image bytes from S3: %s (%s bytes)", s3_key, len(image_bytes))
        return image_bytes
    except Exception as e:
        logger.error("Failed to download image bytes from S3 %s: %s", s3_key, e)
        return None


def load_image_analysis_from_s3(user_id: str, doc_name: str) -> dict:
    try:
        logger.info("[CACHE] Loading cached image analysis for %s", doc_name)
        analysis_prefix = f"{user_id}/image_analysis/{secure_filename(doc_name)}/"
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=analysis_prefix)
        cached_analyses = {}
        for page in pages:
            for obj in page.get("Contents", []):
                analysis_key = obj["Key"]
                if analysis_key.endswith("_analysis.json"):
                    try:
                        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=analysis_key)
                        analysis_data = json.loads(response["Body"].read().decode("utf-8"))
                        image_s3_key = analysis_data.get("s3_key", "")
                        if image_s3_key:
                            cached_analyses[image_s3_key] = analysis_data
                            logger.info("[CACHE] Loaded analysis for %s", image_s3_key)
                    except Exception as e:
                        logger.warning("[CACHE] Failed to load analysis from %s: %s", analysis_key, e)
        logger.info("[CACHE] Loaded %s cached image analyses", len(cached_analyses))
        return cached_analyses
    except Exception as e:
        logger.error("[CACHE] Failed to load cached image analyses: %s", e)
        return {}


def save_image_analysis_to_s3(user_id: str, doc_name: str, image_analysis: dict) -> str:
    try:
        image_s3_key = image_analysis.get("s3_key", "")
        if image_s3_key:
            image_filename = image_s3_key.split("/")[-1]
            analysis_filename = f"{image_filename}_analysis.json"
        else:
            analysis_filename = f"image_analysis_{int(time.time())}.json"

        analysis_s3_key = f"{user_id}/image_analysis/{secure_filename(doc_name)}/{analysis_filename}"
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=analysis_s3_key,
            Body=json.dumps(image_analysis, indent=2),
            ContentType="application/json",
        )
        logger.info("Saved image analysis to S3: %s", analysis_s3_key)
        return analysis_s3_key
    except Exception as e:
        logger.error("Failed to save image analysis to S3: %s", e)
        return ""


def normalize_document_name(doc_name: str) -> str:
    if not doc_name:
        return ""
    normalized = doc_name.lower().strip()
    extensions = [
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tiff",
        ".tif",
        ".webp",
        ".txt",
        ".rtf",
        ".odt",
        ".xlsx",
        ".xls",
        ".csv",
    ]
    for ext in extensions:
        if normalized.endswith(ext):
            normalized = normalized[: -len(ext)]
            break
    return normalized


def _is_randomized_storage_name(doc_name: str) -> bool:
    """
    True for a '<32 hex chars>.<ext>' name produced by randomized_storage_name().

    The stem is split off here rather than via normalize_document_name(), which only strips
    extensions it knows: randomized_storage_name() falls back to '.bin' for anything outside
    its allow-list, and such a name must still be recognised as a UUID rather than shown to
    the user.
    """
    if not doc_name:
        return False
    stem = str(doc_name).strip().lower().rsplit(".", 1)[0]
    return len(stem) == 32 and all(c in "0123456789abcdef" for c in stem)


def course_source_document_names(course: Course) -> List[str]:
    """
    Every STORAGE document name a course was generated from, in the order it was recorded.

    Reads the multi-document JSON list and falls back to the single primary name, so a caller
    never has to care which of the two a given course happens to use.
    """
    storage_names: List[str] = []
    if getattr(course, "source_documents", None):
        try:
            raw = json.loads(course.source_documents)
            if isinstance(raw, list):
                storage_names = [str(d) for d in raw if d]
        except (ValueError, TypeError):
            storage_names = []
    primary = getattr(course, "source_doc_name", None)
    if not storage_names and primary:
        storage_names = [primary]
    return storage_names


def course_document_display_map(course: Course) -> Dict[str, str]:
    """
    Map each of a course's STORAGE document names to the filename the user uploaded.

    Storage names are randomized ("<uuid>.<ext>", VAPT #4: Double Extension File Upload),
    so the original name cannot be recovered from the key itself - it is read from the
    positionally aligned Course.source_document_display_names written at generation time.

    A storage name that is not randomized (uploaded before that change) is already the
    user's filename and maps to itself. A randomized name with no recorded display name -
    a course generated before this column existed - is left OUT of the map entirely, so
    friendly_document_name() can substitute a generic label instead of echoing a bare UUID
    back at the user.
    """
    storage_names = course_source_document_names(course)

    display_names: List[str] = []
    if getattr(course, "source_document_display_names", None):
        try:
            raw_display = json.loads(course.source_document_display_names)
            if isinstance(raw_display, list):
                display_names = [str(d) for d in raw_display if d]
        except (ValueError, TypeError):
            display_names = []

    mapping: Dict[str, str] = {}
    for idx, storage_name in enumerate(storage_names):
        if idx < len(display_names) and display_names[idx]:
            mapping[storage_name] = display_names[idx]
        elif not _is_randomized_storage_name(storage_name):
            mapping[storage_name] = storage_name
    return mapping


def friendly_document_name(doc_name: str, display_map: Optional[Dict[str, str]] = None) -> str:
    """
    Best-effort display name for one storage document name.

    Falls back to the storage name, except for a randomized one with no mapping: showing a
    bare 32-char hex string tells the user nothing, so that renders as a generic label.
    """
    if not doc_name:
        return ""
    if display_map:
        mapped = display_map.get(doc_name)
        if mapped:
            return mapped
    if _is_randomized_storage_name(doc_name):
        ext = doc_name.rsplit(".", 1)[-1].upper() if "." in doc_name else "FILE"
        return f"Uploaded {ext}"
    return doc_name


def course_display_label(course: Course) -> str:
    """
    Human-readable name for a course, for notifications and any other user-facing text.

    `course.source_doc_name` is a randomized storage name (VAPT #4), so the widespread
    `course.course_title or course.source_doc_name` fallback rendered a bare UUID whenever a
    course had no title. This resolves the document's uploaded filename instead.
    """
    if course is None:
        return "a course"
    title = (getattr(course, "course_title", None) or "").strip()
    if title:
        return title
    friendly = friendly_document_name(
        getattr(course, "source_doc_name", "") or "", course_document_display_map(course)
    )
    return friendly or "a course"


def shared_source_display_label(db, sharer_id: str, source_doc_name: str, course=None) -> str:
    """Label for a module share, which records a source document rather than a Course row."""
    if course is not None:
        return course_display_label(course)
    return display_name_for_stored_document(db, sharer_id, source_doc_name) or "shared modules"


def module_display_label(
    sharer_id: str,
    source_doc_name: str,
    report_filename: str,
    db,
    default: str = "a module",
) -> str:
    """
    Module TITLE for a generated-report filename, for notifications and review screens.

    A report filename is a storage key such as "<uuid>_seg3.md" - meaningless to the person
    reading the notification - so it is matched back to its module the same way the edit-review
    endpoints do: first on the filename base, then on the `_seg<n>` index.
    """
    if not report_filename:
        return default
    # Assessment-question edits store "question_<id>" rather than a report file.
    if str(report_filename).startswith("question_"):
        return "an assessment question"
    try:
        all_reports = _get_grouped_reports(sharer_id, db, include_content=False)
        target_group = next(
            (g for g in all_reports if g.get("source_doc") == source_doc_name), None
        )
        reports = (target_group or {}).get("reports") or []
        edit_filename_base = report_filename.replace(".md", "").split("/")[-1]
        for report in reports:
            report_filename_base = str(report.get("filename", "")).replace(".md", "").split("/")[-1]
            if report_filename_base == edit_filename_base or report.get("filename") == report_filename:
                title = (report.get("title") or "").strip()
                if title:
                    return title
        seg_match = re.search(r"_seg(\d+)", report_filename)
        if seg_match:
            seg_index = int(seg_match.group(1))
            for report in reports:
                if report.get("seg_index") == seg_index:
                    title = (report.get("title") or "").strip()
                    if title:
                        return title
    except Exception as exc:  # pragma: no cover - a label must never break the action
        logger.warning(
            "Could not resolve a module title for %s: %s",
            log_safe(report_filename),
            log_safe(exc),
        )
    return default


def display_name_for_stored_document(db, author_id: str, doc_name: str) -> str:
    """
    Display name for a storage document name, looked up across an author's courses.

    For callers that hold only a module's "source_doc" (a randomized storage name, VAPT #4)
    and no Course row - the single-module endpoints behind the Edit Module dialog. Any of the
    author's courses that lists the document supplies the mapping, since a storage name is
    unique per upload. Falls back to friendly_document_name() so the caller always gets
    something renderable rather than a bare UUID.
    """
    if not doc_name:
        return ""
    try:
        courses = db.query(Course).filter(Course.author_id == author_id).all()
    except Exception as exc:  # pragma: no cover - display path must never break an edit
        logger.debug("Could not load courses for a display-name lookup: %s", exc)
        return friendly_document_name(doc_name)

    for course in courses:
        display_map = course_document_display_map(course)
        mapped = display_map.get(doc_name)
        if mapped:
            return mapped
        # Also match on the normalized name, so a module whose source_doc was recorded
        # before storage names were randomized still resolves against its course.
        normalized = normalize_document_name(doc_name)
        for storage_name, display in display_map.items():
            if normalized and normalize_document_name(storage_name) == normalized:
                return display
    return friendly_document_name(doc_name)


def author_document_display_map(db, author_id) -> Dict[str, str]:
    """
    One merged storage-name -> uploaded-filename map covering every course an author owns.

    display_name_for_stored_document() reloads the author's courses on each call, which is fine
    for a single label but wasteful for a list of search hits that all belong to one trainer.
    This loads the courses once so a caller can resolve every hit from memory. Normalized
    aliases are stored alongside the storage names so a document recorded before storage names
    were randomized still resolves.

    author_id may also be a collection of ids, for a page that lists rows from several trainers
    (the super admin activity log); storage names are unique per upload, so one merged map cannot
    resolve a document to the wrong trainer's filename.
    """
    mapping: Dict[str, str] = {}
    author_ids = [author_id] if isinstance(author_id, str) else [a for a in (author_id or []) if a]
    if not author_ids:
        return mapping
    try:
        courses = db.query(Course).filter(Course.author_id.in_(author_ids)).all()
    except Exception as exc:  # pragma: no cover - a display path must never break a search
        logger.debug("Could not load courses for a display-name map: %s", exc)
        return mapping
    for course in courses:
        for storage_name, display in course_document_display_map(course).items():
            mapping.setdefault(storage_name, display)
            normalized = normalize_document_name(storage_name)
            if normalized:
                mapping.setdefault(normalized, display)
    return mapping


def display_name_from_map(display_map: Optional[Dict[str, str]], doc_name: str) -> str:
    """Resolve one storage name against a map built by author_document_display_map()."""
    if not doc_name:
        return ""
    if display_map:
        mapped = display_map.get(doc_name)
        if mapped:
            return mapped
        normalized = normalize_document_name(doc_name)
        if normalized:
            mapped = display_map.get(normalized)
            if mapped:
                return mapped
    return friendly_document_name(doc_name)



# An internal identifier appearing anywhere in a stored message:
#   * a randomized upload storage name, "<32 hex>" with an optional extension (VAPT #4 - the
#     extension is optional because randomized_storage_name() falls back to ".bin" for anything
#     off its allow-list), or
#   * a module's "<8-4-4-4-12>.md" report filename / a bare row id.
# The word boundaries keep this from biting a chunk out of a longer hex string such as a SHA-256,
# which has no boundary at offset 32.
_INTERNAL_IDENTIFIER = re.compile(
    r"\b(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:\.[A-Za-z0-9]{1,8})?\b",
    re.IGNORECASE,
)


def sanitize_notification_message(message: str, display_map: Optional[Dict[str, str]] = None) -> str:
    """
    Replace every internal identifier in a stored message with something a person can read.

    Notification and activity-log rows are written once and read forever, so rows created before
    the message builders learned to resolve display names still carry a randomized storage name
    (VAPT #4) or a module's "<uuid>.md" report filename. Rewriting them on the way out keeps those
    identifiers off the Notifications page and the super admin activity log without touching the
    stored audit trail.

    A storage name resolves through display_map to the filename that was uploaded, or to a generic
    "Uploaded PPTX" when the course that recorded it is gone. A module report filename carries no
    recoverable name at all - the surrounding sentence already says which course it belongs to - so
    it becomes a plain noun instead.
    """
    if not message:
        return ""

    def replace(match: "re.Match[str]") -> str:
        token = match.group(0)
        if "-" in token:
            return "a module" if token.lower().endswith(".md") else "this item"
        return display_name_from_map(display_map, token)

    return _INTERNAL_IDENTIFIER.sub(replace, message)


def course_notification_id(course) -> Optional[str]:
    """
    The course a notification is about, so deleting the course deletes the notification.

    Null-safe on purpose: several notification builders work from a share record whose course
    row may already be gone, and a notification is still worth sending in that case.
    """
    return getattr(course, "id", None)


def course_notification_id_for_document(db, author_id: str, doc_name: str) -> Optional[str]:
    """
    The course a source document belongs to, for notification builders that hold only the
    document (the single-module admin endpoints and the module-share review flows).

    Returns None rather than raising when nothing matches: the notification is still worth
    sending, it just will not be swept when a course is deleted.
    """
    if not author_id or not doc_name:
        return None
    try:
        courses = db.query(Course).filter(Course.author_id == author_id).all()
    except Exception as exc:  # pragma: no cover - a stamp must never break the action
        logger.debug("Could not resolve a course for a notification stamp: %s", exc)
        return None
    # The primary document first, then the multi-document list: a module's source_doc can be
    # any one of the documents a course was generated from, not just source_doc_name.
    for course in courses:
        if course.source_doc_name == doc_name:
            return course_notification_id(course)
    for course in courses:
        if doc_name in course_source_document_names(course):
            return course_notification_id(course)
    return None


def delete_notifications_for_course(db, course, doc_names: Optional[List[str]] = None) -> int:
    """
    Remove every notification that refers to a course being deleted. Returns the row count.

    Rows written since notifications carried a course_id are matched exactly. Older rows carry
    no reference at all, so they are matched on the quoted label the message builders use -
    "... the course 'Title' ...". The quotes are what make this safe: an unquoted substring
    match on a short title would sweep up unrelated rows, whereas every course-scoped message
    in the application wraps its label in single or double quotes.

    Deliberately not touched: rows that merely mention a trainee or a trainer, and anything
    from another feature (voice training scenarios, account approvals) - none of those quote a
    course label, so none of them match.
    """
    course_id = course_notification_id(course)
    labels = []
    if course is not None:
        title = (getattr(course, "course_title", None) or "").strip()
        if title:
            labels.append(title)
        display_map = course_document_display_map(course)
        for storage_name in doc_names or []:
            labels.append(friendly_document_name(storage_name, display_map))
            labels.append(storage_name)
        for display in display_map.values():
            labels.append(display)

    conditions = []
    if course_id:
        conditions.append(Notification.course_id == course_id)
        conditions.append(Notification.related_link.contains(course_id, autoescape=True))
    # autoescape keeps "%" or "_" inside a course title from acting as a LIKE wildcard; the
    # label is a bound parameter, never concatenated into SQL.
    for label in {str(label).strip() for label in labels if label and str(label).strip()}:
        conditions.append(Notification.message.contains(f"'{label}'", autoescape=True))
        conditions.append(Notification.message.contains(f'"{label}"', autoescape=True))
    if not conditions:
        return 0

    try:
        removed = (
            db.query(Notification)
            .filter(or_(*conditions))
            .delete(synchronize_session=False)
        )
        db.commit()
    except Exception as exc:  # pragma: no cover - a stale notification must not fail the delete
        db.rollback()
        logger.warning(
            "Could not delete notifications for course %s: %s", log_safe(course_id), log_safe(exc)
        )
        return 0
    if removed:
        logger.info(
            "[DELETE COURSE] Deleted %s notification(s) referring to course %s",
            removed,
            log_safe(course_id),
        )
    return removed


def _clean_display_filename(value: Optional[str]) -> str:
    """
    Reduce a candidate display filename to a bare, printable basename, or "" if unusable.

    The value reaching record_course_document_display_names() comes back from the curation screen,
    so it is client-supplied even though it originated server-side. It is never a path, a lookup
    key or an authorization input - only a label - but it is persisted and later rendered, so any
    directory component, control character or absurd length is dropped here rather than stored.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = os.path.basename(text.replace("\\", "/"))
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text[:255]


def record_course_document_display_names(course: Course, display_by_storage_name: Dict[str, str]) -> bool:
    """
    Fill in a course's missing storage-name -> uploaded-filename entries. Returns True if changed.

    Course.source_document_display_names is the ONLY record of what a trainer uploaded: storage
    names are randomized (VAPT #4) and nothing about the original name can be recovered from one.
    Rows written by paths that predate that column - or by a code path that created the Course
    without it - therefore render as "Uploaded PPTX" everywhere, which is exactly the complaint
    this repairs. The commit step knows both halves for every module it saves, so it backfills.

    Existing entries are never overwritten: the recorded name is the authoritative one, and a
    caller passing a stale label must not be able to rename a document. The two JSON lists stay
    positionally aligned, and the caller commits.
    """
    if not display_by_storage_name:
        return False

    storage_names = course_source_document_names(course)

    display_names: List[str] = []
    if course.source_document_display_names:
        try:
            raw_display = json.loads(course.source_document_display_names)
            if isinstance(raw_display, list):
                display_names = [str(d) if d else "" for d in raw_display]
        except (ValueError, TypeError):
            display_names = []

    # A document this course owns but never listed still needs a slot to hold its display name.
    for storage_name in display_by_storage_name:
        if storage_name and storage_name not in storage_names:
            storage_names.append(storage_name)

    display_names = (display_names + [""] * len(storage_names))[: len(storage_names)]

    changed = False
    for idx, storage_name in enumerate(storage_names):
        if display_names[idx]:
            continue
        candidate = _clean_display_filename(display_by_storage_name.get(storage_name))
        # A candidate that is itself a storage name, or the generic "Uploaded PPTX" label
        # friendly_document_name() falls back to, would record nothing worth keeping.
        if (
            not candidate
            or candidate == storage_name
            or _is_randomized_storage_name(candidate)
            or candidate == friendly_document_name(storage_name)
        ):
            continue
        display_names[idx] = candidate
        changed = True

    if not changed:
        return False

    course.source_documents = json.dumps(storage_names)
    course.source_document_display_names = json.dumps(display_names)
    return True


def apply_report_display_names(reports: List[Dict], display_map: Optional[Dict[str, str]] = None) -> None:
    """
    Stamp every curation report with the uploaded filename of its source document, in place.

    The Module Generation screen prints `Source: <doc_display_name>` above the markdown editor.
    Reports are built in several places (segmented generation, custom instructions, regeneration,
    and preserved modules carried over from a partial regeneration), and each report's doc_name is
    the randomized storage name (VAPT #4). Normalizing here means no producer can forget the
    display name and leak a hex string onto the screen.

    The map wins over any value a producer already set: regeneration labels every module with the
    document the trainer named, then remaps modules onto their real course slots, so a report can
    end up pointing at a different document of the same course. A name already on the report is
    kept only when the map cannot resolve its doc_name.
    """
    for report in reports or []:
        if not isinstance(report, dict):
            continue
        doc_name = report.get("doc_name") or ""
        mapped = ""
        if display_map and doc_name:
            mapped = display_map.get(doc_name) or display_map.get(normalize_document_name(doc_name)) or ""
        report["doc_display_name"] = (
            mapped or report.get("doc_display_name") or friendly_document_name(doc_name)
        )


__all__ = [
    "get_user_vector_store",
    "clear_all_data_for_user",
    "clean_text",
    "count_images_in_module",
    "extract_image_paths_from_module",
    "get_s3_signed_url",
    "get_image_from_s3_as_bytes",
    "load_image_analysis_from_s3",
    "save_image_analysis_to_s3",
    "normalize_document_name",
]
from core.runtime import *
from models.domain import *
from schemas.domain import *
from services.jobs import *
from services.auth_platform import *
from services.documents import *

def clean_text(text: str) -> str:
    text = re.sub(r'(\w+)-\s*\n\s*(\w+)', r'\1\2', text); text = re.sub(r'\n(?!\n)', ' ', text)
    return re.sub(r'[ \t]+', ' ', text).strip()

def filter_images_by_query_relevance(content: str, query: str, cached_analyses: dict, user_id: str, max_images_override: int | None = None) -> str:
    """
    AI-DRIVEN INTELLIGENT IMAGE FILTERING
    
    Uses cached AI analysis data to make completely dynamic decisions about image relevance.
    No hardcoded patterns - relies entirely on AI understanding of query intent and image content.
    Works for ANY domain: protocols, business processes, technical diagrams, charts, etc.
    """
    if not content or '[IMAGE:' not in content:
        return content
    
    image_tags = extract_image_tags(content)
    if not image_tags:
        return content
    
    logger.info(
        "[AI IMAGE FILTER] Analyzing %s images for query: %s",
        len(image_tags),
        log_safe(query[:100]),
    )
    
    # Prepare query for semantic analysis
    query_lower = query.lower()
    query_words = set(word.strip('.,!?;:()[]{}') for word in query_lower.split())
    
    # Remove only basic stop words, keep domain-specific terms
    basic_stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might', 'can'}
    meaningful_query_words = query_words - basic_stop_words
    
    # AI-DRIVEN QUERY ANALYSIS: Let AI determine query characteristics
    is_comparison_query = any(indicator in query_lower for indicator in ['vs', 'versus', 'compare', 'comparison', 'difference', 'between', 'against', 'contrast'])
    is_visual_query = any(indicator in query_lower for indicator in ['visual', 'diagram', 'architecture', 'overview', 'chart', 'graph', 'figure', 'image', 'structure', 'model', 'show', 'display', 'illustration'])
    is_specific_query = len(meaningful_query_words) <= 3  # Focused queries
    is_broad_query = len(meaningful_query_words) > 8     # Exploratory queries
    
    logger.info('[AI IMAGE FILTER] Query analysis - Comparison: %s, Visual: %s, Specific: %s, Broad: %s', ("" if is_comparison_query is None else str(is_comparison_query).replace("\n", "").replace("\r", "")), ("" if is_visual_query is None else str(is_visual_query).replace("\n", "").replace("\r", "")), ("" if is_specific_query is None else str(is_specific_query).replace("\n", "").replace("\r", "")), ("" if is_broad_query is None else str(is_broad_query).replace("\n", "").replace("\r", "")))
    
    # Score all images using AI analysis data
    image_scores = []
    
    for image_tag in image_tags:
        s3_key = image_tag.strip()
        analysis = cached_analyses.get(s3_key)
        
        if not analysis:
            # No analysis data - use conservative approach
            logger.info('[AI IMAGE FILTER] No AI analysis for %s, using neutral score', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            image_scores.append((s3_key, 40.0, {"reason": "No AI analysis data"}))
            continue
        
        # Extract AI-generated metadata
        ai_keywords = [kw.lower().strip() for kw in analysis.get('keywords', [])]
        ai_topics = [topic.lower().strip() for topic in analysis.get('topics', [])]
        ai_content = analysis.get('content', '').lower()
        ai_caption = analysis.get('caption', '').lower()
        ai_purpose = analysis.get('purpose', '').lower()
        ai_domain = analysis.get('domain', '').lower()
        ai_technical_terms = [term.lower().strip() for term in analysis.get('technical_terms', [])]
        
        # Combine all AI-generated text for comprehensive matching
        all_ai_text = f"{' '.join(ai_keywords)} {' '.join(ai_topics)} {ai_content} {ai_caption} {ai_purpose} {ai_domain} {' '.join(ai_technical_terms)}"
        
        # SEMANTIC RELEVANCE SCORING (100% AI-driven)
        relevance_score = 0
        score_details = {}
        
        # 1. DIRECT SEMANTIC MATCHING (40% weight)
        # Count how many query words appear in AI analysis
        semantic_matches = 0
        matched_terms = []
        for query_word in meaningful_query_words:
            if query_word in all_ai_text:
                semantic_matches += 1
                matched_terms.append(query_word)
            # Also check for partial matches in compound terms
            elif any(query_word in ai_term for ai_term in ai_keywords + ai_topics + ai_technical_terms):
                semantic_matches += 0.5
                matched_terms.append(f"{query_word}*")
        
        if meaningful_query_words:
            semantic_score = (semantic_matches / len(meaningful_query_words)) * 40
            relevance_score += semantic_score
            score_details['semantic'] = f"{semantic_score:.1f}% ({matched_terms})"
        
        # 2. CONTEXTUAL RELEVANCE (30% weight)
        # Use AI's understanding of image purpose and content
        contextual_score = 0
        if ai_purpose and any(word in ai_purpose for word in meaningful_query_words):
            contextual_score += 15  # Image purpose matches query
        if ai_domain and any(word in ai_domain for word in meaningful_query_words):
            contextual_score += 10  # Domain relevance
        if len(set(ai_topics) & meaningful_query_words) > 0:
            contextual_score += 5   # Topic overlap
        
        relevance_score += min(contextual_score, 30)
        score_details['contextual'] = f"{min(contextual_score, 30):.1f}%"
        
        # 3. QUERY TYPE ADAPTATION (20% weight)
        adaptation_score = 0
        
        if is_visual_query:
            # Boost images that AI identified as visual/diagrammatic
            visual_indicators = ['diagram', 'chart', 'graph', 'architecture', 'model', 'visualization', 'figure', 'illustration']
            if any(indicator in all_ai_text for indicator in visual_indicators):
                adaptation_score += 15
        
        if is_comparison_query:
            # For comparison queries, boost images that show multiple concepts
            comparison_indicators = ['comparison', 'versus', 'different', 'types', 'models', 'approaches']
            if any(indicator in all_ai_text for indicator in comparison_indicators):
                adaptation_score += 10
        
        if is_specific_query:
            # For specific queries, require higher precision
            if semantic_matches >= len(meaningful_query_words) * 0.7:  # 70% match required
                adaptation_score += 10
        
        relevance_score += min(adaptation_score, 20)
        score_details['adaptation'] = f"{min(adaptation_score, 20):.1f}%"
        
        # 4. AI CONFIDENCE BOOST (10% weight)
        # Use AI's own assessment of image complexity and clarity
        confidence_score = 0
        ai_complexity = analysis.get('complexity', '').lower()
        if ai_complexity in ['simple', 'clear'] and is_specific_query:
            confidence_score += 5
        elif ai_complexity in ['complex', 'detailed'] and is_broad_query:
            confidence_score += 5
        
        # Boost high-quality AI descriptions
        if len(ai_content) > 100:  # Detailed AI analysis
            confidence_score += 5
        
        relevance_score += min(confidence_score, 10)
        score_details['confidence'] = f"{min(confidence_score, 10):.1f}%"
        
        # Final score
        final_score = min(relevance_score, 100)
        image_scores.append((s3_key, final_score, score_details))
        
        logger.info('[AI IMAGE FILTER] %s: %s% - %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), f"{final_score:.1f}", ("" if score_details is None else str(score_details).replace("\n", "").replace("\r", "")))
    
    # AI-DRIVEN SELECTION LOGIC
    filtered_content = content
    removed_count = 0
    kept_count = 0
    
    # Dynamic threshold based on query characteristics and available images
    if not image_scores:
        return content
    
    # Calculate adaptive threshold
    scores = [score for _, score, _ in image_scores]
    avg_score = sum(scores) / len(scores)
    max_score = max(scores)
    
    # CRITICAL FIX: Much more lenient thresholds to prevent removing ALL images
    # The AI should see images and decide, rather than having them filtered out
    if is_comparison_query:
        # For comparisons, keep multiple relevant images
        threshold = max(10, avg_score * 0.4)  # Very low threshold
        max_images = min(len(image_scores), 5)  # Allow up to 5 images for comparisons
    elif is_visual_query:
        # For visual queries, prioritize high-quality visual content
        threshold = max(10, avg_score * 0.4)  # Very low threshold
        max_images = min(len(image_scores), 4)
    elif is_specific_query:
        # For specific queries, still be somewhat selective but not too strict
        threshold = max(15, avg_score * 0.5)
        max_images = min(len(image_scores), 3)
    else:
        # Default balanced approach - keep most images
        threshold = max(10, avg_score * 0.4)
        max_images = min(len(image_scores), 4)

    if max_images_override is not None:
        max_images = min(len(image_scores), max(1, max_images_override))
    
    # SAFETY NET: If all scores are low (avg < 20), keep at least 1-2 images
    # This prevents the case where NO images pass the threshold
    if avg_score < 20 and len(image_scores) > 0:
        threshold = 5  # Very low threshold to ensure at least some images pass
        if max_images_override is None:
            max_images = min(len(image_scores), 2)
        logger.warning('[AI IMAGE FILTER] Low average score (%s%), using safety threshold: %s', f"{avg_score:.1f}", ("" if threshold is None else str(threshold).replace("\n", "").replace("\r", "")))
    
    logger.info('[AI IMAGE FILTER] Adaptive threshold: %s%, Max images: %s', f"{threshold:.1f}", ("" if max_images is None else str(max_images).replace("\n", "").replace("\r", "")))
    
    # Sort by score and apply selection logic
    sorted_images = sorted(image_scores, key=lambda x: x[1], reverse=True)
    selected_images = []
    
    for s3_key, score, details in sorted_images:
        if len(selected_images) >= max_images:
            break
        if score >= threshold:
            selected_images.append(s3_key)
            logger.info('[AI IMAGE FILTER] ✓ Selected: %s (score: %s%)', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), f"{score:.1f}")
    
    # FINAL SAFETY NET: If NO images were selected but we have images, keep the top 1
    if len(selected_images) == 0 and len(sorted_images) > 0:
        top_image = sorted_images[0]
        selected_images.append(top_image[0])
        logger.warning('[AI IMAGE FILTER] ⚠ No images passed threshold, keeping top image: %s (score: %s%)', ("" if top_image[0] is None else str(top_image[0]).replace("\n", "").replace("\r", "")), f"{top_image[1]:.1f}")
    
    # Remove non-selected images from content
    for image_tag in image_tags:
        s3_key = image_tag.strip()
        if s3_key in selected_images:
            kept_count += 1
        else:
            # Find the score for logging
            image_score = next((score for key, score, _ in image_scores if key == s3_key), 0)
            logger.info('[AI IMAGE FILTER] ✗ Filtered out: %s (score: %s%)', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), f"{image_score:.1f}")
            
            # Remove from content
            filtered_content = filtered_content.replace(f'[IMAGE: {image_tag}]', '')
            filtered_content = re.sub(rf'\n\s*\[IMAGE:\s*{re.escape(image_tag)}\s*\]\s*\n', '\n', filtered_content)
            removed_count += 1
    
    logger.info('[AI IMAGE FILTER] ===== AI FILTERING COMPLETE: Kept %s, Filtered %s =====', ("" if kept_count is None else str(kept_count).replace("\n", "").replace("\r", "")), ("" if removed_count is None else str(removed_count).replace("\n", "").replace("\r", "")))
    return filtered_content

def is_plausible_s3_image_key(s3_key: str) -> bool:
    """True only for real user-scoped extracted_images paths (not captions or placeholders)."""
    key = (s3_key or "").strip()
    if not key or " " in key:
        return False
    if key.lower().startswith("user_id") or key.startswith("s3://"):
        return False
    if "/extracted_images/" not in key.replace("\\", "/"):
        return False
    parts = key.replace("\\", "/").split("/")
    return len(parts) >= 4


def filter_non_existent_images(content: str) -> str:
    """
    Remove [IMAGE: ...] tags for images that:
    1. Don't exist in S3
    2. Are watermarks, branding, or decorative (tiny file size or known patterns)
    Also strips obviously fake image patterns.
    """
    logger.info("[IMAGE FILTER] ===== FILTER CALLED =====")
    logger.info('[IMAGE FILTER] Content length: %s', len(content) if content else 0)
    
    if not content:
        logger.info("[IMAGE FILTER] No content to filter")
        return content
    
    content = strip_non_api_markdown_images(content)

    def _keep_api_image_url(url: str) -> bool:
        if url.startswith("/api/images/"):
            return True
        logger.warning("[IMAGE FILTER] Removing fake IMAGE_URL tag: %s", ("" if url is None else str(url).replace("\n", "").replace("\r", "")))
        return False

    content = strip_invalid_image_url_tags(content, _keep_api_image_url)

    image_tags = extract_image_tags(content)
    
    if not image_tags:
        logger.info("[IMAGE FILTER] No image tags found in content")
        return content
    
    logger.info('[IMAGE FILTER] Found %s image tag(s) in content', len(image_tags))
    
    removed_count = 0
    kept_count = 0
    
    # Check each image in S3
    _WATERMARK_PATTERNS = (
        'made_with', 'madewith', 'gamma', 'canva', 'watermark',
        'logo', 'badge', 'branding', 'powered_by', 'created_with',
        'slide_template', 'background',
    )
    _TINY_THRESHOLD = 8 * 1024  # 8 KB

    for s3_key in image_tags:
        s3_key = s3_key.strip()
        if not is_plausible_s3_image_key(s3_key):
            logger.warning(
                "[IMAGE FILTER] ✗ Invalid image path (not a real S3 key), removing: %s",
                s3_key[:80],
            )
            content = content.replace(f"[IMAGE: {s3_key}]", "")
            content = re.sub(
                rf"\n\s*\[IMAGE:\s*{re.escape(s3_key)}\s*\]\s*\n",
                "\n",
                content,
            )
            removed_count += 1
            continue
        filename_lower = s3_key.split('/')[-1].lower()

        # Check for watermark/branding patterns in filename first (no S3 call needed)
        if any(pat in filename_lower for pat in _WATERMARK_PATTERNS):
            logger.warning('[IMAGE FILTER] ✗ Watermark/branding image, removing: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            content = content.replace(f'[IMAGE: {s3_key}]', '')
            content = re.sub(rf'\n\s*\[IMAGE:\s*{re.escape(s3_key)}\s*\]\s*\n', '\n', content)
            removed_count += 1
            continue

        logger.info('[IMAGE FILTER] Checking image: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
        try:
            head = s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
            file_size = head.get('ContentLength', 0)

            if file_size < _TINY_THRESHOLD:
                logger.warning('[IMAGE FILTER] ✗ Tiny image (%s bytes) — likely watermark, removing: %s', ("" if file_size is None else str(file_size).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                content = content.replace(f'[IMAGE: {s3_key}]', '')
                content = re.sub(rf'\n\s*\[IMAGE:\s*{re.escape(s3_key)}\s*\]\s*\n', '\n', content)
                removed_count += 1
            else:
                logger.info('[IMAGE FILTER] ✓ Image exists (%s bytes): %s', ("" if file_size is None else str(file_size).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                kept_count += 1
        except ClientError as e:
            if e.response['Error']['Code'] == '404' or e.response['Error']['Code'] == 'NoSuchKey':
                logger.warning('[IMAGE FILTER] ✗ Image not found in S3, removing tag: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                content = content.replace(f'[IMAGE: {s3_key}]', '')
                content = re.sub(rf'\n\s*\[IMAGE:\s*{re.escape(s3_key)}\s*\]\s*\n', '\n', content)
                removed_count += 1
            else:
                logger.error('[IMAGE FILTER] Error checking image %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e)
        except Exception as e:
            logger.error('[IMAGE FILTER] Unexpected error checking image %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e)
    
    logger.info('[IMAGE FILTER] ===== FILTER COMPLETE: Kept %s, Removed %s =====', ("" if kept_count is None else str(kept_count).replace("\n", "").replace("\r", "")), ("" if removed_count is None else str(removed_count).replace("\n", "").replace("\r", "")))
    return content

def deduplicate_images_in_module(content: str) -> str:
    """
    Remove duplicate image tags from module content.
    Each image should appear only once in a module, even if the AI included it multiple times.
    """
    logger.info("[IMAGE DEDUP] ===== DEDUPLICATION STARTED =====")
    
    if not content:
        logger.info("[IMAGE DEDUP] No content to deduplicate")
        return content
    
    image_spans = list(iter_image_tag_spans(content))
    if not image_spans:
        logger.info("[IMAGE DEDUP] No image tags found")
        return content

    logger.info("[IMAGE DEDUP] Found %s image tag(s) in content", len(image_spans))

    seen_images = set()
    images_to_remove = []

    for start, end, s3_key in image_spans:
        if s3_key in seen_images:
            logger.warning("[IMAGE DEDUP] Duplicate image found: %s", ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            images_to_remove.append((start, end, s3_key))
        else:
            logger.info("[IMAGE DEDUP] Keeping first occurrence of: %s", ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            seen_images.add(s3_key)
    
    # Remove duplicates in reverse order to maintain correct positions
    for start, end, s3_key in reversed(images_to_remove):
        # Remove the image tag
        content = content[:start] + content[end:]
        
        # Also remove any surrounding whitespace/newlines if the image was on its own line
        content = re.sub(rf'\n\s*\n', '\n', content)  # Remove double newlines
        logger.info('[IMAGE DEDUP] Removed duplicate: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
    
    logger.info('[IMAGE DEDUP] ===== DEDUPLICATION COMPLETE: Kept %s, Removed %s =====', len(seen_images), len(images_to_remove))
    return content

def fix_ai_generated_image_paths(content: str, user_id: str, doc_name: str) -> str:
    """
    Fix AI-generated image paths using intelligent mapping with cached analysis.
    Ensures each image is used exactly once and mapped accurately based on content context.
    """
    if not content:
        return content
    
    logger.info('[IMAGE PATH FIX] Fixing AI-generated image paths for user %s, doc %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
    
    # Load cached image analysis to avoid redundant AI calls
    cached_analyses = load_image_analysis_from_s3(user_id, doc_name)
    
    # Get actual images from S3 (ordered by page/filename; no extraction list in this path)
    actual_images = get_actual_images_from_s3(user_id, doc_name)
    actual_images = order_images_for_module_catalog([], actual_images)
    if not actual_images:
        logger.warning('[IMAGE PATH FIX] No actual images found in S3 for %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
        return content
    
    # Enhance actual images with cached analysis data
    enhanced_images = []
    for img in actual_images:
        s3_key = img['s3_key']
        enhanced_img = img.copy()
        
        if s3_key in cached_analyses:
            cached_data = cached_analyses[s3_key]
            enhanced_img.update({
                'keywords': cached_data.get('keywords', []),
                'topics': cached_data.get('topics', []),
                'content_description': cached_data.get('content', ''),
                'image_type': cached_data.get('type', 'unknown'),
                'caption': cached_data.get('caption', 'Image')
            })
            logger.info('[CACHE] Using cached analysis for %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
        else:
            # Fallback to basic info if no cached analysis
            enhanced_img.update({
                'keywords': [],
                'topics': [],
                'content_description': '',
                'image_type': 'unknown',
                'caption': 'Image'
            })
            logger.info('[CACHE] No cached analysis found for %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
        
        enhanced_images.append(enhanced_img)
    
    # Find all image tags in content
    image_tags = extract_image_tags(content)
    if not image_tags:
        logger.info("[IMAGE PATH FIX] No image tags found in content")
        return content
    
    logger.info('[IMAGE PATH FIX] Found %s image tags, %s actual S3 images', len(image_tags), len(enhanced_images))
    
    # Track used images to prevent duplicates
    used_images = set()
    fixed_count = 0
    removed_count = 0
    
    # Process each image tag with intelligent mapping
    for image_tag in image_tags:
        image_tag = image_tag.strip()
        logger.info('[IMAGE PATH FIX] Processing image tag: %s', ("" if image_tag is None else str(image_tag).replace("\n", "").replace("\r", "")))
        
        # Check if this is already a valid S3 path
        try:
            s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=image_tag)
            if image_tag not in used_images:
                used_images.add(image_tag)
                logger.info('[IMAGE PATH FIX] ✓ Image already valid: %s', ("" if image_tag is None else str(image_tag).replace("\n", "").replace("\r", "")))
                continue
            else:
                logger.warning('[IMAGE PATH FIX] ⚠ Duplicate image detected: %s', ("" if image_tag is None else str(image_tag).replace("\n", "").replace("\r", "")))
                # This is a duplicate, need to find alternative
        except:
            logger.warning('[IMAGE PATH FIX] ✗ Invalid image path: %s', ("" if image_tag is None else str(image_tag).replace("\n", "").replace("\r", "")))
        
        # Find best matching unused image using content-based analysis
        best_match = None
        best_score = 0
        
        # Get surrounding content context for better matching
        tag_pattern = re.escape(f'[IMAGE: {image_tag}]')
        match = re.search(f'(.{{0,200}}){tag_pattern}(.{{0,200}})', content, re.DOTALL)
        context_text = ""
        if match:
            context_text = (match.group(1) + match.group(2)).lower()
        
        # Score each unused image
        for img in enhanced_images:
            if img['s3_key'] in used_images:
                continue  # Skip already used images
            
            score = 0
            
            # Score based on cached keywords
            for keyword in img.get('keywords', []):
                if keyword.lower() in context_text:
                    score += 15
                    logger.debug("[MAPPING] Keyword match '%s' for %s", ("" if keyword is None else str(keyword).replace("\n", "").replace("\r", "")), ("" if img['s3_key'] is None else str(img['s3_key']).replace("\n", "").replace("\r", "")))
            
            # Score based on cached topics
            for topic in img.get('topics', []):
                if topic.lower() in context_text:
                    score += 20
                    logger.debug("[MAPPING] Topic match '%s' for %s", ("" if topic is None else str(topic).replace("\n", "").replace("\r", "")), ("" if img['s3_key'] is None else str(img['s3_key']).replace("\n", "").replace("\r", "")))
            
            # Score based on image type
            img_type = img.get('image_type', '').lower()
            if img_type != 'unknown' and img_type in context_text:
                score += 25
                logger.debug("[MAPPING] Type match '%s' for %s", ("" if img_type is None else str(img_type).replace("\n", "").replace("\r", "")), ("" if img['s3_key'] is None else str(img['s3_key']).replace("\n", "").replace("\r", "")))
            
            # Score based on content description
            content_desc = img.get('content_description', '').lower()
            if content_desc:
                desc_words = set(content_desc.split())
                context_words = set(context_text.split())
                common_words = desc_words.intersection(context_words)
                score += len(common_words) * 3
                if common_words:
                    logger.debug('[MAPPING] Content match (%s words) for %s', len(common_words), ("" if img['s3_key'] is None else str(img['s3_key']).replace("\n", "").replace("\r", "")))
            
            # Score based on filename similarity (fallback)
            if score == 0:
                image_filename = image_tag.split('/')[-1] if '/' in image_tag else image_tag
                actual_filename = img['s3_key'].split('/')[-1]
                
                if image_filename.lower() == actual_filename.lower():
                    score = 10
                elif any(word in actual_filename.lower() for word in image_filename.lower().split('_')):
                    score = 5
            
            if score > best_score:
                best_match = img
                best_score = score
        
        # Apply the best match or remove if no good match
        if best_match and best_score > 0:
            old_tag = f'[IMAGE: {image_tag}]'
            new_tag = f'[IMAGE: {best_match["s3_key"]}]'
            content = content.replace(old_tag, new_tag)
            used_images.add(best_match['s3_key'])
            fixed_count += 1
            logger.info('[IMAGE PATH FIX] ✓ Intelligent mapping: %s -> %s (score: %s)', ("" if image_tag is None else str(image_tag).replace("\n", "").replace("\r", "")), ("" if best_match['s3_key'] is None else str(best_match['s3_key']).replace("\n", "").replace("\r", "")), ("" if best_score is None else str(best_score).replace("\n", "").replace("\r", "")))
        else:
            # No good match found, remove the image tag
            content = content.replace(f'[IMAGE: {image_tag}]', '')
            removed_count += 1
            logger.warning('[IMAGE PATH FIX] ✗ Removed image (no suitable match): %s', ("" if image_tag is None else str(image_tag).replace("\n", "").replace("\r", "")))
    
    # Report mapping statistics
    total_images = len(enhanced_images)
    used_count = len(used_images)
    unused_count = total_images - used_count
    
    logger.info('[IMAGE PATH FIX] Summary: Fixed %s, Removed %s image paths', ("" if fixed_count is None else str(fixed_count).replace("\n", "").replace("\r", "")), ("" if removed_count is None else str(removed_count).replace("\n", "").replace("\r", "")))
    logger.info('[IMAGE PATH FIX] Image usage: %s/%s used, %s unused', ("" if used_count is None else str(used_count).replace("\n", "").replace("\r", "")), ("" if total_images is None else str(total_images).replace("\n", "").replace("\r", "")), ("" if unused_count is None else str(unused_count).replace("\n", "").replace("\r", "")))
    
    if unused_count > 0:
        unused_images = [img['s3_key'] for img in enhanced_images if img['s3_key'] not in used_images]
        _unused_suffix = '...' if len(unused_images) > 3 else ''
        logger.warning(
            '[IMAGE PATH FIX] Unused images: %s%s',
            str(unused_images[:3]).replace("\n", "").replace("\r", ""),
            str(_unused_suffix).replace("\n", "").replace("\r", ""),
        )
    
    return content


def _module_images_need_commit_fix(content: str) -> bool:
    """Return True when commit must run expensive S3 image repair (already done at generation time)."""
    image_tags = extract_image_tags(content)
    if not image_tags:
        return False
    for tag in image_tags:
        key = tag.strip()
        if not key or not is_plausible_s3_image_key(key):
            return True
        try:
            s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=key)
        except ClientError:
            return True
        except Exception:
            return True
    return False


def _prepare_module_content_for_commit(content: str, user_id: str, doc_name: str) -> str:
    """
    Prepare module markdown for S3 storage during commit.
    Skips redundant S3 scans when image tags were already validated during generation.
    """
    if not content:
        return content
    final_content = deduplicate_images_in_module(content)
    if _module_images_need_commit_fix(final_content):
        final_content = fix_ai_generated_image_paths(final_content, user_id, doc_name)
        final_content = filter_non_existent_images(final_content)
        final_content = deduplicate_images_in_module(final_content)
    return final_content


def _embed_chunks_in_batches(chunks: list[str], batch_size: int = 20) -> list:
    """Embed text chunks in batches to avoid Bedrock token/limit errors."""
    if not chunks:
        return []
    all_vectors = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        batch_vectors = embedding_model.embed_documents(batch)
        all_vectors.extend(batch_vectors)
    return all_vectors


def _pinecone_metadata_text(chunk: str, max_bytes: int = 35000) -> str:
    """Keep Pinecone metadata text under the 40KB per-vector limit."""
    encoded = chunk.encode("utf-8")
    if len(encoded) <= max_bytes:
        return chunk
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# REMOVED: filter_images_by_relevance function - replaced with pure AI intelligence
# The AI agent now handles all relevance filtering using semantic understanding
# No hardcoded scoring or filtering rules - fully AI-driven approach

def validate_and_clean_image_tags(content: str, user_id: str) -> str:
    """
    Validate and clean image tags to ensure they are proper S3 keys.
    Remove any placeholder or invalid image tags.
    """
    if not content or '[IMAGE:' not in content:
        return content
    
    # Find all image tags
    image_tags = extract_image_tags(content)
    if not image_tags:
        return content
    
    logger.info('[IMAGE VALIDATION] Validating %s image tags', len(image_tags))
    
    cleaned_content = content
    removed_count = 0
    
    for image_tag in image_tags:
        s3_key = image_tag.strip()
        
        # Check for invalid patterns - be strict but allow valid S3 keys
        is_invalid = (
            not s3_key or
            s3_key.lower().startswith("user_id/") or
            s3_key == "user_id" or
            s3_key == 's3_key_path' or
            s3_key == 'path/to/image' or
            s3_key.startswith('placeholder') or
            s3_key.startswith('s3://') or  # S3 URLs are invalid
            s3_key.startswith('http') or   # External URLs
            s3_key.startswith('www') or    # Web URLs
            any(fake_name in s3_key for fake_name in ['diagrams/', 'fake-', 'placeholder-']) or    # Fake bucket names
            len(s3_key) < 20 or            # Too short to be real S3 key
            not '/' in s3_key or           # Must have path separators
            s3_key.count('/') < 2 or       # Must have at least user_id/folder/file structure
            # Check if it's a generic fake name (but allow real extracted image names)
            (len(s3_key) < 50 and any(fake_pattern in s3_key.lower() for fake_pattern in [
                'diagram.png', 'architecture.png', 'flowchart.png', 'chart.png', 'graph.png'
            ]) and '/extracted_images/' not in s3_key)
        )
        
        if is_invalid:
            logger.warning('[IMAGE VALIDATION] ✗ Removing invalid image tag: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            old_tag = f'[IMAGE: {image_tag}]'
            cleaned_content = cleaned_content.replace(old_tag, '')
            removed_count += 1
        else:
            logger.info('[IMAGE VALIDATION] ✓ Valid image tag: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
    
    if removed_count > 0:
        logger.info('[IMAGE VALIDATION] Cleaned content: removed %s invalid image tags', ("" if removed_count is None else str(removed_count).replace("\n", "").replace("\r", "")))
    
    return cleaned_content

def analyze_image_content_with_ai(image_bytes: bytes, context_text: str = "", s3_key: str = "") -> dict:
    """
    Analyze image content using AI vision to extract detailed information for accurate mapping.
    
    Args:
        image_bytes: The image data
        context_text: Surrounding text context
        s3_key: S3 key for reference
        
    Returns:
        Dictionary with caption, keywords, topics, and mapping info
    """
    try:
        # CRITICAL FIX: Resize image if dimensions exceed Bedrock's limit (8000 pixels)
        try:
            image_bytes = resize_image_if_needed(image_bytes, max_dimension=7900)
        except Exception as resize_error:
            logger.error('Image resize failed in analyze_image_content_with_ai: %s', log_safe(resize_error))
            # Return minimal analysis if resize fails
            return {
                'caption': 'Image (resize failed)',
                'keywords': [],
                'topics': [],
                'type': 'unknown',
                'content': '',
                's3_key': s3_key
            }
        
        # Create comprehensive prompt for image analysis
        prompt = f"""Analyze this image with high precision to support accurate content-based mapping.

Document Context (surrounding text): {context_text[:500] if context_text else "No context available"}

Provide a structured analysis with the following fields:

1. CAPTION: A precise, descriptive title for this image (max 50 words). Be specific about what is actually shown.
2. KEYWORDS: 8-15 highly specific keywords that describe the actual content of this image. Use terms that would appear in a document discussing this image.
3. TOPICS: 3-5 main subject areas or concepts this image covers. Be specific to the actual content, not generic categories.
4. TYPE: The exact visual type (e.g., flowchart, bar_chart, line_graph, architecture_diagram, process_flow, table, screenshot, photo, illustration, pie_chart, scatter_plot, network_diagram, timeline, etc.)
5. CONTENT: A detailed description of the specific concepts, data, processes, or information shown. Include all visible labels, values, and text.
6. TECHNICAL_TERMS: Any technical terms, acronyms, proper nouns, or domain-specific vocabulary visible in or directly described by the image.
7. VISUAL_ELEMENTS: Key structural components (e.g., axes, nodes, arrows, boxes, layers, connections, data points, legends).
8. PURPOSE: What specific concept or process this image is meant to explain or demonstrate.
9. DOMAIN: The specific subject domain of this image's content (derive from the actual content — do not guess or use generic labels).
10. IS_DECORATIVE: YES if this image is a watermark, logo, branding, background, or decorative element with no educational content. NO otherwise.

Be extremely specific. Use the actual terms visible in the image. This analysis is used for precise content matching.

Format:
CAPTION: [precise caption]
KEYWORDS: [keyword1, keyword2, ...]
TOPICS: [topic1, topic2, ...]
TYPE: [exact_type]
CONTENT: [detailed description with actual visible text/labels/values]
TECHNICAL_TERMS: [term1, term2, ...]
VISUAL_ELEMENTS: [element1, element2, ...]
PURPOSE: [specific purpose]
DOMAIN: [specific domain derived from content]
IS_DECORATIVE: [YES/NO]"""

        # Use Amazon Nova Pro for image analysis
        analysis_text, result = invoke_nova_pro_vision(
            sync_bedrock_client,
            image_bytes=image_bytes,
            prompt=prompt,
            image_format="png",
            model_id=BEDROCK_IMAGE_MODEL,
            max_tokens=2048,
            temperature=0.1,
        )
        try:
            from services.token_tracking import record_vision_tokens_from_response

            record_vision_tokens_from_response(result)
        except Exception as vision_track_exc:
            logger.debug("Vision token tracking skipped: %s", log_safe(vision_track_exc))
        analysis_text = analysis_text.strip()
        
        # Parse the enhanced structured response
        analysis = {
            'caption': 'Image',
            'keywords': [],
            'topics': [],
            'type': 'unknown',
            'content': '',
            'technical_terms': [],
            'visual_elements': [],
            'purpose': '',
            'domain': '',
            'complexity': 'Simple',
            'is_decorative': False,
            's3_key': s3_key
        }
        
        lines = analysis_text.split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('CAPTION:'):
                analysis['caption'] = line.replace('CAPTION:', '').strip()
            elif line.startswith('KEYWORDS:'):
                keywords_str = line.replace('KEYWORDS:', '').strip()
                analysis['keywords'] = [k.strip() for k in keywords_str.split(',') if k.strip()]
            elif line.startswith('TOPICS:'):
                topics_str = line.replace('TOPICS:', '').strip()
                analysis['topics'] = [t.strip() for t in topics_str.split(',') if t.strip()]
            elif line.startswith('TYPE:'):
                analysis['type'] = line.replace('TYPE:', '').strip()
            elif line.startswith('CONTENT:'):
                analysis['content'] = line.replace('CONTENT:', '').strip()
            elif line.startswith('TECHNICAL_TERMS:'):
                terms_str = line.replace('TECHNICAL_TERMS:', '').strip()
                analysis['technical_terms'] = [t.strip() for t in terms_str.split(',') if t.strip()]
            elif line.startswith('VISUAL_ELEMENTS:'):
                elements_str = line.replace('VISUAL_ELEMENTS:', '').strip()
                analysis['visual_elements'] = [e.strip() for e in elements_str.split(',') if e.strip()]
            elif line.startswith('PURPOSE:'):
                analysis['purpose'] = line.replace('PURPOSE:', '').strip()
            elif line.startswith('DOMAIN:'):
                analysis['domain'] = line.replace('DOMAIN:', '').strip()
            elif line.startswith('COMPLEXITY:'):
                analysis['complexity'] = line.replace('COMPLEXITY:', '').strip()
            elif line.startswith('IS_DECORATIVE:'):
                val = line.replace('IS_DECORATIVE:', '').strip().upper()
                analysis['is_decorative'] = (val == 'YES')

        if analysis['is_decorative']:
            logger.info('[IMAGE ANALYSIS] Image flagged as decorative/non-educational: %s', log_safe(s3_key))

        logger.info(
            "[IMAGE ANALYSIS] %s: %s keywords, type=%s, decorative=%s, domain=%s",
            log_safe(s3_key),
            len(analysis["keywords"]),
            log_safe(analysis["type"]),
            analysis["is_decorative"],
            log_safe(analysis["domain"]),
        )
        
        return analysis
        
    except Exception as exc:
        logger.warning('Failed to analyze image content: %s', log_safe(exc))
        return {
            'caption': 'Image',
            'keywords': [],
            'topics': [],
            'type': 'unknown',
            'content': '',
            's3_key': s3_key
        }

def load_image_analysis_from_s3(user_id: str, doc_name: str) -> dict:
    """
    Load cached image analysis data from S3 to avoid redundant AI calls.
    
    Args:
        user_id: User ID
        doc_name: Document name
        
    Returns:
        Dictionary mapping S3 keys to their analysis data
    """
    try:
        logger.info('[CACHE] Loading cached image analysis for %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
        
        # List all analysis files for this document
        analysis_prefix = f"{user_id}/image_analysis/{secure_filename(doc_name)}/"
        
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=analysis_prefix)
        
        cached_analyses = {}
        
        for page in pages:
            for obj in page.get('Contents', []):
                analysis_key = obj['Key']
                if not analysis_key.lower().endswith('.json'):
                    continue
                try:
                    response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=analysis_key)
                    analysis_data = json.loads(response['Body'].read().decode('utf-8'))

                    if not isinstance(analysis_data, dict):
                        continue

                    # Legacy save shape: { "user/.../frame.jpg": { caption, keywords, ... } }
                    if 's3_key' not in analysis_data:
                        for key, val in analysis_data.items():
                            if not isinstance(val, dict):
                                continue
                            if '/' not in key and not key.lower().endswith(
                                ('.jpg', '.jpeg', '.png', '.webp')
                            ):
                                continue
                            entry = dict(val)
                            entry.setdefault('s3_key', key)
                            cached_analyses[key] = entry
                        continue

                    image_s3_key = analysis_data.get('s3_key', '')
                    if image_s3_key:
                        cached_analyses[image_s3_key] = analysis_data
                        logger.info('[CACHE] Loaded analysis for %s', ("" if image_s3_key is None else str(image_s3_key).replace("\n", "").replace("\r", "")))

                except Exception as e:
                    logger.warning('[CACHE] Failed to load analysis from %s: %s', ("" if analysis_key is None else str(analysis_key).replace("\n", "").replace("\r", "")), e)
        
        logger.info('[CACHE] Loaded %s cached image analyses', len(cached_analyses))
        return cached_analyses
        
    except Exception as e:
        logger.error('[CACHE] Failed to load cached image analyses: %s', e)
        return {}

def save_image_analysis_to_s3(user_id: str, doc_name: str, image_analysis: dict) -> str:
    """
    Save image analysis data to S3 for future reference and accurate mapping.
    
    Args:
        user_id: User ID
        doc_name: Document name
        image_analysis: Analysis data from AI
        
    Returns:
        S3 key where analysis was saved
    """
    try:
        # Create analysis filename based on image S3 key
        image_s3_key = image_analysis.get('s3_key', '')
        if image_s3_key:
            # Extract image filename from S3 key
            image_filename = image_s3_key.split('/')[-1]
            analysis_filename = f"{image_filename}_analysis.json"
        else:
            analysis_filename = f"image_analysis_{int(time.time())}.json"
        
        analysis_s3_key = f"{user_id}/image_analysis/{secure_filename(doc_name)}/{analysis_filename}"
        
        # Save analysis data
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=analysis_s3_key,
            Body=json.dumps(image_analysis, indent=2),
            ContentType='application/json'
        )
        
        logger.info('Saved image analysis to S3: %s', ("" if analysis_s3_key is None else str(analysis_s3_key).replace("\n", "").replace("\r", "")))
        return analysis_s3_key
        
    except Exception as e:
        logger.error('Failed to save image analysis to S3: %s', e)
        return ""

def create_intelligent_image_mapping(segments: list, image_analyses: list) -> dict:
    """
    Assign each non-decorative image to exactly one module segment.

    Uses document position (page / slide / filename hints) so figures follow the
    source layout across pages and are split across modules instead of piling
    into the first segment. When every image collapses to a single page, falls
    back to an even spread by catalog reading order.
    """
    if not image_analyses:
        return {}

    educational_images = [img for img in image_analyses if not img.get("is_decorative", False)]
    decorative_count = len(image_analyses) - len(educational_images)
    if decorative_count:
        logger.info(
            f"[MAPPING] Excluded {decorative_count} decorative/non-educational image(s) before mapping"
        )

    if not educational_images:
        logger.info("[MAPPING] No educational images to map")
        return {}

    num_segments = len(segments)
    if num_segments <= 0:
        return {}

    pages = [_effective_page_from_image_analysis(img) for img in educational_images]
    total_pages = max(pages) if pages else 1
    meaningful_pages = total_pages > 1 or any(p > 1 for p in pages)
    n_img = len(educational_images)

    logger.info(
        f"[MAPPING] Page-aware mapping: {num_segments} segment(s), {n_img} image(s), "
        f"total_pages={total_pages}, meaningful_multi_page={meaningful_pages}"
    )

    final_mapping: Dict[int, List[Dict]] = {}
    mapping_decisions: List[Dict] = []

    for reading_order, img_analysis in enumerate(educational_images):
        s3_key = img_analysis.get("s3_key")
        if not s3_key:
            continue
        page = _effective_page_from_image_analysis(img_analysis)
        if num_segments <= 1:
            seg_idx = 0
        elif meaningful_pages:
            seg_idx = segment_index_for_document_page(page, total_pages, num_segments)
        else:
            seg_idx = min(num_segments - 1, reading_order * num_segments // max(n_img, 1))

        entry = {
            "s3_key": s3_key,
            "analysis": img_analysis,
            "confidence_score": float(reading_order),
            "confidence_breakdown": {},
            "confidence_level": "PAGE_BIN",
        }
        final_mapping.setdefault(seg_idx, []).append(entry)
        mapping_decisions.append(
            {"segment": seg_idx, "image": s3_key, "score": float(page), "level": "PAGE_BIN"}
        )
        logger.info(
            f"[MAPPING] PAGE_BIN: page={page} reading_order={reading_order} -> segment {seg_idx} ({s3_key.split('/')[-1]})"
        )

    def _segment_image_reading_order(entry: dict) -> Tuple[int, int, str]:
        analysis = entry.get("analysis") or {}
        page = analysis.get("document_page")
        if page is None:
            page = analysis.get("page_num")
        try:
            page_i = int(page)
        except (TypeError, ValueError):
            page_i = 999999
        ro = analysis.get("reading_order")
        try:
            ro_i = int(ro)
        except (TypeError, ValueError):
            ro_i = 999999
        return (page_i, ro_i, entry.get("s3_key") or "")

    for _seg_idx, img_list in list(final_mapping.items()):
        if len(img_list) > 1:
            final_mapping[_seg_idx] = sorted(img_list, key=_segment_image_reading_order)

    total_mapped = sum(len(v) for v in final_mapping.values())
    logger.info("[MAPPING] ===== FINAL REPORT (page-aware) =====")
    logger.info('[MAPPING] Total images: %s', len(image_analyses))
    logger.info('[MAPPING] Decorative (excluded): %s', ("" if decorative_count is None else str(decorative_count).replace("\n", "").replace("\r", "")))
    logger.info('[MAPPING] Educational: %s', len(educational_images))
    logger.info('[MAPPING] Mapped: %s', ("" if total_mapped is None else str(total_mapped).replace("\n", "").replace("\r", "")))

    validate_mapping_quality(final_mapping, mapping_decisions, educational_images)
    return final_mapping

def validate_mapping_quality(final_mapping: dict, mapping_decisions: list, image_analyses: list):
    """Validate the quality of the image mapping with detailed reporting."""
    try:
        logger.info("[QUALITY VALIDATION] ===== MAPPING QUALITY REPORT =====")
        
        # Check for perfect 1:1 mapping
        total_images = len(image_analyses)
        mapped_images = sum(len(images) for images in final_mapping.values())
        
        if mapped_images == total_images:
            logger.info('[QUALITY VALIDATION] ✓ Perfect 1:1 mapping achieved: %s/%s', ("" if mapped_images is None else str(mapped_images).replace("\n", "").replace("\r", "")), ("" if total_images is None else str(total_images).replace("\n", "").replace("\r", "")))
        else:
            logger.warning('[QUALITY VALIDATION] ⚠ Mapping incomplete: %s/%s', ("" if mapped_images is None else str(mapped_images).replace("\n", "").replace("\r", "")), ("" if total_images is None else str(total_images).replace("\n", "").replace("\r", "")))
        
        # Analyze confidence distribution
        confidence_distribution = {}
        for decision in mapping_decisions:
            level = decision['level']
            confidence_distribution[level] = confidence_distribution.get(level, 0) + 1
        
        logger.info('[QUALITY VALIDATION] Confidence distribution: %s', ("" if confidence_distribution is None else str(confidence_distribution).replace("\n", "").replace("\r", "")))
        
        # Check for high-quality mappings
        high_quality_count = confidence_distribution.get('ULTRA_HIGH', 0) + confidence_distribution.get('HIGH', 0)
        quality_percentage = (high_quality_count / total_images * 100) if total_images > 0 else 0
        
        if quality_percentage >= 80:
            logger.info('[QUALITY VALIDATION] ✓ EXCELLENT mapping quality: %s% high confidence', f"{quality_percentage:.1f}")
        elif quality_percentage >= 60:
            logger.info('[QUALITY VALIDATION] ✓ GOOD mapping quality: %s% high confidence', f"{quality_percentage:.1f}")
        else:
            logger.warning('[QUALITY VALIDATION] ⚠ MODERATE mapping quality: %s% high confidence', f"{quality_percentage:.1f}")
        
        # Log detailed mapping decisions for review
        for decision in mapping_decisions[:5]:  # Show top 5 decisions
            logger.info(
                "[QUALITY VALIDATION] Top mapping: Segment %s <- %s (Score: %.2f, %s)",
                ("" if decision["segment"] is None else str(decision["segment"]).replace("\n", "").replace("\r", "")),
                ("" if decision["image"].split("/")[-1] is None else str(decision["image"].split("/")[-1]).replace("\n", "").replace("\r", "")),
                decision["score"],
                ("" if decision["level"] is None else str(decision["level"]).replace("\n", "").replace("\r", "")),
            )
        
        logger.info("[QUALITY VALIDATION] ===== VALIDATION COMPLETE =====")
        
    except Exception as e:
        logger.error('Error in quality validation: %s', e)

def extract_key_phrases(text: str) -> list:
    """Extract key phrases from text using advanced NLP techniques."""
    try:
        # Simple key phrase extraction - can be enhanced with more sophisticated NLP
        words = text.lower().split()
        # Filter out common words and extract meaningful phrases
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might', 'can', 'this', 'that', 'these', 'those'}
        key_words = [word for word in words if len(word) > 3 and word not in stop_words]
        
        # Extract 2-3 word phrases
        phrases = []
        for i in range(len(key_words) - 1):
            phrase = f"{key_words[i]} {key_words[i+1]}"
            phrases.append(phrase)
        
        return list(set(phrases[:10]))  # Return top 10 unique phrases
    except:
        return []

def extract_technical_terms(text: str) -> list:
    """Extract technical terms and domain-specific vocabulary."""
    try:
        # Look for capitalized terms, acronyms, and technical patterns
        import re
        
        technical_patterns = [
            r'\b[A-Z]{2,}\b',  # Acronyms
            r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b',  # Proper nouns/technical terms
            r'\b\w+(?:[-_]\w+)+\b',  # Hyphenated/underscore terms
            r'\b\d+(?:\.\d+)*\b',  # Numbers and versions
        ]
        
        technical_terms = []
        for pattern in technical_patterns:
            matches = re.findall(pattern, text)
            technical_terms.extend(matches)
        
        return list(set(technical_terms[:15]))  # Return top 15 unique terms
    except:
        return []

def extract_context_indicators(text: str) -> dict:
    """Extract context indicators like figures, tables, diagrams mentioned in text."""
    try:
        import re
        text_lower = text.lower()
        
        indicators = {
            'figure_references': len(re.findall(r'\bfigure\s+\d+|\bfig\.\s+\d+|\bfig\s+\d+', text_lower)),
            'table_references': len(re.findall(r'\btable\s+\d+|\btbl\.\s+\d+', text_lower)),
            'diagram_mentions': len(re.findall(r'\bdiagram|\bschematic|\bflowchart|\barchitecture', text_lower)),
            'chart_mentions': len(re.findall(r'\bchart|\bgraph|\bplot|\bvisualization', text_lower)),
            'image_mentions': len(re.findall(r'\bimage|\bpicture|\bphoto|\billustration', text_lower)),
            'visual_indicators': len(re.findall(r'\bshown|\bdisplayed|\billustrated|\bdepicted|\bvisualized', text_lower)),
            'mathematical_content': len(re.findall(r'\bequation|\bformula|\bcalculation|\balgorithm', text_lower)),
            'process_indicators': len(re.findall(r'\bprocess|\bstep|\bprocedure|\bworkflow|\bmethodology', text_lower))
        }
        
        return indicators
    except:
        return {}

def extract_semantic_topics(text: str) -> list:
    """
    Extract semantic topics from text using frequency-based term extraction.
    Domain-agnostic: works by identifying the most prominent noun phrases and
    repeated significant terms in the text, without any hardcoded domain assumptions.
    """
    try:
        import re
        text_lower = text.lower()

        # Remove common stop words and extract candidate terms
        stop_words = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
            'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
            'should', 'may', 'might', 'can', 'this', 'that', 'these', 'those',
            'it', 'its', 'as', 'from', 'into', 'through', 'during', 'before',
            'after', 'above', 'below', 'between', 'each', 'more', 'most', 'other',
            'some', 'such', 'than', 'then', 'there', 'they', 'their', 'them',
            'when', 'where', 'which', 'who', 'how', 'what', 'also', 'not', 'no',
            'if', 'so', 'up', 'out', 'about', 'all', 'both', 'any', 'only',
            'same', 'very', 'just', 'over', 'our', 'we', 'you', 'your', 'he',
            'she', 'his', 'her', 'my', 'me', 'us', 'i', 'am', 'use', 'used',
            'using', 'based', 'new', 'one', 'two', 'three', 'first', 'second',
            'third', 'can', 'well', 'way', 'ways', 'make', 'made', 'making',
            'get', 'set', 'see', 'like', 'need', 'needs', 'provide', 'provides',
            'include', 'includes', 'allow', 'allows', 'ensure', 'ensures',
        }

        # Tokenize into words (alphanumeric only, length > 3)
        words = re.findall(r'\b[a-z][a-z0-9_-]{2,}\b', text_lower)
        filtered = [w for w in words if w not in stop_words and len(w) > 3]

        # Count frequency
        freq = {}
        for w in filtered:
            freq[w] = freq.get(w, 0) + 1

        # Sort by frequency descending, take top terms as topics
        sorted_terms = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        top_topics = [term for term, count in sorted_terms[:8] if count >= 2]

        return top_topics

    except Exception:
        return []

def calculate_ultra_confidence_score(segment_data: dict, image_analysis: dict) -> dict:
    """
    Calculate confidence score for mapping an image to a segment.
    Domain-agnostic: all scoring is driven purely by the actual content of the
    image analysis and the segment text — no hardcoded domain assumptions.

    METADATA VALIDATION GATE: If the image's AI-extracted metadata (caption,
    keywords, topics, content description) has zero meaningful overlap with the
    segment text, the image is rejected outright (score = 0, level = REJECTED).
    This prevents irrelevant images (watermarks, logos, off-topic visuals) from
    being mapped to any module.
    """
    try:
        breakdown = {}
        total_score = 0.0

        seg_text_lower = segment_data['text_lower']
        seg_words = segment_data['words']

        # ── METADATA VALIDATION GATE ──────────────────────────────────────────
        # Collect all meaningful text from the image's AI metadata
        image_keywords = [kw.lower().strip() for kw in image_analysis.get('keywords', [])]
        image_topics   = [t.lower().strip()  for t in image_analysis.get('topics', [])]
        image_caption  = image_analysis.get('caption', '').lower()
        image_content  = image_analysis.get('content', '').lower()
        image_tech     = [t.lower().strip()  for t in image_analysis.get('technical_terms', [])]
        image_purpose  = image_analysis.get('purpose', '').lower()
        image_domain   = image_analysis.get('domain', '').lower()

        # Build a flat set of all significant words from image metadata
        import re as _re
        stop = {'the','a','an','and','or','but','in','on','at','to','for','of','with',
                'by','is','are','was','were','be','been','have','has','had','do','does',
                'did','this','that','these','those','it','its','as','from','into','not',
                'no','if','so','up','out','about','all','both','any','only','same','very',
                'just','over','our','we','you','your','he','she','his','her','my','me',
                'us','i','am','use','used','using','based','new','one','two','three',
                'image','shows','diagram','figure','chart','graph','visual','display',
                'shown','depicted','illustrates','represents','contains','includes'}

        def _words(s: str) -> set:
            return {w for w in _re.findall(r'\b[a-z][a-z0-9_-]{2,}\b', s) if w not in stop}

        meta_words = set()
        for kw in image_keywords:
            meta_words.update(_words(kw))
        for tp in image_topics:
            meta_words.update(_words(tp))
        meta_words.update(_words(image_caption))
        meta_words.update(_words(image_content))
        for tt in image_tech:
            meta_words.update(_words(tt))
        meta_words.update(_words(image_purpose))
        meta_words.update(_words(image_domain))

        # Count how many metadata words appear in the segment text
        overlap = meta_words.intersection(seg_words)
        breakdown['metadata_overlap_count'] = len(overlap)
        breakdown['metadata_overlap_words'] = list(overlap)[:10]

        # GATE: require at least 2 overlapping meaningful words to proceed
        # (1 word could be coincidental; 0 means completely unrelated)
        if len(overlap) < 2:
            logger.debug(
                "[GATE] Image %s REJECTED for segment %s: only %s metadata word(s) overlap",
                ("" if image_analysis.get("s3_key", "?") is None else str(image_analysis.get("s3_key", "?")).replace("\n", "").replace("\r", "")),
                ("" if segment_data["index"] is None else str(segment_data["index"]).replace("\n", "").replace("\r", "")),
                len(overlap),
            )
            return {
                'total_score': 0.0,
                'confidence_level': 'REJECTED',
                'breakdown': breakdown
            }

        # ── FACTOR 1: Keyword Matching (30%) ──────────────────────────────────
        keyword_score = 0
        for keyword in image_keywords:
            kw_words = _words(keyword)
            direct_hits = kw_words.intersection(seg_words)
            if direct_hits:
                keyword_score += 20 * len(direct_hits)
                breakdown[f'kw_direct_{keyword[:20]}'] = 20 * len(direct_hits)
            # Substring match for compound terms
            elif keyword in seg_text_lower:
                keyword_score += 15
                breakdown[f'kw_substr_{keyword[:20]}'] = 15

        total_score += keyword_score * 0.30
        breakdown['keyword_total'] = keyword_score

        # ── FACTOR 2: Topic Alignment (25%) ───────────────────────────────────
        topic_score = 0
        seg_topics = segment_data.get('semantic_topics', [])
        for img_topic in image_topics:
            if img_topic in seg_text_lower:
                topic_score += 30
                breakdown[f'topic_direct_{img_topic[:20]}'] = 30
            else:
                # Word-level overlap between image topic and segment topics
                img_topic_words = _words(img_topic)
                for seg_topic in seg_topics:
                    seg_topic_words = _words(seg_topic)
                    common = img_topic_words.intersection(seg_topic_words)
                    if common:
                        topic_score += 15 * len(common)
                        breakdown[f'topic_semantic_{img_topic[:15]}'] = 15 * len(common)

        total_score += topic_score * 0.25
        breakdown['topic_total'] = topic_score

        # ── FACTOR 3: Content Description Similarity (20%) ────────────────────
        content_score = 0
        if image_content:
            content_words = _words(image_content)
            common_content = seg_words.intersection(content_words)
            content_score = len(common_content) * 8
            breakdown['content_word_matches'] = len(common_content)

        total_score += content_score * 0.20
        breakdown['content_total'] = content_score

        # ── FACTOR 4: Technical Term Alignment (15%) ──────────────────────────
        tech_score = 0
        for img_term in image_tech:
            if img_term in seg_text_lower:
                tech_score += 25
                breakdown[f'tech_direct_{img_term[:20]}'] = 25
            else:
                term_words = _words(img_term)
                if term_words.intersection(seg_words):
                    tech_score += 12
                    breakdown[f'tech_partial_{img_term[:20]}'] = 12

        # Caption technical overlap
        caption_words = _words(image_caption)
        caption_overlap = caption_words.intersection(seg_words)
        tech_score += len(caption_overlap) * 10
        breakdown['caption_overlap'] = len(caption_overlap)

        total_score += tech_score * 0.15
        breakdown['tech_total'] = tech_score

        # ── FACTOR 5: Context Indicators (10%) ────────────────────────────────
        # Reward segments that explicitly reference visual content
        context_score = 0
        ctx = segment_data.get('context_indicators', {})
        visual_refs = (
            ctx.get('figure_references', 0) * 25 +
            ctx.get('diagram_mentions', 0) * 20 +
            ctx.get('chart_mentions', 0) * 20 +
            ctx.get('table_references', 0) * 15 +
            ctx.get('visual_indicators', 0) * 10 +
            ctx.get('image_mentions', 0) * 10
        )
        context_score = min(visual_refs, 100)  # cap at 100
        breakdown['context_visual_refs'] = context_score

        total_score += context_score * 0.10
        breakdown['context_total'] = context_score

        # ── Confidence Level ───────────────────────────────────────────────────
        if total_score >= 120:
            confidence_level = 'ULTRA_HIGH'
        elif total_score >= 80:
            confidence_level = 'HIGH'
        elif total_score >= 50:
            confidence_level = 'MEDIUM'
        elif total_score >= 25:
            confidence_level = 'LOW'
        else:
            confidence_level = 'VERY_LOW'

        return {
            'total_score': total_score,
            'confidence_level': confidence_level,
            'breakdown': breakdown
        }

    except Exception as e:
        logger.error('Error calculating confidence score: %s', e)
        return {
            'total_score': 0.0,
            'confidence_level': 'ERROR',
            'breakdown': {'error': str(e)}
        }

def find_best_fallback_segment(image_analysis: dict, processed_segments: list, current_mapping: dict) -> int:
    """Find the best segment for fallback assignment."""
    try:
        # Find segments with fewest images assigned
        segment_counts = []
        for seg_data in processed_segments:
            seg_idx = seg_data['index']
            current_image_count = len(current_mapping.get(seg_idx, []))
            segment_counts.append((current_image_count, seg_idx, seg_data))
        
        # Sort by image count (ascending) and segment length (descending for substantial content)
        segment_counts.sort(key=lambda x: (x[0], -x[2]['length']))
        
        # Return the segment with fewest images and substantial content
        for count, seg_idx, seg_data in segment_counts:
            if seg_data['length'] > 50:  # Ensure segment has substantial content
                return seg_idx
        
        # If no substantial segment found, return the first one
        return segment_counts[0][1] if segment_counts else 0
        
    except Exception as e:
        logger.error('Error finding fallback segment: %s', e)
        return 0

def generate_ai_image_caption(image_bytes: bytes, context_text: str = "") -> str:
    """
    Generate a meaningful caption for an image using AI vision analysis.
    
    Args:
        image_bytes: The image data
        context_text: Surrounding text context for better understanding
        
    Returns:
        A descriptive caption for the image
    """
    try:
        # Create a concise prompt for caption generation
        prompt = f"""Analyze this image and provide a short, descriptive caption (max 50 words).

Context from document: {context_text[:200] if context_text else "No context available"}

Focus on:
- What the image shows (diagram, chart, photo, etc.)
- Key elements or components
- Purpose or function if apparent

Provide only the caption, no additional text."""

        # Use Amazon Nova Pro for image analysis
        analysis_text, result = invoke_nova_pro_vision(
            sync_bedrock_client,
            image_bytes=image_bytes,
            prompt=prompt,
            image_format="png",
            model_id=BEDROCK_IMAGE_MODEL,
            max_tokens=2048,
            temperature=0.1,
        )
        try:
            from services.token_tracking import record_vision_tokens_from_response

            record_vision_tokens_from_response(result)
        except Exception as vision_track_exc:
            logger.debug("Vision token tracking skipped: %s", log_safe(vision_track_exc))
        caption = analysis_text.strip()
        
        # Clean up the caption
        caption = caption.replace('"', '').replace("'", "").strip()
        if len(caption) > 100:
            caption = caption[:97] + "..."
            
        return caption if caption else "Image"
        
    except Exception as exc:
        logger.warning('Failed to generate AI caption: %s', log_safe(exc))
        return "Image"

def ensure_trainee_image_inclusion(content: str, user_id: str, doc_name: str) -> str:
    """
    Ensure that trainees get comprehensive image inclusion in their modules.
    This function validates and enhances image content specifically for trainee learning.
    """
    try:
        if not content or '[IMAGE:' not in content:
            logger.info('[TRAINEE IMAGES] No images found in content for %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            return content
        
        # Count current images
        current_images = extract_image_tags(content)
        logger.info('[TRAINEE IMAGES] Found %s images in module content', len(current_images))
        
        # Validate each image exists and is accessible
        valid_images = []
        for s3_key in current_images:
            s3_key = s3_key.strip()
            try:
                # Check if image exists in S3
                s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
                valid_images.append(s3_key)
                logger.info('[TRAINEE IMAGES] ✓ Validated image: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            except ClientError as e:
                if e.response['Error']['Code'] in ['404', 'NoSuchKey']:
                    logger.warning('[TRAINEE IMAGES] ✗ Removing invalid image: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                    # Remove invalid image tag
                    content = content.replace(f'[IMAGE: {s3_key}]', '')
                else:
                    logger.error('[TRAINEE IMAGES] Error checking image %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e)
        
        logger.info('[TRAINEE IMAGES] Final validation: %s valid images for trainee module', len(valid_images))
        return content
        
    except Exception as e:
        logger.error('[TRAINEE IMAGES] Error ensuring trainee image inclusion: %s', e)
        return content

def verify_image_mapping_accuracy(generated_modules: list, available_images: list) -> dict:
    """
    Verify that all images are mapped correctly with 300% accuracy.
    
    Args:
        generated_modules: List of generated module dictionaries
        available_images: List of available image analyses
        
    Returns:
        Dictionary with verification results
    """
    logger.info("[VERIFICATION] Starting 200% accuracy image mapping verification")
    
    # Collect all image references from modules
    used_images = set()
    duplicate_images = []
    missing_images = []
    
    for module in generated_modules:
        content = module.get('edited_content', '')
        module_images = extract_image_tags(content)
        
        for img_path in module_images:
            img_path = img_path.strip()
            if img_path in used_images:
                duplicate_images.append(img_path)
                logger.warning('[VERIFICATION] Duplicate image found: %s', ("" if img_path is None else str(img_path).replace("\n", "").replace("\r", "")))
            else:
                used_images.add(img_path)
    
    # Check for missing images
    available_s3_keys = {img.get('s3_key', '') for img in available_images}
    for s3_key in available_s3_keys:
        if s3_key and s3_key not in used_images:
            missing_images.append(s3_key)
            logger.warning('[VERIFICATION] Missing image: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
    
    # Check for fake/invalid images
    fake_images = []
    for img_path in used_images:
        if img_path not in available_s3_keys:
            fake_images.append(img_path)
            logger.error('[VERIFICATION] Fake/invalid image found: %s', ("" if img_path is None else str(img_path).replace("\n", "").replace("\r", "")))
    
    # Calculate accuracy metrics
    total_available = len(available_s3_keys)
    total_used = len(used_images)
    total_duplicates = len(duplicate_images)
    total_missing = len(missing_images)
    total_fake = len(fake_images)
    
    accuracy_percentage = ((total_used - total_duplicates - total_fake) / total_available * 100) if total_available > 0 else 100
    
    verification_result = {
        'total_available_images': total_available,
        'total_used_images': total_used,
        'duplicate_images': duplicate_images,
        'missing_images': missing_images,
        'fake_images': fake_images,
        'accuracy_percentage': accuracy_percentage,
        # Missing images are informational only. The cleanup flow intentionally does
        # not force-insert them when the metadata confidence gate excludes them.
        'is_perfect_mapping': (total_duplicates == 0 and total_fake == 0)
    }
    
    # Log results
    logger.info("[VERIFICATION] Results:")
    logger.info('  - Available images: %s', ("" if total_available is None else str(total_available).replace("\n", "").replace("\r", "")))
    logger.info('  - Used images: %s', ("" if total_used is None else str(total_used).replace("\n", "").replace("\r", "")))
    logger.info('  - Duplicate images: %s', ("" if total_duplicates is None else str(total_duplicates).replace("\n", "").replace("\r", "")))
    logger.info('  - Missing images: %s', ("" if total_missing is None else str(total_missing).replace("\n", "").replace("\r", "")))
    logger.info('  - Fake images: %s', ("" if total_fake is None else str(total_fake).replace("\n", "").replace("\r", "")))
    logger.info('  - Accuracy: %s%', f"{accuracy_percentage:.1f}")
    logger.info('  - Perfect mapping: %s', ("" if verification_result['is_perfect_mapping'] is None else str(verification_result['is_perfect_mapping']).replace("\n", "").replace("\r", "")))
    
    return verification_result

def fix_image_mapping_issues(generated_modules: list, available_images: list, verification_result: dict) -> list:
    """
    Fix image mapping issues: remove duplicates and fake/invalid image paths.
    Images that were not mapped (missing) are intentionally left out — they
    failed the metadata confidence gate and should not be force-inserted.
    """
    if verification_result['is_perfect_mapping']:
        logger.info("[FIX] No fixes needed - mapping already clean")
        return generated_modules

    logger.info("[FIX] Cleaning image mapping issues")

    # Remove duplicate images across modules
    used_images = set()
    for module in generated_modules:
        content = module.get('edited_content', '')
        image_matches = list(re.finditer(r'\[IMAGE:\s*([^\]]+)\]', content))
        for match in reversed(image_matches):
            img_path = match.group(1).strip()
            if img_path in used_images:
                logger.info('[FIX] Removing duplicate image: %s', ("" if img_path is None else str(img_path).replace("\n", "").replace("\r", "")))
                content = content[:match.start()] + content[match.end():]
            else:
                used_images.add(img_path)
        module['edited_content'] = content

    # Remove fake/invalid image paths
    available_s3_keys = {img.get('s3_key', '') for img in available_images}
    for module in generated_modules:
        content = module.get('edited_content', '')
        for fake_img in verification_result.get('fake_images', []):
            content = re.sub(rf'\[IMAGE:\s*{re.escape(fake_img)}\s*\]', '', content)
            logger.info('[FIX] Removed fake image: %s', ("" if fake_img is None else str(fake_img).replace("\n", "").replace("\r", "")))
        module['edited_content'] = content

    # NOTE: Missing images are NOT re-inserted here.
    # If an image was not mapped by create_intelligent_image_mapping, it means
    # its metadata did not match any module content with sufficient confidence.
    # Force-inserting it would introduce irrelevant visuals.
    if verification_result.get('missing_images'):
        logger.info(
            "[FIX] %s image(s) intentionally excluded (metadata confidence gate): %s",
            len(verification_result["missing_images"]),
            ("" if [p.split("/")[-1] for p in verification_result["missing_images"]] is None else str([p.split("/")[-1] for p in verification_result["missing_images"]]).replace("\n", "").replace("\r", "")),
        )

    logger.info("[FIX] Image mapping cleanup completed")
    return generated_modules

def get_s3_signed_url(s3_key: str, expiration: int = 3600) -> Optional[str]:
    """
    Generate a signed URL for S3 object that works in both local and cloud environments.
    
    Args:
        s3_key: S3 object key
        expiration: URL expiration time in seconds (default: 1 hour)
    
    Returns:
        Signed URL string or None if error
    """
    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': S3_BUCKET_NAME,
                'Key': s3_key
            },
            ExpiresIn=expiration
        )
        logger.info('Generated signed URL for: %s (expires in %ss)', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), ("" if expiration is None else str(expiration).replace("\n", "").replace("\r", "")))
        return url
    except Exception as e:
        logger.error('Failed to generate signed URL for %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e)
        return None

def get_image_from_s3_as_bytes(s3_key: str) -> Optional[bytes]:
    """
    Download image from S3 and return as bytes (for vision analysis).
    
    Args:
        s3_key: S3 object key
    
    Returns:
        Image bytes or None if error
    """
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        image_bytes = response['Body'].read()
        logger.info('Downloaded image bytes from S3: %s (%s bytes)', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), len(image_bytes))
        return image_bytes
    except Exception as e:
        logger.error('Failed to download image bytes from S3 %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e)
        return None

def get_actual_images_from_s3(user_id: str, doc_name: str) -> List[Dict]:
    """
    Scan S3 to get the actual list of images that exist for a document.
    This ensures we only reference images that actually exist in S3.
    
    Args:
        user_id: User ID
        doc_name: Document name
    
    Returns:
        List of image dictionaries with s3_key, caption, page info
    """
    logger.info("[S3 SCAN] ===== SCANNING ACTUAL S3 IMAGES =====")
    logger.info('[S3 SCAN] User ID: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
    logger.info('[S3 SCAN] Document: %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
    
    actual_images = []
    
    try:
        # Scan the extracted_images folder for this document
        secure_doc_name = secure_filename(doc_name)
        s3_prefix = f"{user_id}/extracted_images/{secure_doc_name}/"
        
        logger.info('[S3 SCAN] Scanning S3 prefix: %s', ("" if s3_prefix is None else str(s3_prefix).replace("\n", "").replace("\r", "")))
        
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=s3_prefix)
        
        image_count = 0
        for page in pages:
            for obj in page.get('Contents', []):
                s3_key = obj['Key']
                
                # Only include actual image files
                if s3_key.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                    image_count += 1
                    
                    # Extract filename and create caption
                    filename = s3_key.split('/')[-1]
                    filename_without_ext = filename.rsplit('.', 1)[0]
                    
                    # Try to extract page number from filename
                    page_num = 1
                    if 'page' in filename_without_ext.lower():
                        try:
                            # Extract page number from patterns like "page1_img0" or "page4_img0"
                            import re
                            page_match = re.search(r'page(\d+)', filename_without_ext.lower())
                            if page_match:
                                page_num = int(page_match.group(1))
                        except:
                            page_num = 1
                    
                    # Create a readable caption from filename
                    caption = filename_without_ext.replace('_', ' ').replace('-', ' ')
                    if caption.startswith('page') and 'img' in caption:
                        # Convert "page4 img0" to "page4 img0"
                        caption = caption
                    elif 'Figure' in filename:
                        # Handle figure names
                        caption = filename_without_ext.replace('_', ' ').replace('__', ' - ')
                    
                    actual_images.append({
                        's3_key': s3_key,
                        'caption': caption,
                        'page_num': page_num,
                        'filename': filename
                    })
                    
                    logger.info('[S3 SCAN] Found ACTUAL image %s: %s', ("" if image_count is None else str(image_count).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
        
        logger.info("[S3 SCAN] ===== S3 SCAN COMPLETE =====")
        logger.info('[S3 SCAN] Found %s ACTUAL images in S3', len(actual_images))

        # Baseline sort: page hint from filename, then filename (S3 list order is arbitrary).
        actual_images.sort(
            key=lambda img: (
                int(img.get("page_num") or 9999),
                (img.get("filename") or img.get("s3_key", "").split("/")[-1] or "").lower(),
            )
        )

        return actual_images

    except Exception as e:
        logger.error('[S3 SCAN] Error scanning S3 for images: %s', e, exc_info=True)
        return []


# Shown under "--- AVAILABLE IMAGES ---" so models keep narrative aligned with source layout.
IMAGE_CATALOG_ORDER_INSTRUCTION = (
    "**IMAGE CATALOG ORDER:** Images are listed in **document reading order** "
    "(PDF page order, then the order each image was extracted on that page). "
    "The main document text may also contain the same `[IMAGE: ...]` lines **inline** right after the page they came from—treat that inline order as ground truth for sequencing. "
    "Place `[IMAGE: ...]` tags in that same sequence in your modules for a coherent flow. "
    "**Exception:** If the user's custom instructions explicitly require a different image sequence "
    "(e.g. thematic grouping, \"diagrams first\", reverse order), follow those instructions instead.\n\n"
)


def parse_image_filename_sort_keys(filename: str) -> Tuple[int, int, str]:
    """
    Best-effort (page_1based, index_on_page_or_figure, filename_lower) for tie-breaking
    when extraction metadata is missing (e.g. leftover S3 objects).
    """
    if not filename:
        return (999999, 999999, "")
    base = filename.rsplit(".", 1)[0].lower()
    page = 999999
    img_idx = 999999
    m = re.search(r"page\s*(\d+)", base, re.IGNORECASE)
    if m:
        page = int(m.group(1))
    m2 = re.search(r"(?:_|\b)img\s*(\d+)\b", base, re.IGNORECASE)
    if m2:
        img_idx = int(m2.group(1))
    m3 = re.search(r"figure\s*[_-]?\s*(\d+)", base, re.IGNORECASE)
    if m3 and img_idx == 999999:
        img_idx = int(m3.group(1))
    return (page, img_idx, filename.lower())


def order_images_for_module_catalog(
    extracted_images: Optional[List[Dict]],
    actual_s3_images: List[Dict],
) -> List[Dict]:
    """
    Order images for module prompts and mapping.

    Primary order follows PDF extraction (``extracted_images``): page flow, then extraction order.
    Any S3 object not present in ``extracted_images`` is appended, sorted by page hint and filename.
    """
    if not actual_s3_images:
        return []
    extracted_images = extracted_images or []
    by_key: Dict[str, Dict] = {}
    for img in actual_s3_images:
        key = img.get("s3_key")
        if key:
            by_key[key] = dict(img)

    ordered: List[Dict] = []
    seen: set = set()

    for ex in extracted_images:
        key = ex.get("s3_key")
        if not key or key in seen or key not in by_key:
            continue
        row = dict(by_key[key])
        ex_page = ex.get("page_num")
        if ex_page is not None:
            try:
                row["page_num"] = int(ex_page)
            except (TypeError, ValueError):
                pass
        if ex.get("extraction_index") is not None:
            row["extraction_index"] = ex["extraction_index"]
        if ex.get("caption"):
            row["caption"] = ex["caption"]
        if ex.get("ai_analysis"):
            row["ai_analysis"] = ex["ai_analysis"]
        ordered.append(row)
        seen.add(key)

    remainder = [dict(img) for img in actual_s3_images if img.get("s3_key") and img["s3_key"] not in seen]

    def remainder_sort_key(img: Dict) -> Tuple[int, int, int, str]:
        fn = img.get("filename") or (img["s3_key"].split("/")[-1] if img.get("s3_key") else "")
        p_pat, idx_pat, fn_low = parse_image_filename_sort_keys(fn)
        try:
            page_meta = int(img.get("page_num") or 999999)
        except (TypeError, ValueError):
            page_meta = 999999
        page_use = p_pat if p_pat != 999999 else page_meta
        idx_use = idx_pat if idx_pat != 999999 else 999999
        try:
            ext_idx = int(img.get("extraction_index", 999999))
        except (TypeError, ValueError):
            ext_idx = 999999
        return (page_use, idx_use, ext_idx, fn_low)

    remainder.sort(key=remainder_sort_key)
    ordered.extend(remainder)
    logger.info(
        f"[IMAGE ORDER] Catalog: {len(ordered)} image(s) "
        f"({len(extracted_images)} extraction ref(s), {len(remainder)} S3-only)"
    )
    return ordered


def reading_order_key_from_segment_image(img_info: dict) -> Tuple[int, int, str]:
    """Sort key (page, reading_order, s3_key) for images assigned to a module segment."""
    analysis = img_info.get("analysis") or {}
    page = analysis.get("document_page", analysis.get("page_num"))
    try:
        page_i = int(page)
    except (TypeError, ValueError):
        page_i = 999999
    ro = analysis.get("reading_order")
    try:
        ro_i = int(ro)
    except (TypeError, ValueError):
        ro_i = 999999
    return (page_i, ro_i, (img_info.get("s3_key") or ""))


def segment_index_for_document_page(page: int, total_pages: int, num_segments: int) -> int:
    """Map 1-based page number to a segment index (0..N-1) with even spread across the document."""
    if num_segments <= 1:
        return 0
    try:
        tp = int(total_pages)
    except (TypeError, ValueError):
        tp = 1
    tp = max(tp, 1)
    try:
        p = int(page)
    except (TypeError, ValueError):
        p = 1
    p = max(1, min(p, tp))
    idx = (p - 1) * num_segments // tp
    return min(max(idx, 0), num_segments - 1)


def _effective_page_from_image_analysis(img: dict) -> int:
    """Best-effort 1-based page from AI metadata, filename patterns, or slide hints."""
    for key in ("document_page", "page_num"):
        v = img.get(key)
        if v is not None:
            try:
                pi = int(v)
                if pi >= 1:
                    return pi
            except (TypeError, ValueError):
                continue
    fn = (img.get("s3_key") or "").split("/")[-1]
    page_guess, _, _ = parse_image_filename_sort_keys(fn)
    if page_guess != 999999:
        return page_guess
    m = re.search(r"slide[_\s-]?(\d+)", fn, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 1


def merge_extracted_with_catalog_order(
    extracted_images: List[Dict], catalog_rows: List[Dict]
) -> List[Dict]:
    """
    Rows in strict catalog (S3) order with AI fields merged from extraction records.
    Ensures reading_order / mapping uses the same sequence as ``--- AVAILABLE IMAGES ---``.
    """
    by_key: Dict[str, Dict] = {}
    for ex in extracted_images or []:
        k = ex.get("s3_key")
        if k:
            by_key[k] = dict(ex)
    merged: List[Dict] = []
    for row in catalog_rows or []:
        k = row.get("s3_key")
        if not k:
            continue
        base = dict(by_key.get(k, {}))
        base.update(dict(row))
        merged.append(base)
    return merged


_IMAGE_TAG_PATTERN = re.compile(r"\[IMAGE:\s*([^\]]+)\]")


def _image_metadata_tail_end(content: str, after_tag_end: int) -> int:
    """
    Exclusive end index for metadata tied to the [IMAGE:] tag ending at ``after_tag_end``.
    Stops at the next image tag or a markdown heading that begins a new section.
    """
    rest = content[after_tag_end:]
    if not rest:
        return after_tag_end
    next_img = re.search(r"\n\[IMAGE:\s*", rest)
    limit = next_img.start() if next_img else len(rest)
    chunk = rest[:limit]
    heading = re.search(r"\n(?=#{1,6}\s+\S)", chunk)
    if heading:
        limit = heading.start()
    return after_tag_end + limit


def _split_content_into_text_and_image_blocks(content: str) -> List[Tuple]:
    """
    Split module markdown into alternating text and image blocks.
    Each image block is the [IMAGE: ...] tag plus metadata that immediately follows it.
    """
    matches = list(_IMAGE_TAG_PATTERN.finditer(content))
    if not matches:
        return [("text", content)] if content else []

    segments: List[Tuple] = []
    pos = 0
    for i, m in enumerate(matches):
        if m.start() > pos:
            segments.append(("text", content[pos : m.start()]))
        key = m.group(1).strip()
        block_end = _image_metadata_tail_end(content, m.end())
        if i + 1 < len(matches):
            block_end = min(block_end, matches[i + 1].start())
        segments.append(("image", key, content[m.start() : block_end]))
        pos = block_end
    if pos < len(content):
        segments.append(("text", content[pos:]))
    return segments


def _format_catalog_metadata_below_image(row: Dict) -> str:
    """Learner-visible caption/description for the image keyed in the row."""
    ia = row.get("ai_analysis") if isinstance(row.get("ai_analysis"), dict) else {}
    cap = (ia.get("caption") or row.get("caption") or "").strip()
    parts: List[str] = []
    if cap:
        parts.append(f"*{cap}*")
    purpose = (ia.get("purpose") or "").strip()
    if purpose:
        parts.append(purpose)
    elif ia.get("content"):
        body = str(ia.get("content", "")).strip()
        if body and body.lower() != cap.lower():
            parts.append(body[:600] + ("..." if len(body) > 600 else ""))
    analysis_block = _format_image_analysis_markdown_block(ia) if ia else ""
    if analysis_block and analysis_block.strip() not in "\n".join(parts):
        parts.append(analysis_block.strip())
    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts) + "\n"


def sync_image_metadata_blocks_from_catalog(content: str, ordered_catalog: List[Dict]) -> str:
    """
    Ensure text directly under each [IMAGE: key] describes that key (from extraction/AI catalog).
    """
    if not content or not ordered_catalog:
        return content
    key_to_row = {r["s3_key"]: r for r in ordered_catalog if r.get("s3_key")}
    out = content
    for m in reversed(list(_IMAGE_TAG_PATTERN.finditer(out))):
        key = m.group(1).strip()
        row = key_to_row.get(key)
        if not row:
            continue
        meta = _format_catalog_metadata_below_image(row)
        if not meta:
            continue
        tail_start = m.end()
        tail_end = _image_metadata_tail_end(out, tail_start)
        out = out[:tail_start] + meta + out[tail_end:]
    return out


def reorder_image_tags_in_content_by_catalog(
    content: str,
    catalog_keys_ordered: List[str],
    ordered_catalog: Optional[List[Dict]] = None,
) -> str:
    """
    Re-order image blocks (tag + attached metadata) to follow ``catalog_keys_ordered``.

    Moves each figure and its caption/analysis together so metadata is not left under the
    wrong image after reordering (PPT/DOCX/PDF document reading order).
    """
    if not content or not catalog_keys_ordered:
        return content
    segments = _split_content_into_text_and_image_blocks(content)
    image_segs = [s for s in segments if s[0] == "image"]
    if not image_segs:
        return content

    rank = {k: i for i, k in enumerate(catalog_keys_ordered)}

    def block_sort_key(seg: Tuple) -> Tuple[int, str]:
        return (rank.get(seg[1], 10**9), seg[1])

    sorted_blocks = sorted(image_segs, key=block_sort_key)
    keys_in_file = [s[1] for s in image_segs]
    sorted_keys = [s[1] for s in sorted_blocks]

    if keys_in_file == sorted_keys:
        result = content
    else:
        out_parts: List[str] = []
        block_iter = iter(sorted_blocks)
        for seg in segments:
            if seg[0] == "text":
                out_parts.append(seg[1])
            else:
                _, key, block_text = next(block_iter)
                new_tag = f"[IMAGE: {key}]"
                block_text = _IMAGE_TAG_PATTERN.sub(new_tag, block_text, count=1)
                out_parts.append(block_text)
        result = "".join(out_parts)
        logger.info(
            f"[IMAGE ORDER] Reordered {len(image_segs)} image block(s) to document catalog order"
        )

    if ordered_catalog:
        result = sync_image_metadata_blocks_from_catalog(result, ordered_catalog)
    return result


def _catalog_keys_for_custom_instruction(
    ordered_catalog: List[Dict], user_instruction: Optional[str]
) -> List[str]:
    keys = [r["s3_key"] for r in (ordered_catalog or []) if r.get("s3_key")]
    if not keys or not (user_instruction or "").strip():
        return keys
    ui = user_instruction.lower()
    if "reverse" in ui and any(
        w in ui for w in ("image", "figure", "diagram", "illustration", "visual", "picture")
    ):
        return list(reversed(keys))
    return keys


def _module_text_for_image_scoring(mod: Dict) -> str:
    title = (mod.get("title") or "").strip()
    body = mod.get("edited_content") or mod.get("content") or ""
    # Weight title twice so protocol/topic names in titles drive mapping.
    return f"{title} {title} {body[:8000]}".lower()


def _image_vocab_from_catalog_row(row: Dict) -> set:
    tokens: set = set()
    ia = row.get("ai_analysis") if isinstance(row.get("ai_analysis"), dict) else {}
    for field in ("topics", "keywords", "entities", "labels", "objects"):
        v = ia.get(field)
        if isinstance(v, list):
            for x in v:
                tokens.update(re.findall(r"[a-z0-9][a-z0-9\-]{1,}", str(x).lower()))
        elif isinstance(v, str) and v.strip():
            tokens.update(re.findall(r"[a-z0-9][a-z0-9\-]{1,}", v.lower()))
    for field in ("caption", "content_description", "summary", "content", "description"):
        s = ia.get(field) if isinstance(ia, dict) else None
        if not s:
            s = row.get("caption") or ""
        tokens.update(re.findall(r"[a-z0-9][a-z0-9\-]{1,}", str(s).lower()))
    fn = (row.get("s3_key") or "").split("/")[-1]
    tokens.update(re.findall(r"[a-z0-9][a-z0-9\-]{1,}", fn.lower()))
    return {t for t in tokens if len(t) > 1}


def _semantic_image_module_score(mod: Dict, row: Dict) -> int:
    blob = _module_text_for_image_scoring(mod)
    score = 0
    for tok in _image_vocab_from_catalog_row(row):
        if len(tok) < 2:
            continue
        if tok in blob:
            score += 3 if len(tok) > 4 else 2
    fn = (row.get("s3_key") or "").split("/")[-1]
    stem = fn.rsplit(".", 1)[0].lower() if fn else ""
    if len(stem) > 2 and stem in blob:
        score += 2
    return score


def _strip_image_tag(text: str, s3_key: str) -> str:
    if not text:
        return text
    text = re.sub(rf"\[IMAGE:\s*{re.escape(s3_key)}\s*\]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _module_slice_until_next_image(content: str, img_end: int) -> str:
    """Text after an [IMAGE:] tag until the next image tag or a reasonable horizon."""
    rest = content[img_end:]
    next_img = re.search(r"\n\[IMAGE:\s*", rest)
    if next_img:
        rest = rest[: next_img.start()]
    return rest[:900]


def _module_already_has_image_analysis_block_after(content: str, img_end: int) -> bool:
    window = _module_slice_until_next_image(content, img_end)
    return bool(re.search(r"(?i)\*\*image analysis\*\*", window))


def _module_has_substantial_prose_after_image(content: str, img_end: int, min_words: int = 32) -> bool:
    """
    True if the model already placed a real paragraph after the image (default REPORT_AGENT path).
    Avoid duplicating that with an injected catalog block.
    """
    chunk = _module_slice_until_next_image(content, img_end)
    lines = []
    for ln in chunk.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("|") and s.endswith("|"):
            continue
        lines.append(s)
    text = " ".join(lines)
    words = re.findall(r"\w+", text)
    return len(words) >= min_words


def _format_image_analysis_markdown_block(ia: dict) -> str:
    """Build learner-visible markdown from catalog / cached image analysis dict."""
    if not ia or _is_irrelevant_image(ia):
        return ""
    parts: List[str] = []
    cap = (ia.get("caption") or "").strip()
    if cap:
        parts.append(f"**Caption:** {cap}")
    typ = (ia.get("type") or "").strip()
    if typ:
        parts.append(f"**Type:** {typ}")
    kws = ia.get("keywords") or []
    if kws:
        parts.append("**Keywords:** " + ", ".join(str(x) for x in kws[:30]))
    tops = ia.get("topics") or []
    if tops:
        parts.append("**Topics:** " + ", ".join(str(x) for x in tops[:30]))
    body = (ia.get("content") or "").strip()
    if body:
        parts.append(body)
    if not parts:
        return ""
    return "\n\n**Image analysis**\n\n" + "\n\n".join(parts) + "\n"


def inject_image_analysis_below_image_tags(
    content: str,
    ordered_catalog: List[Dict],
    cached_analyses_by_s3_key: Optional[Dict[str, dict]] = None,
) -> str:
    """
    Custom-instruction modules often place [IMAGE: ...] in '(A) data-only' sections with no
    narrative under the figure. Default pipeline modules get prose from REPORT_AGENT.
    Inject the same AI metadata we already store (catalog / S3 cache) under each image
    when there is no analysis block and little prose after the tag.
    """
    if not content or "[IMAGE:" not in content:
        return content

    key_to_row = {r["s3_key"]: r for r in (ordered_catalog or []) if r.get("s3_key")}
    cache = cached_analyses_by_s3_key or {}

    def analysis_dict_for_key(s3_key: str) -> dict:
        row = key_to_row.get(s3_key) or {}
        ia = row.get("ai_analysis") if isinstance(row.get("ai_analysis"), dict) else {}
        if ia:
            return dict(ia)
        cached = cache.get(s3_key)
        return dict(cached) if isinstance(cached, dict) else {}

    out = content
    for m in reversed(list(re.finditer(r"\[IMAGE:\s*([^\]]+)\]", out))):
        s3_key = m.group(1).strip()
        img_end = m.end()
        if _module_already_has_image_analysis_block_after(out, img_end):
            continue
        if _module_has_substantial_prose_after_image(out, img_end):
            continue
        block = _format_image_analysis_markdown_block(analysis_dict_for_key(s3_key))
        if not block:
            continue
        out = out[:img_end] + block + out[img_end:]
        logger.info('[CUSTOM INSTRUCTION] Injected image analysis below [IMAGE: ...] for %s', ("" if s3_key.split('/')[-1] is None else str(s3_key.split('/')[-1]).replace("\n", "").replace("\r", "")))
    return out


def rebalance_custom_instruction_modules_images(
    modules: List[Dict],
    ordered_catalog: List[Dict],
    user_instruction: Optional[str],
) -> List[Dict]:
    """
    After JSON custom-instruction generation, align catalog images with modules without
    destroying the model's topic-based placement. Preserves existing [IMAGE:] tags when
    valid; resolves duplicates by semantic match to module titles/content; inserts only
    missing keys using the same scoring, with page-based spread as a fallback.
    """
    if not modules or not ordered_catalog:
        return modules
    catalog_keys = _catalog_keys_for_custom_instruction(ordered_catalog, user_instruction)
    if not catalog_keys:
        return modules

    num_mod = len(modules)
    pages = [_effective_page_from_image_analysis(dict(row)) for row in ordered_catalog if row.get("s3_key")]
    total_pages = max(pages) if pages else 1
    meaningful_pages = total_pages > 1 or any(p > 1 for p in pages)
    key_to_row = {r["s3_key"]: r for r in ordered_catalog if r.get("s3_key")}
    n_img = len(catalog_keys)
    img_pat = re.compile(r"\[IMAGE:\s*([^\]]+)\]")

    def page_fallback_index(s3_key: str, idx_in_catalog: int) -> int:
        row = key_to_row.get(s3_key) or {}
        ia = row.get("ai_analysis") if isinstance(row.get("ai_analysis"), dict) else {}
        merged = dict(ia) if ia else {}
        merged.setdefault("s3_key", s3_key)
        if row.get("page_num") is not None:
            merged["page_num"] = row["page_num"]
        page = _effective_page_from_image_analysis(merged)
        if num_mod <= 1:
            return 0
        if meaningful_pages:
            return segment_index_for_document_page(page, total_pages, num_mod)
        return min(num_mod - 1, idx_in_catalog * num_mod // max(n_img, 1))

    def best_module_index_for_row(row: Dict, idx_in_catalog: int) -> int:
        best_i, best_s = 0, -1
        for mi, mod in enumerate(modules):
            s = _semantic_image_module_score(mod, row)
            if s > best_s:
                best_s, best_i = s, mi
        if best_s <= 0:
            k = row.get("s3_key") or ""
            return page_fallback_index(k, idx_in_catalog)
        return best_i

    # Ensure edited_content exists (JSON path uses edited_content)
    for mod in modules:
        if mod.get("edited_content") is None and mod.get("content") is not None:
            mod["edited_content"] = mod["content"]

    # Map each catalog key -> list of (module_index, semantic_score) where it appears
    placements: Dict[str, List[Tuple[int, int]]] = {k: [] for k in catalog_keys}
    for mi, mod in enumerate(modules):
        text = mod.get("edited_content") or ""
        for m in img_pat.finditer(text):
            raw_key = m.group(1).strip()
            if raw_key in placements:
                row = key_to_row.get(raw_key) or {}
                sc = _semantic_image_module_score(mod, row)
                placements[raw_key].append((mi, sc))

    # Resolve duplicates: keep the module with highest semantic score (tie: lower index)
    for key, plist in placements.items():
        if len(plist) <= 1:
            continue
        plist.sort(key=lambda t: (-t[1], t[0]))
        keep_mi = plist[0][0]
        for mi, _ in plist[1:]:
            modules[mi]["edited_content"] = _strip_image_tag(modules[mi].get("edited_content") or "", key)

    # Insert any catalog image that is still missing from all modules
    for i, k in enumerate(catalog_keys):
        row = key_to_row.get(k) or {}
        any_present = False
        for mod in modules:
            if k in (mod.get("edited_content") or ""):
                any_present = True
                break
        if any_present:
            continue
        ti = best_module_index_for_row(row, i)
        if 0 <= ti < len(modules):
            block = f"\n\n[IMAGE: {k}]\n"
            cur = modules[ti].get("edited_content") or ""
            modules[ti]["edited_content"] = (cur.rstrip() + block).strip()

    for mod in modules:
        mod["edited_content"] = reorder_image_tags_in_content_by_catalog(
            mod.get("edited_content") or "",
            catalog_keys,
            ordered_catalog,
        )
    return modules


def fix_inverted_image_and_heading_blocks(content: str) -> str:
    """
    If an [IMAGE:] block is immediately followed by a markdown heading (# ...),
    move the heading above the image so layout matches source documents
    (heading introduces the figure).
    """
    if not content or "[IMAGE:" not in content:
        return content
    # Non-greedy heading run: one or more consecutive lines that are markdown headings
    pattern = re.compile(
        r"(?P<img>\[IMAGE:\s*[^\]]+\])\s*\n+"
        r"(?P<head>(?:^[ \t]*#{1,6}\s[^\n]+\n)+)",
        re.MULTILINE,
    )

    def repl(m: re.Match) -> str:
        img = m.group("img").strip()
        head = m.group("head").rstrip() + "\n\n"
        return head + img + "\n"

    prev = None
    out = content
    while prev != out:
        prev = out
        out = pattern.sub(repl, out)
    return out


def download_image_to_temp(s3_key: str, module_id: str) -> Optional[str]:
    """
    LEGACY FUNCTION: Download image from S3 to temp folder for preview feature.
    
    Note: This is kept for backward compatibility with the preview feature.
    For tutor, use get_s3_signed_url() instead.
    
    Args:
        s3_key: S3 object key
        module_id: Module ID for logging
    
    Returns:
        Local file path or None if error
    """
    try:
        import hashlib
        
        # Create hash-based filename to prevent duplicates (non-crypto fingerprint only)
        hash_obj = hashlib.md5(s3_key.encode(), usedforsecurity=False)
        hash_str = hash_obj.hexdigest()[:12]
        
        # Get original extension
        original_ext = Path(s3_key).suffix
        
        # Create unique filename
        filename = f"{hash_str}{original_ext}"
        local_path = TEMP_IMAGE_DIR / filename
        
        # Skip if already downloaded
        if local_path.exists():
            logger.info('Image already in temp: %s', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")))
            return str(local_path)
        
        # Download from S3
        s3_client.download_file(S3_BUCKET_NAME, s3_key, str(local_path))
        logger.info('Downloaded image to temp: %s (from %s)', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
        
        return str(local_path)
    except Exception as e:
        logger.error('Failed to download image %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e)
        return None

def resize_image_if_needed(image_bytes: bytes, max_dimension: int = 7900) -> bytes:
    """
    Resize image if any dimension exceeds max_dimension while maintaining aspect ratio.
    
    Args:
        image_bytes: Original image bytes
        max_dimension: Maximum allowed dimension (default 7900 for Bedrock safety margin)
        
    Returns:
        Resized image bytes (or original if no resize needed)
    """
    if not PIL_AVAILABLE:
        logger.error("[RESIZE ERROR] PIL/Pillow not available - cannot resize image")
        raise Exception("PIL/Pillow not installed - cannot resize large images")
    
    try:
        import io
        
        # Open image and get dimensions
        img = Image.open(io.BytesIO(image_bytes))
        width, height = img.size
        
        logger.info('[RESIZE CHECK] Original image dimensions: %sx%s', ("" if width is None else str(width).replace("\n", "").replace("\r", "")), ("" if height is None else str(height).replace("\n", "").replace("\r", "")))
        
        # Check if resize is needed
        if width <= max_dimension and height <= max_dimension:
            logger.info('[RESIZE CHECK] Image size OK: %sx%s (no resize needed)', ("" if width is None else str(width).replace("\n", "").replace("\r", "")), ("" if height is None else str(height).replace("\n", "").replace("\r", "")))
            return image_bytes
        
        # Calculate new dimensions maintaining aspect ratio
        if width > height:
            new_width = max_dimension
            new_height = int((max_dimension / width) * height)
        else:
            new_height = max_dimension
            new_width = int((max_dimension / height) * width)
        
        logger.warning('[RESIZE REQUIRED] Resizing image from %sx%s to %sx%s', ("" if width is None else str(width).replace("\n", "").replace("\r", "")), ("" if height is None else str(height).replace("\n", "").replace("\r", "")), ("" if new_width is None else str(new_width).replace("\n", "").replace("\r", "")), ("" if new_height is None else str(new_height).replace("\n", "").replace("\r", "")))
        
        # Convert to RGB if necessary (handles RGBA, P, etc.)
        if img.mode not in ('RGB', 'L'):
            logger.info('[RESIZE] Converting image from %s to RGB', ("" if img.mode is None else str(img.mode).replace("\n", "").replace("\r", "")))
            img = img.convert('RGB')
        
        # Resize image
        img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Save to bytes as PNG (most compatible)
        output = io.BytesIO()
        img_resized.save(output, format='PNG', optimize=True)
        output.seek(0)
        
        resized_bytes = output.read()
        logger.info('[RESIZE SUCCESS] Image resized: %s bytes -> %s bytes', len(image_bytes), len(resized_bytes))
        
        # Verify the resized image dimensions
        verify_img = Image.open(io.BytesIO(resized_bytes))
        verify_width, verify_height = verify_img.size
        logger.info('[RESIZE VERIFY] Final dimensions: %sx%s', ("" if verify_width is None else str(verify_width).replace("\n", "").replace("\r", "")), ("" if verify_height is None else str(verify_height).replace("\n", "").replace("\r", "")))
        
        return resized_bytes
        
    except Exception as e:
        logger.error('[RESIZE ERROR] Failed to resize image: %s', e, exc_info=True)
        # DO NOT return original - raise exception instead
        raise Exception(f"Image resize failed: {e}")

def extract_text_from_image_with_bedrock(image_bytes: bytes, is_pdf_extraction: bool = False) -> str:
    """
    Extracts text and detailed analysis from an image using AWS Bedrock's multimodal AI model.
    
    This function uses Amazon Nova Pro to perform image analysis with context-aware prompting:
    - For PDF extraction: Uses concise prompt for faster processing
    - For standalone images: Uses comprehensive prompt for detailed analysis
    
    Args:
        image_bytes (bytes): Raw image data in bytes format (PNG, JPEG, etc.)
        is_pdf_extraction (bool): If True, uses shorter prompt for faster PDF processing.
                                   If False, uses detailed prompt for standalone image uploads.
        
    Returns:
        str: Text analysis combining extracted text and visual description.
             Returns empty string if extraction fails.
             
    Example:
        >>> # For PDF extraction (fast)
        >>> text = extract_text_from_image_with_bedrock(image_data, is_pdf_extraction=True)
        >>> # For standalone image upload (detailed)
        >>> text = extract_text_from_image_with_bedrock(image_data, is_pdf_extraction=False)
    """
    try:
        # CRITICAL FIX: Resize image if dimensions exceed Bedrock's limit (8000 pixels)
        try:
            image_bytes = resize_image_if_needed(image_bytes, max_dimension=7900)  # Use 7900 to be safe
        except Exception as resize_error:
            logger.error('Image resize failed, cannot process: %s', ("" if resize_error is None else str(resize_error).replace("\n", "").replace("\r", "")))
            return ""
        
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        if is_pdf_extraction:
            # Concise prompt for PDF image extraction (faster processing, avoids timeouts)
            enhanced_prompt = """Extract all visible text from this image verbatim. Then briefly describe what the image shows (diagram type, main components, purpose). Keep your response concise and focused. Format: 
TEXT: [extracted text]
DESCRIPTION: [brief 2-3 sentence description]"""
        else:
            # Comprehensive prompt for standalone image uploads (detailed analysis)
            enhanced_prompt = """You are an expert AI assistant specializing in visual content analysis for educational purposes. 

Analyze this image comprehensively:

1. **TEXT EXTRACTION**: Extract ALL visible text verbatim
2. **VISUAL TYPE**: Identify the type (diagram, chart, graph, etc.)
3. **COMPONENTS**: Describe main elements and their relationships
4. **PURPOSE**: Explain what concept or process is illustrated
5. **KEY INSIGHTS**: Highlight important learning points

Provide a clear, educational analysis suitable for learners."""
        
        # Payload for Amazon Nova Pro on Bedrock
        model_id = BEDROCK_IMAGE_MODEL
        context = "PDF extraction" if is_pdf_extraction else "standalone image"
        logger.info(
            'Invoking Bedrock model %s for image analysis (%s)',
            log_safe(model_id),
            log_safe(context),
        )

        extracted_text, response_body = invoke_nova_pro_vision(
            sync_bedrock_client,
            image_bytes=image_bytes,
            prompt=enhanced_prompt,
            image_format="png",
            model_id=model_id,
            max_tokens=2048 if is_pdf_extraction else 4096,
            temperature=0.3,
        )
        try:
            from services.token_tracking import record_vision_tokens_from_response

            record_vision_tokens_from_response(response_body)
        except Exception as vision_track_exc:
            logger.debug("Vision token tracking skipped: %s", log_safe(vision_track_exc))
        extracted_text = (extracted_text or "").strip()

        if not extracted_text:
            logger.warning('Model %s returned no text content from the image.', log_safe(model_id))
            return ""
        
        logger.info(
            'Successfully extracted %s characters from image using %s (%s)',
            len(extracted_text),
            log_safe(model_id),
            log_safe(context),
        )
        return extracted_text

    except BedrockClientError as exc:
        logger.error('Bedrock API error with model %s: %s', log_safe(BEDROCK_IMAGE_MODEL), log_safe(exc), exc_info=True)
        return ""
    except Exception as exc:
        logger.error('Unexpected error in image extraction with %s: %s', log_safe(BEDROCK_IMAGE_MODEL), log_safe(exc), exc_info=True)
        return ""


def _find_caption_for_image(page: fitz.Page, img_bbox: fitz.Rect) -> Optional[str]:
    """Heuristically finds text below an image to use as a caption."""
    try:
        text_blocks = page.get_text("blocks")
        for tb in text_blocks:
            is_below = tb[1] > img_bbox.y1
            is_horizontally_aligned = (max(img_bbox.x0, tb[0]) < min(img_bbox.x1, tb[2]))
            vertical_distance = tb[1] - img_bbox.y1
            
            if is_below and is_horizontally_aligned and vertical_distance < 50:
                caption_text = tb[4].strip()
                if re.match(r'^(figure|fig|image|chart)\s*\d*', caption_text, re.IGNORECASE):
                    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', caption_text)
                    return sanitized[:100]
    except Exception as e:
        logger.warning('Error while trying to find image caption: %s', e)
    return None


# Patterns for watermarks / footnotes to ignore when extracting text from PDFs
_FOOTNOTE_PATTERNS = re.compile(
    r'made\s+with\s+gamma|gamma\.app|created\s+with\s+gamma|'
    r'powered\s+by\s+gamma|gamma\s+presentation|'
    r'made\s+with\s+canva|canva\.com|'
    r'made\s+with\s+beautiful\.ai|beautiful\.ai|'
    r'slide\s+\d+\s+of\s+\d+|page\s+\d+\s+of\s+\d+|'
    r'confidential|all\s+rights\s+reserved|'
    r'©\s*\d{4}|copyright\s+\d{4}',
    re.IGNORECASE
)

def _is_footnote_text(text: str) -> bool:
    """Return True if the text block is a watermark / footnote that should be ignored."""
    t = text.strip()
    if not t:
        return True
    if len(t) < 3:
        return True
    return bool(_FOOTNOTE_PATTERNS.search(t))


# Keywords in AI analysis that indicate a watermark / decorative / irrelevant image
_IRRELEVANT_IMAGE_PATTERNS = re.compile(
    r'made\s+with\s+gamma|gamma\s+logo|gamma\.app|'
    r'made\s+with\s+canva|canva\s+logo|'
    r'watermark|brand\s+logo|company\s+logo|'
    r'decorative\s+(element|image|graphic|icon)|'
    r'background\s+(pattern|texture|gradient)|'
    r'abstract\s+(shape|design|art|illustration)|'
    r'geometric\s+(shape|pattern|design)|'
    r'placeholder|clip\s*art|stock\s+(photo|image)|'
    r'no\s+relevant\s+content|not\s+relevant|irrelevant|'
    r'purely\s+decorative|ornamental',
    re.IGNORECASE
)

def _is_irrelevant_image(analysis: dict) -> bool:
    """
    Return True if the AI analysis indicates the image is a watermark,
    logo, decorative element, or otherwise adds no educational value.
    """
    if not analysis:
        return False

    # Check caption, content description, and type fields
    check_fields = [
        analysis.get('caption', ''),
        analysis.get('content', ''),
        analysis.get('type', ''),
        analysis.get('purpose', ''),
    ]
    combined = ' '.join(str(f) for f in check_fields if f)

    if _IRRELEVANT_IMAGE_PATTERNS.search(combined):
        return True

    # Also discard if the image type is purely decorative
    img_type = str(analysis.get('type', '')).lower()
    if img_type in ('logo', 'watermark', 'icon', 'decoration', 'background', 'abstract'):
        return True

    # Discard if caption explicitly mentions Gamma/Canva
    caption = str(analysis.get('caption', '')).lower()
    if any(kw in caption for kw in ('gamma', 'canva', 'watermark', 'made with', 'powered by')):
        return True

    return False


def _extract_vector_diagrams_from_page(
    page: fitz.Page,
    page_num: int,
    zoom: float = 2.0,
) -> list:
    """
    Extract Gamma-style vector diagrams from a PDF page.
    Groups drawing primitives into clusters, absorbs nearby text labels,
    and renders each cluster as a PNG image bytes object.
    Returns a list of dicts: {image_bytes, bbox, caption_hint}
    """
    results = []
    try:
        drawings = page.get_drawings()
        if not drawings:
            return results

        page_area = page.rect.get_area()
        matrix = fitz.Matrix(zoom, zoom)

        # Collect candidate rects — skip full-page backgrounds and tiny dots
        diagram_rects = []
        for d in drawings:
            rect = fitz.Rect(d["rect"])
            area = rect.get_area()
            if area > page_area * 0.6 or area < 50:
                continue
            diagram_rects.append(rect)

        if not diagram_rects:
            return results

        # Merge overlapping / touching rects
        changed = True
        while changed:
            changed = False
            for i in range(len(diagram_rects)):
                for j in range(i + 1, len(diagram_rects)):
                    expanded = diagram_rects[i] + fitz.Rect(-20, -20, 20, 20)
                    if expanded.intersects(diagram_rects[j]):
                        diagram_rects[i].include_rect(diagram_rects[j])
                        del diagram_rects[j]
                        changed = True
                        break
                if changed:
                    break

        # Gather text blocks — ignore header/footer zones and footnotes
        text_blocks = []
        for b in page.get_text("blocks"):
            txt = b[4].strip() if len(b) > 4 else ""
            if _is_footnote_text(txt):
                continue
            # Skip header (top 8%) and footer (bottom 8%)
            if b[1] < page.rect.height * 0.08 or b[3] > page.rect.height * 0.92:
                continue
            text_blocks.append(fitz.Rect(b[:4]))

        # Absorb nearby text into each diagram cluster
        for i in range(len(diagram_rects)):
            cluster = diagram_rects[i]
            absorbed = True
            while absorbed:
                absorbed = False
                catch_zone = cluster + fitz.Rect(-60, -20, 60, 20)
                remaining = []
                for t_rect in text_blocks:
                    if catch_zone.intersects(t_rect):
                        cluster.include_rect(t_rect)
                        absorbed = True
                    else:
                        remaining.append(t_rect)
                text_blocks = remaining
            diagram_rects[i] = cluster

        # Render each cluster
        for idx, region in enumerate(diagram_rects):
            region = region + fitz.Rect(-15, -15, 15, 15)  # padding
            region = region.intersect(page.rect)
            if region.width < 60 or region.height < 60:
                continue
            try:
                pix = page.get_pixmap(matrix=matrix, clip=region)
                img_bytes = pix.tobytes("png")
                if len(img_bytes) < 2000:  # skip trivially small renders
                    continue
                results.append({
                    "image_bytes": img_bytes,
                    "image_ext":   "png",
                    "bbox":        region,
                    "caption_hint": f"diagram_p{page_num + 1}_{idx + 1}",
                })
            except Exception as e:
                logger.warning('[VECTOR DIAGRAM] Failed to render region on page %s: %s', ("" if page_num + 1 is None else str(page_num + 1).replace("\n", "").replace("\r", "")), e)

    except Exception as e:
        logger.warning('[VECTOR DIAGRAM] Error on page %s: %s', ("" if page_num + 1 is None else str(page_num + 1).replace("\n", "").replace("\r", "")), e)

    return results


def _slide_snapshot_zoom(snapshot_kind: str = "slide") -> float:
    """Lower zoom on Linux/Docker to reduce memory use during PPT slide capture."""
    import sys

    if snapshot_kind == "slide" and sys.platform != "win32":
        return 2.0
    return 3.0 if snapshot_kind == "slide" else 2.0


def _render_pdf_page_snapshot_png(page: fitz.Page, zoom: float = 2.0) -> bytes:
    """Render a full PDF page to PNG bytes (one slide/page layout snapshot)."""
    for attempt_zoom in (zoom, min(zoom, 2.0), 1.5):
        try:
            matrix = fitz.Matrix(attempt_zoom, attempt_zoom)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            return pix.tobytes("png")
        except Exception as exc:
            logger.warning(
                "Page snapshot render failed at zoom %s: %s",
                attempt_zoom,
                log_safe(exc),
            )
    raise RuntimeError("Could not render page snapshot")


def _upload_layout_snapshot_image(
    image_bytes: bytes,
    page_label: int,
    doc_name: str,
    user_id: str,
    context_text: str,
    extracted_images: list,
    snapshot_kind: str = "slide",
    caption_hint_suffix: Optional[str] = None,
) -> str:
    """Store a slide/page PNG in S3 and return [IMAGE:...] markers for module text."""
    if len(image_bytes) < 1000:
        return ""

    default_caption = f"{snapshot_kind.title()} {page_label} layout snapshot"
    hint = caption_hint_suffix or f"page{page_label}_{snapshot_kind}_snapshot"
    temp_s3_key = f"{user_id}/extracted_images/{secure_filename(doc_name)}/{hint}.png"
    try:
        image_analysis = analyze_image_content_with_ai(image_bytes, context_text[:500], temp_s3_key)
    except Exception as analysis_exc:
        logger.warning(
            "[PAGE SNAPSHOT] Image analysis failed for page %s of '%s': %s",
            page_label,
            log_safe(doc_name),
            log_safe(analysis_exc),
        )
        image_analysis = {
            "caption": default_caption,
            "keywords": [],
            "topics": [],
            "type": "document_text",
            "content": context_text[:500] if context_text else "",
            "s3_key": temp_s3_key,
        }

    # Full-slide PPT snapshots are always kept — layout fidelity matters more than decorative heuristics.
    if snapshot_kind != "slide" and _is_irrelevant_image(image_analysis):
        logger.info("[PAGE SNAPSHOT] Discarding irrelevant snapshot on page %s", page_label)
        return ""

    if snapshot_kind == "slide":
        image_analysis["is_decorative"] = False
        image_analysis["is_page_snapshot"] = True

    caption = image_analysis.get("caption") or default_caption
    clean_caption = re.sub(r"[^\w\s-]", "", caption)
    clean_caption = re.sub(r"\s+", "_", clean_caption.strip())
    image_filename = f"{clean_caption[:50]}.png"
    s3_image_key = (
        f"{user_id}/extracted_images/{secure_filename(doc_name)}/"
        f"{secure_filename(image_filename)}"
    )
    image_analysis["s3_key"] = s3_image_key
    s3_client.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=s3_image_key,
        Body=image_bytes,
        ContentType="image/png",
    )
    save_image_analysis_to_s3(user_id, doc_name, image_analysis)
    extracted_images.append(
        {
            "s3_key": s3_image_key,
            "page_num": page_label,
            "extraction_index": len(extracted_images),
            "caption": caption,
            "context_text": context_text[:500],
            "ai_analysis": image_analysis,
            "keywords": image_analysis.get("keywords", []),
            "topics": image_analysis.get("topics", []),
            "content_description": image_analysis.get("content", ""),
            "image_type": image_analysis.get("type", snapshot_kind),
            "is_page_snapshot": True,
        }
    )
    logger.info(
        "[PAGE SNAPSHOT] Saved %s snapshot for page %s: %s",
        snapshot_kind,
        page_label,
        s3_image_key,
    )
    marker = f"\n[IMAGE: {s3_image_key}]\n"
    if caption:
        marker += f"*{caption}*\n"
    return marker


def _docx_pdf_page_needs_snapshot(page: fitz.Page, page_num: int) -> bool:
    """
    Heuristic for DOCX-derived PDFs: use a full-page snapshot when visuals are
    complex (diagrams, architecture drawings, multiple/large images).
    """
    try:
        page_area = max(page.rect.get_area(), 1.0)
        images = page.get_images(full=True)
        # If we already have a strong embedded image candidate on this page, prefer the
        # standalone image over a full-page snapshot to avoid duplicates.
        max_img_ratio = 0.0
        if images:
            for img in images:
                try:
                    bbox = page.get_image_bbox(img)
                    max_img_ratio = max(max_img_ratio, float(bbox.get_area() / page_area))
                except Exception:
                    pass

        # For DOCX, only snapshot when the page is genuinely hard to extract:
        # dense tables, multi-figure layouts, or diagram-heavy pages.
        if len(images) >= 2:
            return True

        blocks = page.get_text("blocks") or []
        # Exclude footers/footnotes/watermarks from table/complexity signals
        content_blocks = []
        text_len = 0
        for b in blocks:
            if len(b) > 4:
                txt = (b[4] or "").strip()
                if txt and not _is_footnote_text(txt):
                    text_len += len(txt)
                    content_blocks.append(b)

        # Table-like pages often have many small blocks (cell fragments)
        small_blocks = 0
        for b in content_blocks:
            try:
                x0, y0, x1, y1 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
                w = max(0.0, x1 - x0)
                h = max(0.0, y1 - y0)
                if w < page.rect.width * 0.55 and h < page.rect.height * 0.08:
                    small_blocks += 1
            except Exception:
                continue
        if len(content_blocks) >= 35 and small_blocks >= 18:
            # If a large embedded figure already exists, let the standalone image represent it.
            if max_img_ratio >= 0.15 and len(images) == 1:
                return False
            return True

        # Vector-heavy pages (shapes/diagrams) are hard to extract reliably
        try:
            drawings = page.get_drawings() or []
            if len(drawings) >= 60 and text_len >= 200:
                if max_img_ratio >= 0.15 and len(images) == 1:
                    return False
                return True
        except Exception:
            pass

        # Large single image: snapshot only if the surrounding page is complex
        if images:
            large_img = False
            for img in images:
                try:
                    bbox = page.get_image_bbox(img)
                    if bbox.get_area() / page_area >= 0.25:
                        large_img = True
                        break
                except Exception:
                    pass
            if large_img and (len(content_blocks) >= 20 or text_len >= 800):
                return True
            # Otherwise, prefer the embedded image (no full-page snapshot).
            return False

        # As a last resort, treat vector-diagram detection as a signal (expensive)
        if len(images) == 0:
            if _extract_vector_diagrams_from_page(page, page_num):
                return True
    except Exception as exc:
        logger.warning("[DOCX SNAPSHOT] Heuristic failed on page %s: %s", page_num + 1, exc)
    return False


def _filter_ppt_layout_slide_images(extracted_images: list) -> list:
    """Keep only full-slide layout snapshots in slide order (drop partial/embedded assets)."""
    layout = []
    for img in extracted_images:
        if not img.get("is_page_snapshot"):
            continue
        key = (img.get("s3_key") or "").lower()
        if key.endswith("/image.png"):
            continue
        layout.append(img)
    layout.sort(key=lambda row: (row.get("page_num") or 0, row.get("extraction_index") or 0))
    return layout


def _build_ppt_slide_source_text(
    layout_slides: list, slide_texts: Optional[Dict[int, str]] = None
) -> str:
    """
    One full-slide image per slide plus its content — not one module per slide.

    slide_texts holds the slide's own words, read from the .pptx by slide number. It is
    included because a slide snapshot is an IMAGE: its text is pixels, so this text is the
    only path by which the actual wording of slides reaches module generation. Without it
    the model sees just the AI caption of each slide, which is why decks generated modules
    covering only the title slide.

    Iteration is over the UNION of slide numbers, not over layout_slides, because the two
    sets can disagree: LibreOffice sometimes exports fewer PNGs than the deck has slides
    (and a per-slide upload can fail individually), so keying the loop on the images would
    silently drop the text of every slide that has no snapshot - reproducing the original
    symptom on any deck where the export is incomplete.
    """
    parts: List[str] = []
    slide_texts = slide_texts or {}

    def _slide_key(value) -> Optional[int]:
        try:
            key = int(value)
        except (TypeError, ValueError):
            return None
        return key if key > 0 else None

    images_by_slide: Dict[int, dict] = {}
    unnumbered: List[dict] = []
    for img in layout_slides or []:
        key = _slide_key(img.get("page_num"))
        if key is None or key in images_by_slide:
            unnumbered.append(img)
        else:
            images_by_slide[key] = img

    ordered_keys = sorted(set(images_by_slide) | {k for k in slide_texts if _slide_key(k)})

    def _emit(img: Optional[dict], slide_num: Optional[int]) -> None:
        analysis = {}
        if img and isinstance(img.get("ai_analysis"), dict):
            analysis = img["ai_analysis"]
        topic = (analysis.get("caption") or (img or {}).get("caption") or "").strip()
        if not topic or topic.lower() == "image":
            topic = f"Presentation section {slide_num or len(parts) + 1}"
        parts.append(f"\n--- {topic} ---\n")
        if img and img.get("s3_key"):
            parts.append(f"[IMAGE: {img['s3_key']}]\n")
        native_text = (slide_texts.get(slide_num) or "").strip() if slide_num else ""
        if native_text:
            parts.append(f"{native_text}\n")
        summary = (analysis.get("content") or "").strip()
        if summary:
            parts.append(f"{summary}\n")

    for slide_num in ordered_keys:
        _emit(images_by_slide.get(slide_num), slide_num)
    # Snapshots that carry no usable slide number still belong in the source text; they are
    # appended rather than dropped so no captured image is lost.
    for img in unnumbered:
        _emit(img, None)
    return "".join(parts)


def _restrict_catalog_to_ppt_layout_images(catalog_rows: list, layout_slides: list) -> list:
    allowed = {row["s3_key"] for row in layout_slides if row.get("s3_key")}
    if not allowed:
        return catalog_rows
    return [row for row in catalog_rows if row.get("s3_key") in allowed]


PPT_MULTI_MODULE_INSTRUCTION = (
    "\n\n**POWERPOINT SOURCE:** Each [IMAGE: ...] is a FULL SLIDE screenshot (layout preserved). "
    "You MUST include every slide image assigned to this module using the exact [IMAGE: path] from the catalog, "
    "in slide/page order. Do NOT create one module per slide unless the user explicitly requested that count. "
    "Group related slides into modules by topic. "
    "Focus module text on concepts and processes, not colors or decorative layout. "
    "Do not use headings like 'Slide 7' or 'Slide 8'.\n"
    "**COVER THE WHOLE DECK:** every '--- ... ---' section in the source is one slide. Some "
    "sections have no [IMAGE: ...] because that slide's screenshot could not be rendered - "
    "their text is still slide content and MUST be covered. Never write modules about only "
    "the first slide or only the slides that happen to have an image.\n"
)


def _finalize_ppt_module_output(modules: list) -> list:
    for mod in modules or []:
        if mod.get("title"):
            mod["title"] = _strip_slide_number_from_title(mod["title"])
        if mod.get("edited_content"):
            mod["edited_content"] = _clean_ppt_module_markdown(mod["edited_content"])
    return modules


def _default_ppt_module_count(slide_count: int) -> int:
    """Soft hint for grouping slides (~3 slides per module), not one module per slide."""
    if slide_count <= 1:
        return 1
    return max(2, min(5, (slide_count + 2) // 3))


def _ppt_group_note(slide_count: int, image_count: int, requested_count: int) -> str:
    """
    Tell the model how many slides the deck really has, and how many of them have an image.

    The two numbers can differ: LibreOffice's PNG export sometimes yields fewer snapshots
    than the deck has slides. Stating only the image count made the model treat a deck as a
    one-slide document and write a single module about the title slide, so the slide count
    leads and the image count is named separately.
    """
    if slide_count <= 0:
        return ""
    note = (
        f" This is a PowerPoint with {slide_count} slide(s); cover the content of EVERY slide "
        f"and group them into these {requested_count} module(s) — do not use one module per "
        f"slide unless that count was explicitly requested."
    )
    if 0 < image_count < slide_count:
        note += (
            f" Only {image_count} slide(s) have a full-slide image available; use the slide "
            f"text for the rest and do not skip them."
        )
    return note


def _ppt_layout_snapshot_key(s3_key: str) -> bool:
    k = (s3_key or "").lower()
    return "layout_snapshot" in k or "slide" in k and "_snapshot" in k


def _ensure_ppt_slide_images_in_modules(modules: list, layout_slides: list) -> list:
    """
    Guarantee every full-slide PPT snapshot appears in module content in slide order.
    Distributes slides across modules when the model omitted [IMAGE:] tags.
    """
    if not modules or not layout_slides:
        return modules
    layout_slides = _filter_ppt_layout_slide_images(layout_slides)
    if not layout_slides:
        return modules

    ordered_keys = [s["s3_key"] for s in layout_slides if s.get("s3_key")]
    if not ordered_keys:
        return modules

    img_pat = re.compile(r"\[IMAGE:\s*([^\]]+)\]")
    mods = sorted(modules, key=lambda m: m.get("seg_idx", 0))
    n_mod = len(mods)
    n_slide = len(ordered_keys)
    used_globally: set = set()

    for mod in mods:
        for k in img_pat.findall(mod.get("edited_content") or ""):
            used_globally.add(k.strip())

    for mod_i, mod in enumerate(mods):
        start = (mod_i * n_slide) // n_mod
        end = ((mod_i + 1) * n_slide) // n_mod
        slice_slides = layout_slides[start:end]
        content = (mod.get("edited_content") or "").rstrip()
        append_parts: List[str] = []

        for slide in slice_slides:
            key = slide.get("s3_key")
            if not key or key in content or key in used_globally:
                continue
            analysis = slide.get("ai_analysis") if isinstance(slide.get("ai_analysis"), dict) else {}
            cap = (analysis.get("caption") or slide.get("caption") or "").strip()
            if not cap or cap.lower() == "image":
                page = slide.get("page_num")
                cap = f"Presentation slide {page}" if page else "Presentation slide"
            append_parts.append(f"### {cap}\n\n[IMAGE: {key}]\n")
            used_globally.add(key)

        if append_parts:
            if "## Presentation slides" not in content:
                content += "\n\n## Presentation slides\n\n"
            else:
                content += "\n\n"
            content += "\n\n".join(append_parts)
            mod["edited_content"] = content

    # Any slide still missing (e.g. wrong module split) goes to the best-matching module by page
    for slide in layout_slides:
        key = slide.get("s3_key")
        if not key or key in used_globally:
            continue
        page = slide.get("page_num") or 1
        mod_i = min(n_mod - 1, max(0, int((page - 1) * n_mod / max(n_slide, 1))))
        mod = mods[mod_i]
        analysis = slide.get("ai_analysis") if isinstance(slide.get("ai_analysis"), dict) else {}
        cap = (analysis.get("caption") or slide.get("caption") or f"Presentation slide {page}").strip()
        block = f"\n\n### {cap}\n\n[IMAGE: {key}]\n"
        cur = (mod.get("edited_content") or "").rstrip()
        if "## Presentation slides" not in cur:
            cur += "\n\n## Presentation slides\n\n"
        mod["edited_content"] = cur + block
        used_globally.add(key)
        logger.info("[PPT ENFORCE] Appended missing slide image to module %s: %s", mod_i + 1, key)

    return mods


def _strip_slide_number_from_title(title: str) -> str:
    t = (title or "").strip().strip('"').strip("'")
    t = re.sub(r"^Slide\s+\d+\s*[-:–—]?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[-:–—]\s*Slide\s+\d+\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^Slide\s+\d+\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()


def _clean_ppt_module_markdown(content: str) -> str:
    """Remove slide-number headings and visual-design filler from generated modules."""
    if not content:
        return content
    out = content
    out = re.sub(r"^#+\s*Slide\s+\d+\s*$", "", out, flags=re.MULTILINE | re.IGNORECASE)
    out = re.sub(r"^#+\s*Slide\s+\d+\s*[-:–—].*$", "", out, flags=re.MULTILINE | re.IGNORECASE)
    out = re.sub(r"\n---\s*Slide\s+\d+.*?---\s*\n", "\n", out, flags=re.IGNORECASE)
    return out.strip()


def _process_pdf_page_snapshot(
    page: fitz.Page,
    page_num: int,
    doc_name: str,
    user_id: str,
    page_text: str,
    extracted_images: list,
    snapshot_kind: str = "slide",
) -> str:
    """Upload a full-page PNG snapshot; return markdown/image markers for module text."""
    zoom = _slide_snapshot_zoom(snapshot_kind)
    try:
        image_bytes = _render_pdf_page_snapshot_png(page, zoom=zoom)
    except Exception as exc:
        logger.warning(
            "[PAGE SNAPSHOT] Render failed for page %s of '%s': %s",
            page_num + 1,
            log_safe(doc_name),
            log_safe(exc),
        )
        return f"\n\nSlide {page_num + 1} (layout capture unavailable)\n"
    page_label = page_num + 1
    return _upload_layout_snapshot_image(
        image_bytes,
        page_label,
        doc_name,
        user_id,
        page_text,
        extracted_images,
        snapshot_kind=snapshot_kind,
    )


def split_text_into_chunks(text: str, chunk_size_words: int, overlap_words: int) -> list[str]:
    sentences = sent_tokenize(text);
    if not sentences: return []
    words_per_sentence = [count_words(s) for s in sentences]; chunks, current_chunk_sentences, current_word_count = [], [], 0
    for i, sentence in enumerate(sentences):
        sentence_word_count = words_per_sentence[i]
        if current_word_count + sentence_word_count > chunk_size_words and current_chunk_sentences:
            chunks.append(" ".join(current_chunk_sentences)); words_to_keep, sentences_to_keep = 0, []
            for j in range(len(current_chunk_sentences) - 1, -1, -1):
                word_count_j = count_words(current_chunk_sentences[j])
                if words_to_keep + word_count_j <= overlap_words: words_to_keep += word_count_j; sentences_to_keep.insert(0, current_chunk_sentences[j])
                else: break
            current_chunk_sentences, current_word_count = sentences_to_keep, words_to_keep
        current_chunk_sentences.append(sentence); current_word_count += sentence_word_count
    if current_chunk_sentences: chunks.append(" ".join(current_chunk_sentences))
    return [c for c in chunks if count_words(c) >= DEFAULT_MIN_CHUNK_WORDS]

def find_segment_boundaries(embeddings: np.ndarray, threshold_multiplier: float) -> list[int]:
    if embeddings.shape[0] < 3: return [0]
    similarities = cosine_similarity(embeddings[:-1], embeddings[1:]).diagonal()
    if len(similarities) < 2: return [0]
    abs_diffs = np.abs(np.diff(similarities));
    if abs_diffs.size == 0: return [0]
    mean_diff, std_diff = np.mean(abs_diffs), np.std(abs_diffs)
    threshold = mean_diff + threshold_multiplier * std_diff if std_diff > 0 else np.inf
    boundary_indices = [i + 1 for i, sim in enumerate(similarities) if (i > 0 and (similarities[i-1] - sim > threshold))]
    return sorted(list(set([0] + boundary_indices)))

def group_chunks_into_segments(chunks: list[str], boundary_indices: list[int], min_segment_words: int) -> list[str]:
    segments = []
    for i in range(len(boundary_indices)):
        start_idx = boundary_indices[i]; end_idx = boundary_indices[i+1] if i + 1 < len(boundary_indices) else len(chunks)
        segment_text = " ".join(chunks[start_idx:end_idx]).strip()
        if count_words(segment_text) >= min_segment_words: segments.append(segment_text)
    return segments

def segment_text_intelligently(chunks: list[str], requested_count: int, min_segment_words: int = 25) -> list[str]:
    """
    Intelligently segment text into the exact number of modules requested by the user.
    
    Args:
        chunks: List of text chunks
        requested_count: Number of modules/segments requested by user
        min_segment_words: Minimum words per segment
        
    Returns:
        List of text segments matching the requested count
    """
    logger.info('[INTELLIGENT SEGMENTATION] Creating exactly %s segments from %s chunks', ("" if requested_count is None else str(requested_count).replace("\n", "").replace("\r", "")), len(chunks))
    
    if not chunks:
        return []
    
    total_text = " ".join(chunks)
    total_words = count_words(total_text)
    
    # If requested count is 1, return all text as one segment
    if requested_count == 1:
        logger.info('[INTELLIGENT SEGMENTATION] Single segment requested - returning full text (%s words)', ("" if total_words is None else str(total_words).replace("\n", "").replace("\r", "")))
        return [total_text]
    
    # Calculate target words per segment
    target_words_per_segment = max(min_segment_words, total_words // requested_count)
    logger.info('[INTELLIGENT SEGMENTATION] Target words per segment: %s', ("" if target_words_per_segment is None else str(target_words_per_segment).replace("\n", "").replace("\r", "")))
    
    # Create segments by distributing chunks evenly
    segments = []
    chunks_per_segment = max(1, len(chunks) // requested_count)
    
    for i in range(requested_count):
        start_chunk = i * chunks_per_segment
        
        # For the last segment, include all remaining chunks
        if i == requested_count - 1:
            end_chunk = len(chunks)
        else:
            end_chunk = (i + 1) * chunks_per_segment
        
        segment_chunks = chunks[start_chunk:end_chunk]
        segment_text = " ".join(segment_chunks).strip()
        
        # Ensure segment has minimum words
        if count_words(segment_text) >= min_segment_words:
            segments.append(segment_text)
            logger.info('[INTELLIGENT SEGMENTATION] Segment %s: %s words', ("" if i+1 is None else str(i+1).replace("\n", "").replace("\r", "")), count_words(segment_text))
        else:
            # If segment is too small, merge with previous segment
            if segments:
                segments[-1] += " " + segment_text
                logger.info('[INTELLIGENT SEGMENTATION] Merged small segment %s with previous segment', ("" if i+1 is None else str(i+1).replace("\n", "").replace("\r", "")))
            else:
                segments.append(segment_text)
                logger.info('[INTELLIGENT SEGMENTATION] Kept small segment %s as first segment', ("" if i+1 is None else str(i+1).replace("\n", "").replace("\r", "")))
    
    # If we have fewer segments than requested, try to split the largest segment
    while len(segments) < requested_count:
        # Find the largest segment
        largest_idx = max(range(len(segments)), key=lambda i: count_words(segments[i]))
        largest_segment = segments[largest_idx]
        largest_words = count_words(largest_segment)
        
        # Only split if the segment is large enough
        if largest_words >= min_segment_words * 2:
            # Split the largest segment in half
            sentences = sent_tokenize(largest_segment)
            mid_point = len(sentences) // 2
            
            first_half = " ".join(sentences[:mid_point])
            second_half = " ".join(sentences[mid_point:])
            
            # Replace the largest segment with two halves
            segments[largest_idx] = first_half
            segments.insert(largest_idx + 1, second_half)
            
            logger.info('[INTELLIGENT SEGMENTATION] Split segment %s into two parts', ("" if largest_idx+1 is None else str(largest_idx+1).replace("\n", "").replace("\r", "")))
        else:
            # Can't split further without going below minimum words
            logger.warning('[INTELLIGENT SEGMENTATION] Cannot create %s segments - content too small', ("" if requested_count is None else str(requested_count).replace("\n", "").replace("\r", "")))
            break
    
    # If we have more segments than requested, merge the smallest ones
    while len(segments) > requested_count:
        # Find two adjacent segments with the smallest combined size
        min_combined_size = float('inf')
        merge_idx = 0
        
        for i in range(len(segments) - 1):
            combined_size = count_words(segments[i]) + count_words(segments[i + 1])
            if combined_size < min_combined_size:
                min_combined_size = combined_size
                merge_idx = i
        
        # Merge the two segments
        segments[merge_idx] = segments[merge_idx] + " " + segments[merge_idx + 1]
        segments.pop(merge_idx + 1)
        
        logger.info('[INTELLIGENT SEGMENTATION] Merged segments %s and %s', ("" if merge_idx+1 is None else str(merge_idx+1).replace("\n", "").replace("\r", "")), ("" if merge_idx+2 is None else str(merge_idx+2).replace("\n", "").replace("\r", "")))
    
    logger.info('[INTELLIGENT SEGMENTATION] ✅ Created %s segments (requested: %s)', len(segments), ("" if requested_count is None else str(requested_count).replace("\n", "").replace("\r", "")))
    
    # Log segment statistics
    for i, segment in enumerate(segments):
        word_count = count_words(segment)
        logger.info('[INTELLIGENT SEGMENTATION] Final Segment %s: %s words', ("" if i+1 is None else str(i+1).replace("\n", "").replace("\r", "")), ("" if word_count is None else str(word_count).replace("\n", "").replace("\r", "")))
    
    return segments

def _prompt_requests_combine_into_single_module(prompt_lower: str) -> bool:
    """
    Detect "combine/merge/join ... into (a) single/one/1 module".
    Linear scan equivalent of the former ReDoS-prone regex (Sonar S5852).
    ``prompt_lower`` must already be lowercased.
    """
    if not prompt_lower:
        return False
    n = len(prompt_lower)
    for verb in ("combine", "merge", "join"):
        pos = 0
        vlen = len(verb)
        while True:
            i = prompt_lower.find(verb, pos)
            if i < 0:
                break
            after = i + vlen
            # Former pattern required whitespace after the verb (\s+)
            if after >= n or not prompt_lower[after].isspace():
                pos = i + 1
                continue
            j = after
            while j < n and prompt_lower[j].isspace():
                j += 1
            # Find "into" with preceding whitespace and non-empty .+? before it
            into_pos = prompt_lower.find("into", j + 1)
            while into_pos >= 0:
                if into_pos == 0 or not prompt_lower[into_pos - 1].isspace():
                    into_pos = prompt_lower.find("into", into_pos + 1)
                    continue
                between = prompt_lower[j:into_pos]
                if not between.rstrip():
                    into_pos = prompt_lower.find("into", into_pos + 1)
                    continue
                k = into_pos + 4
                # Former pattern required \s+ after "into"
                if k >= n or not prompt_lower[k].isspace():
                    into_pos = prompt_lower.find("into", into_pos + 1)
                    continue
                while k < n and prompt_lower[k].isspace():
                    k += 1
                # Optional "a "
                if prompt_lower.startswith("a", k):
                    k2 = k + 1
                    if k2 < n and prompt_lower[k2].isspace():
                        while k2 < n and prompt_lower[k2].isspace():
                            k2 += 1
                        k = k2
                for word in ("single", "one", "1"):
                    if not prompt_lower.startswith(word, k):
                        continue
                    m = k + len(word)
                    if word != "1" and m < n and prompt_lower[m].isalnum():
                        continue
                    if m >= n or not prompt_lower[m].isspace():
                        continue
                    while m < n and prompt_lower[m].isspace():
                        m += 1
                    if prompt_lower.startswith("module", m):
                        return True
                into_pos = prompt_lower.find("into", into_pos + 1)
            pos = i + 1
    return False


def extract_module_count_with_llm(user_prompt: str) -> Optional[int]:
    """
    Extract the desired OUTPUT module count using BOTH regex and LLM.
    Handles "combine X and Y into single module" → 1, "create 4 modules" → 4, etc.
    """
    logger.info("[MODULE COUNT] ===== EXTRACTING MODULE COUNT =====")
    logger.info('[MODULE COUNT] User prompt: %s', ("" if user_prompt is None else str(user_prompt).replace("\n", "").replace("\r", "")))
    
    import re
    from collections import Counter
    
    user_prompt_lower = user_prompt.lower()
    
    # STEP 0: Detect "combine/merge into single/one module" → always 1 output module
    if _prompt_requests_combine_into_single_module(user_prompt_lower):
        logger.info("[MODULE COUNT] ✅ Detected 'combine into single module' → 1 output module")
        return 1
    
    # STEP 1: Try regex extraction (both numeric and word formats)
    regex_result = None
    all_numbers = []
    
    # Numeric patterns: "create 4 modules", "generate 3 sections", etc.
    # IMPORTANT: exclude patterns like "module 1 and 2" (input selection, not output count)
    numeric_patterns = [
        r'create\s+(\d+)\s+(?:modules?|sections?|parts?|chapters?)',
        r'generate\s+(\d+)\s+(?:modules?|sections?|parts?|chapters?)',
        r'make\s+(\d+)\s+(?:modules?|sections?|parts?|chapters?)',
        r'(?:need|want|exactly)\s+(\d+)\s+(?:modules?|sections?|parts?)',
        r'total\s+(?:of\s+)?(\d+)\s+(?:modules?|sections?|parts?)',
        r'(?:only|just)\s+(\d+)\s+(?:modules?|sections?|parts?)',
        r'reduce\s+to\s+(\d+)\s+(?:modules?|sections?|parts?)',
        r'split\s+into\s+(\d+)\s+(?:modules?|sections?|parts?)',
    ]
    
    for pattern in numeric_patterns:
        matches = re.findall(pattern, user_prompt_lower)
        for match in matches:
            num = int(match)
            if 1 <= num <= 20:
                all_numbers.append(num)
                logger.info("[MODULE COUNT] ✓ Regex numeric pattern '%s' found: %s", ("" if pattern is None else str(pattern).replace("\n", "").replace("\r", "")), ("" if num is None else str(num).replace("\n", "").replace("\r", "")))
    
    # Word patterns: "three modules", "four sections", etc.
    word_to_num = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
        'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20
    }
    
    word_patterns = [
        r'create\s+({words})\s+(?:modules?|sections?|parts?|chapters?)',
        r'generate\s+({words})\s+(?:modules?|sections?|parts?|chapters?)',
        r'make\s+({words})\s+(?:modules?|sections?|parts?|chapters?)',
        r'(?:need|want|exactly)\s+({words})\s+(?:modules?|sections?|parts?)',
        r'total\s+(?:of\s+)?({words})\s+(?:modules?|sections?|parts?)',
        r'split\s+into\s+({words})\s+(?:modules?|sections?|parts?)',
    ]
    
    words_regex = '|'.join(word_to_num.keys())
    for pattern_template in word_patterns:
        pattern = pattern_template.format(words=words_regex)
        matches = re.findall(pattern, user_prompt_lower)
        for match in matches:
            if match in word_to_num:
                num = word_to_num[match]
                all_numbers.append(num)
                logger.info("[MODULE COUNT] ✓ Regex word pattern found: '%s' = %s", ("" if match is None else str(match).replace("\n", "").replace("\r", "")), ("" if num is None else str(num).replace("\n", "").replace("\r", "")))
    
    if all_numbers:
        counter = Counter(all_numbers)
        regex_result = counter.most_common(1)[0][0]
        logger.info('[MODULE COUNT] ✅ Regex extraction result: %s modules', ("" if regex_result is None else str(regex_result).replace("\n", "").replace("\r", "")))
    else:
        logger.info("[MODULE COUNT] ⚠️ Regex extraction found no matches")
    
    # STEP 2: Try LLM extraction for validation/fallback
    llm_result = None
    try:
        extraction_prompt = f"""You are a helpful assistant that extracts information from user instructions.

The user has provided the following instruction for regenerating course modules:

"{user_prompt}"

Your task: Extract ONLY the number of OUTPUT modules the user wants to end up with after regeneration.

Rules:
- "combine module 1 and 2 into single module" → 1 (they want 1 output module from combining)
- "create 3 modules" → 3
- "generate 4 sections" → 4
- "split into 2 modules" → 2
- "module 3 and 4 are fine, do not change" → 0 (no output count specified, just skip instructions)
- "regenerate module 1 with more examples" → 0 (no count specified)
- If no specific OUTPUT count is mentioned, return: 0

IMPORTANT: Do NOT count module numbers used for selection (like "module 1 and 2") as the output count.

Return ONLY a single number (0-20). Do not include any explanation.

Number:"""

        response = sync_bedrock_client.converse(
            modelId="amazon.nova-pro-v1:0",
            messages=[{"role": "user", "content": [{"text": extraction_prompt}]}],
            inferenceConfig={"maxTokens": 10, "temperature": 0.1}
        )
        
        result_text = response['output']['message']['content'][0]['text'].strip()
        logger.info('[MODULE COUNT] LLM response: %s', ("" if result_text is None else str(result_text).replace("\n", "").replace("\r", "")))
        
        numbers = re.findall(r'\d+', result_text)
        if numbers:
            count = int(numbers[0])
            if 1 <= count <= 20:
                llm_result = count
                logger.info('[MODULE COUNT] ✅ LLM extraction result: %s modules', ("" if llm_result is None else str(llm_result).replace("\n", "").replace("\r", "")))
            elif count == 0:
                logger.info("[MODULE COUNT] ⚠️ LLM found no specific count (returned 0)")
            else:
                logger.warning('[MODULE COUNT] ⚠️ LLM returned invalid count: %s (must be 1-20)', ("" if count is None else str(count).replace("\n", "").replace("\r", "")))
        else:
            logger.warning('[MODULE COUNT] ⚠️ Could not parse number from LLM response: %s', ("" if result_text is None else str(result_text).replace("\n", "").replace("\r", "")))
            
    except Exception as e:
        logger.error('[MODULE COUNT] ❌ LLM extraction failed: %s', e)
    
    # STEP 3: Validate and decide
    if regex_result and llm_result:
        if regex_result == llm_result:
            logger.info('[MODULE COUNT] ✅✅ VALIDATED: Both regex and LLM agree on %s modules', ("" if regex_result is None else str(regex_result).replace("\n", "").replace("\r", "")))
            return regex_result
        else:
            logger.warning('[MODULE COUNT] ⚠️ MISMATCH: Regex=%s, LLM=%s. Trusting LLM (better context understanding).', ("" if regex_result is None else str(regex_result).replace("\n", "").replace("\r", "")), ("" if llm_result is None else str(llm_result).replace("\n", "").replace("\r", "")))
            return llm_result
    elif regex_result:
        logger.info('[MODULE COUNT] ✅ Using regex result: %s modules (LLM failed)', ("" if regex_result is None else str(regex_result).replace("\n", "").replace("\r", "")))
        return regex_result
    elif llm_result:
        logger.info('[MODULE COUNT] ✅ Using LLM result: %s modules (regex failed)', ("" if llm_result is None else str(llm_result).replace("\n", "").replace("\r", "")))
        return llm_result
    else:
        logger.info("[MODULE COUNT] ❌ Both regex and LLM failed to extract module count")
        return None

def parse_module_selection_instructions(user_prompt: str, existing_modules: List[dict]) -> dict:
    """
    Intelligently parse user instructions to determine which modules to regenerate.
    
    Supports multiple selection methods:
    1. By module number: "module 1", "modules 1 and 2", "1-3", "first module"
    2. By module title: "Model Context Protocol", "Training and Fine-tuning"
    3. By document name: "mcp.pdf", "citi_arch.docx"
    4. By natural language: "the first module", "the last two modules", "all modules"
    5. Exclusion patterns: "do not modify", "skip", "leave unchanged"
    
    Args:
        user_prompt: User's regeneration instructions
        existing_modules: List of existing modules with 'title', 'seg_idx', 'doc_name' fields
    
    Returns:
        {
            'modules_to_regenerate': set of module seg_idx to regenerate,
            'modules_to_skip': set of module seg_idx to skip,
            'regenerate_all': bool (True if user wants all modules),
            'skip_all': bool (True if user wants to skip all),
            'reason': str (explanation of what was parsed)
        }
    """
    logger.info('[MODULE SELECTION] Parsing module selection from: %s...', ("" if user_prompt[:200] is None else str(user_prompt[:200]).replace("\n", "").replace("\r", "")))
    
    modules_to_regenerate = set()
    modules_to_skip = set()
    regenerate_all = False
    skip_all = False
    reason = ""
    
    prompt_lower = user_prompt.lower()
    
    # Create a mapping of module info for matching
    module_by_title = {}
    module_by_number = {}
    module_by_doc = {}
    module_by_doc_no_ext = {}  # Document name without extension
    
    for module in existing_modules:
        seg_idx = module.get('seg_idx', 0)
        title = module.get('title', '').lower()
        doc_name = module.get('doc_name', '').lower()
        
        # Extract document name without extension
        doc_name_no_ext = doc_name.rsplit('.', 1)[0] if '.' in doc_name else doc_name
        
        if title:
            module_by_title[title] = seg_idx
        module_by_number[seg_idx] = module
        if doc_name not in module_by_doc:
            module_by_doc[doc_name] = []
        module_by_doc[doc_name].append(seg_idx)
        
        if doc_name_no_ext not in module_by_doc_no_ext:
            module_by_doc_no_ext[doc_name_no_ext] = []
        module_by_doc_no_ext[doc_name_no_ext].append(seg_idx)
    
    logger.info('[MODULE SELECTION] Available modules: %s total', len(existing_modules))
    for module in existing_modules:
        logger.info("[MODULE SELECTION]   - Module %s: '%s' from %s", ("" if module.get('seg_idx') is None else str(module.get('seg_idx')).replace("\n", "").replace("\r", "")), ("" if module.get('title') is None else str(module.get('title')).replace("\n", "").replace("\r", "")), ("" if module.get('doc_name') is None else str(module.get('doc_name')).replace("\n", "").replace("\r", "")))
    
    # Sort modules by seg_idx so positional numbers (1,2,3,4) map correctly to display order
    sorted_modules = sorted(existing_modules, key=lambda m: m.get('seg_idx', 0))
    # Build a display-number → seg_idx map (1-based)
    display_to_seg = {i + 1: m.get('seg_idx') for i, m in enumerate(sorted_modules)}
    seg_to_display = {v: k for k, v in display_to_seg.items()}
    logger.info('[MODULE SELECTION] Display number map: %s', ("" if display_to_seg is None else str(display_to_seg).replace("\n", "").replace("\r", "")))
    
    # PATTERN 1: Check for "regenerate all" or "all modules"
    if re.search(r'\b(?:all|every|each)\s+(?:modules?|sections?|parts?)\b', prompt_lower):
        regenerate_all = True
        modules_to_regenerate = set(m.get('seg_idx') for m in existing_modules)
        reason = "User requested to regenerate ALL modules"
        logger.info("[MODULE SELECTION] ✓ Pattern 1: Regenerate ALL modules")
        return {
            'modules_to_regenerate': modules_to_regenerate,
            'modules_to_skip': set(),
            'regenerate_all': True,
            'skip_all': False,
            'reason': reason
        }
    
    # PATTERN 2: Check for exclusion patterns — two styles:
    # Style A: "do not modify module 3" (keyword BEFORE module number)
    # Style B: "module 3 and 4 are fine / complete / do not change" (keyword AFTER)
    
    # Style A
    skip_pattern_a = r"(?:do not modify|skip|leave unchanged|don't change|don't regenerate|don't update|do not change|do not regenerate|do not update|keep|preserve)\s+(?:module[s]?\s+)?([0-9]+(?:\s*(?:and|,)\s*[0-9]+)*)"
    skip_matches_a = re.findall(skip_pattern_a, prompt_lower)
    
    # Style B: "module 3 and 4 are (fine|complete|ok|good|unchanged|correct|perfect|done)"
    skip_pattern_b = r"module[s]?\s+([0-9]+(?:\s*(?:and|,)\s*[0-9]+)*)\s+(?:are|is)\s+(?:fine|complete|ok|good|unchanged|correct|perfect|done|already\s+(?:fine|good|complete|ok|correct|done)|completely\s+fine)"
    skip_matches_b = re.findall(skip_pattern_b, prompt_lower)
    
    # Style C: "do not change anything in module 3 and 4" / "do not change anything in 3 and 4"
    skip_pattern_c = r"(?:do not|don't|please do not|plz do not)\s+(?:change|modify|touch|alter|update|regenerate)\s+(?:anything\s+)?(?:in\s+)?(?:module[s]?\s+)?([0-9]+(?:\s*(?:and|,)\s*[0-9]+)*)"
    skip_matches_c = re.findall(skip_pattern_c, prompt_lower)
    
    all_skip_raw = skip_matches_a + skip_matches_b + skip_matches_c
    
    def _parse_number_list(raw: str):
        """Extract all integers from a string like '3 and 4' or '1, 2, 3'."""
        return [int(x) for x in re.findall(r'\d+', raw)]
    
    if all_skip_raw:
        logger.info('[MODULE SELECTION] Found skip instructions: %s', ("" if all_skip_raw is None else str(all_skip_raw).replace("\n", "").replace("\r", "")))
        for raw in all_skip_raw:
            for display_num in _parse_number_list(raw):
                seg_idx = display_to_seg.get(display_num)
                if seg_idx is not None:
                    modules_to_skip.add(seg_idx)
                    logger.info('[MODULE SELECTION] ✓ Skip display module %s → seg_idx %s', ("" if display_num is None else str(display_num).replace("\n", "").replace("\r", "")), ("" if seg_idx is None else str(seg_idx).replace("\n", "").replace("\r", "")))
                else:
                    # Fallback: try direct seg_idx match
                    if display_num in module_by_number:
                        modules_to_skip.add(display_num)
                        logger.info('[MODULE SELECTION] ✓ Skip module seg_idx %s (direct)', ("" if display_num is None else str(display_num).replace("\n", "").replace("\r", "")))
    
    # Also handle title-based skip: "module 3 and 4 are fine" with title matching
    skip_title_pattern = r"(?:do not modify|skip|leave unchanged|don't change|don't regenerate|keep|preserve)\s+['\"]([^'\"]+)['\"]"
    skip_title_matches = re.findall(skip_title_pattern, prompt_lower)
    for match in skip_title_matches:
        match_lower = match.strip()
        for title, seg_idx in module_by_title.items():
            if match_lower in title or title in match_lower:
                modules_to_skip.add(seg_idx)
                logger.info('[MODULE SELECTION] ✓ Skip module %s (title match: %s)', ("" if seg_idx is None else str(seg_idx).replace("\n", "").replace("\r", "")), ("" if title is None else str(title).replace("\n", "").replace("\r", "")))
    
    # PATTERN 3: Check for document-specific instructions (with and without extension)
    # Match: "based on 'mcp.pdf'" or "based on 'mcp'" or "for document1.docx" or "for document1"
    doc_pattern = r"(?:based on|for|referring to|regarding|from)\s+['\"]?([^'\"]+?)(?:\.(?:pdf|docx|doc|pptx))?['\"]?"
    doc_matches = re.findall(doc_pattern, prompt_lower)
    
    if doc_matches:
        logger.info('[MODULE SELECTION] Found document-specific instructions: %s', ("" if doc_matches is None else str(doc_matches).replace("\n", "").replace("\r", "")))
        for match in doc_matches:
            match_lower = match.lower().strip()
            
            # Try exact match with extension
            for doc_name, seg_indices in module_by_doc.items():
                if match_lower in doc_name or doc_name in match_lower:
                    for seg_idx in seg_indices:
                        modules_to_regenerate.add(seg_idx)
                    logger.info('[MODULE SELECTION] ✓ Regenerate modules from %s (exact match)', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            
            # Try match without extension
            for doc_name_no_ext, seg_indices in module_by_doc_no_ext.items():
                if match_lower in doc_name_no_ext or doc_name_no_ext in match_lower:
                    for seg_idx in seg_indices:
                        modules_to_regenerate.add(seg_idx)
                    logger.info('[MODULE SELECTION] ✓ Regenerate modules from %s (no extension match)', ("" if doc_name_no_ext is None else str(doc_name_no_ext).replace("\n", "").replace("\r", "")))
    
    # PATTERN 4: Check for module number patterns
    # "module 1", "modules 1 and 2", "1-3", "first", "last", etc.
    
    # Numeric patterns: "module 1", "modules 1, 2, 3", "1-3", "combine module 1 and 2"
    numeric_pattern = r"(?:module[s]?|section[s]?|part[s]?)\s+(\d+)(?:\s*[-,]\s*(\d+))?"
    numeric_matches = re.findall(numeric_pattern, prompt_lower)
    
    # Also catch "combine module 1 and 2" style
    combine_pattern = r"(?:combine|merge|join)\s+(?:module[s]?\s+)?(\d+)\s+(?:and|&|with)\s+(\d+)"
    combine_matches = re.findall(combine_pattern, prompt_lower)
    for m in combine_matches:
        numeric_matches.append(m)
    
    if numeric_matches:
        logger.info('[MODULE SELECTION] Found numeric module patterns: %s', ("" if numeric_matches is None else str(numeric_matches).replace("\n", "").replace("\r", "")))
        for match in numeric_matches:
            start_display = int(match[0])
            end_display = int(match[1]) if match[1] else start_display
            
            for display_num in range(start_display, end_display + 1):
                seg_idx = display_to_seg.get(display_num)
                if seg_idx is not None:
                    modules_to_regenerate.add(seg_idx)
                    logger.info('[MODULE SELECTION] ✓ Regenerate display module %s → seg_idx %s', ("" if display_num is None else str(display_num).replace("\n", "").replace("\r", "")), ("" if seg_idx is None else str(seg_idx).replace("\n", "").replace("\r", "")))
                elif display_num in module_by_number:
                    modules_to_regenerate.add(display_num)
                    logger.info('[MODULE SELECTION] ✓ Regenerate module %s (direct)', ("" if display_num is None else str(display_num).replace("\n", "").replace("\r", "")))
    
    # Ordinal patterns: "first", "second", "last", "last two"
    ordinal_pattern = r"\b(?:first|second|third|fourth|fifth|last|last\s+two|last\s+three)\b"
    ordinal_matches = re.findall(ordinal_pattern, prompt_lower)
    
    if ordinal_matches:
        logger.info('[MODULE SELECTION] Found ordinal patterns: %s', ("" if ordinal_matches is None else str(ordinal_matches).replace("\n", "").replace("\r", "")))
        all_seg_indices = [m.get('seg_idx') for m in sorted_modules]
        
        for match in ordinal_matches:
            if match == 'first':
                if all_seg_indices:
                    modules_to_regenerate.add(all_seg_indices[0])
                    logger.info('[MODULE SELECTION] ✓ Regenerate first module (#%s)', ("" if all_seg_indices[0] is None else str(all_seg_indices[0]).replace("\n", "").replace("\r", "")))
            elif match == 'second':
                if len(all_seg_indices) > 1:
                    modules_to_regenerate.add(all_seg_indices[1])
                    logger.info('[MODULE SELECTION] ✓ Regenerate second module (#%s)', ("" if all_seg_indices[1] is None else str(all_seg_indices[1]).replace("\n", "").replace("\r", "")))
            elif match == 'third':
                if len(all_seg_indices) > 2:
                    modules_to_regenerate.add(all_seg_indices[2])
                    logger.info('[MODULE SELECTION] ✓ Regenerate third module (#%s)', ("" if all_seg_indices[2] is None else str(all_seg_indices[2]).replace("\n", "").replace("\r", "")))
            elif match == 'fourth':
                if len(all_seg_indices) > 3:
                    modules_to_regenerate.add(all_seg_indices[3])
                    logger.info('[MODULE SELECTION] ✓ Regenerate fourth module (#%s)', ("" if all_seg_indices[3] is None else str(all_seg_indices[3]).replace("\n", "").replace("\r", "")))
            elif match == 'fifth':
                if len(all_seg_indices) > 4:
                    modules_to_regenerate.add(all_seg_indices[4])
                    logger.info('[MODULE SELECTION] ✓ Regenerate fifth module (#%s)', ("" if all_seg_indices[4] is None else str(all_seg_indices[4]).replace("\n", "").replace("\r", "")))
            elif match == 'last':
                if all_seg_indices:
                    modules_to_regenerate.add(all_seg_indices[-1])
                    logger.info('[MODULE SELECTION] ✓ Regenerate last module (#%s)', ("" if all_seg_indices[-1] is None else str(all_seg_indices[-1]).replace("\n", "").replace("\r", "")))
            elif 'last' in match and 'two' in match:
                if len(all_seg_indices) >= 2:
                    modules_to_regenerate.add(all_seg_indices[-2])
                    modules_to_regenerate.add(all_seg_indices[-1])
                    logger.info('[MODULE SELECTION] ✓ Regenerate last two modules (#%s, #%s)', ("" if all_seg_indices[-2] is None else str(all_seg_indices[-2]).replace("\n", "").replace("\r", "")), ("" if all_seg_indices[-1] is None else str(all_seg_indices[-1]).replace("\n", "").replace("\r", "")))
            elif 'last' in match and 'three' in match:
                if len(all_seg_indices) >= 3:
                    modules_to_regenerate.add(all_seg_indices[-3])
                    modules_to_regenerate.add(all_seg_indices[-2])
                    modules_to_regenerate.add(all_seg_indices[-1])
                    logger.info("[MODULE SELECTION] ✓ Regenerate last three modules")
    
    # PATTERN 5: Check for module title patterns
    # Try to match partial titles
    for title, seg_idx in module_by_title.items():
        # Check if any significant words from the title appear in the prompt
        title_words = [w for w in title.split() if len(w) > 3]  # Words longer than 3 chars
        for word in title_words:
            if word in prompt_lower:
                modules_to_regenerate.add(seg_idx)
                logger.info('[MODULE SELECTION] ✓ Regenerate module %s (title match: %s)', ("" if seg_idx is None else str(seg_idx).replace("\n", "").replace("\r", "")), ("" if title is None else str(title).replace("\n", "").replace("\r", "")))
                break
    
    # PATTERN 6: Use LLM for intelligent parsing if no patterns matched
    if not modules_to_regenerate and not modules_to_skip:
        logger.info("[MODULE SELECTION] No patterns matched, using LLM for intelligent parsing...")
        try:
            module_list_str = "\n".join([
                f"  - Module {m.get('seg_idx')}: '{m.get('title')}' (from {m.get('doc_name')})"
                for m in existing_modules
            ])
            
            llm_prompt = f"""You are a helpful assistant that understands user instructions for module regeneration.

The course has the following modules:
{module_list_str}

The user provided this instruction:
"{user_prompt}"

Your task: Determine which modules the user wants to regenerate based on their instruction.

Rules:
- If the user mentions specific module numbers, regenerate those
- If the user mentions module titles, regenerate those modules
- If the user mentions document names (with or without file extension), regenerate modules from those documents
- If the user says "all modules" or "everything", regenerate all
- If the user says "do not modify X" or "skip X", skip those modules
- If the user's instruction is general (not specific to any module), regenerate all

Return ONLY a JSON object with this format (no other text):
{{"modules_to_regenerate": [list of module numbers], "modules_to_skip": [list of module numbers], "regenerate_all": boolean}}

Example: {{"modules_to_regenerate": [1, 2], "modules_to_skip": [], "regenerate_all": false}}

JSON:"""

            response = sync_bedrock_client.converse(
                modelId="amazon.nova-pro-v1:0",
                messages=[{"role": "user", "content": [{"text": llm_prompt}]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0.1}
            )
            
            result_text = response['output']['message']['content'][0]['text'].strip()
            logger.info('[MODULE SELECTION] LLM response: %s', ("" if result_text is None else str(result_text).replace("\n", "").replace("\r", "")))
            
            # Parse JSON response
            import json
            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                modules_to_regenerate = set(parsed.get('modules_to_regenerate', []))
                modules_to_skip = set(parsed.get('modules_to_skip', []))
                regenerate_all = parsed.get('regenerate_all', False)
                reason = "LLM parsed module selection"
                logger.info('[MODULE SELECTION] ✓ LLM parsed: regenerate=%s, skip=%s, all=%s', ("" if modules_to_regenerate is None else str(modules_to_regenerate).replace("\n", "").replace("\r", "")), ("" if modules_to_skip is None else str(modules_to_skip).replace("\n", "").replace("\r", "")), ("" if regenerate_all is None else str(regenerate_all).replace("\n", "").replace("\r", "")))
        except Exception as e:
            logger.error('[MODULE SELECTION] LLM parsing failed: %s', e)
            # Fallback: regenerate all if parsing fails
            modules_to_regenerate = set(m.get('seg_idx') for m in existing_modules)
            regenerate_all = True
            reason = "LLM parsing failed, defaulting to regenerate all"
    
    # Remove skipped modules from regenerate set
    modules_to_regenerate = modules_to_regenerate - modules_to_skip
    
    if not reason:
        if modules_to_regenerate:
            reason = f"Regenerate modules: {sorted(modules_to_regenerate)}"
        elif modules_to_skip:
            reason = f"Skip modules: {sorted(modules_to_skip)}, regenerate others"
        else:
            reason = "No specific modules selected, regenerating all"
            modules_to_regenerate = set(m.get('seg_idx') for m in existing_modules)
            regenerate_all = True
    
    logger.info("[MODULE SELECTION] ===== FINAL DECISION =====")
    logger.info('[MODULE SELECTION] Regenerate: %s', ("" if sorted(modules_to_regenerate) is None else str(sorted(modules_to_regenerate)).replace("\n", "").replace("\r", "")))
    logger.info('[MODULE SELECTION] Skip: %s', ("" if sorted(modules_to_skip) is None else str(sorted(modules_to_skip)).replace("\n", "").replace("\r", "")))
    logger.info('[MODULE SELECTION] Reason: %s', ("" if reason is None else str(reason).replace("\n", "").replace("\r", "")))
    
    return {
        'modules_to_regenerate': modules_to_regenerate,
        'modules_to_skip': modules_to_skip,
        'regenerate_all': regenerate_all,
        'skip_all': skip_all,
        'reason': reason
    }

def generate_course_title_from_content(content: str, source_docs: List[str], avoid_title: str = None) -> str:
    """
    Generate an AI-powered course title based on the content and source documents.
    Returns a well-structured, meaningful title (40-70 characters) that captures the essence of the course.
    
    Args:
        content: Combined content from all modules
        source_docs: List of source document names
        avoid_title: Optional title to avoid generating (for regeneration)
    """
    try:
        # Truncate content for analysis (use more content for better context)
        content_sample = content[:8000] if len(content) > 8000 else content
        
        # Create a more detailed prompt for better title generation
        doc_list = '\n'.join([f"  - {doc}" for doc in source_docs])
        
        # Add instruction to avoid previous title if provided
        avoid_instruction = f"\n\nIMPORTANT: Do NOT generate this title: '{avoid_title}'\nCreate a DIFFERENT title with a fresh perspective." if avoid_title else ""
        
        prompt = f"""You are an expert educational content curator. Analyze the following course content and generate a PROFESSIONAL, MEANINGFUL course title.

Number of Source Documents: {len(source_docs)}
Source Documents:
{doc_list}

Content Sample (from all modules):
{content_sample}

CRITICAL REQUIREMENTS:
1. Length: 40-70 characters (not too short, not too long)
2. Structure: Use proper title case (capitalize main words)
3. Clarity: Clearly convey what the course teaches
4. Professional: Suitable for an educational platform
5. **COMPREHENSIVE COVERAGE**: 
   - Carefully analyze ALL module titles and content provided above
   - Identify ALL major topics, concepts, protocols, or themes covered
   - The title MUST reflect ALL significant topics found in the modules
   - If multiple important topics are covered, include them all or use a broader encompassing term
   - Don't omit important topics just to make the title shorter
6. Accuracy: Base the title ONLY on what you find in the provided content
7. No generic words like "Course" or "Training" in the title
8. No quotes, special characters, or formatting
9. Be CREATIVE and provide a UNIQUE perspective{avoid_instruction}

ANALYSIS APPROACH:
1. First, read the MODULE TITLES section carefully to identify all major topics
2. Then, scan the content to understand the depth and breadth of coverage
3. Identify the common theme or domain that connects all modules
4. Create a title that accurately represents the complete scope of the course

**CRITICAL**: Base your title ONLY on what you find in the provided content above. Analyze the actual topics, concepts, and themes present in the modules.

Return ONLY the title text, nothing else."""

        response = sync_bedrock_client.converse(
            modelId="amazon.nova-pro-v1:0",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 150, "temperature": 0.7}  # Increased temperature for more variety
        )
        
        title = response['output']['message']['content'][0]['text'].strip()
        
        # Clean up the title
        title = title.strip('"\'').strip()
        title = title.replace('\n', ' ').replace('  ', ' ')
        
        # Validate title length and quality
        if len(title) < 10:
            logger.warning('Generated title too short (%s chars): %s', len(title), ("" if title is None else str(title).replace("\n", "").replace("\r", "")))
            raise ValueError("Title too short")
        
        if len(title) > 80:
            logger.warning('Generated title too long (%s chars), truncating: %s', len(title), ("" if title is None else str(title).replace("\n", "").replace("\r", "")))
            # Truncate at word boundary
            title = title[:77].rsplit(' ', 1)[0] + '...'
        
        # Ensure proper title case
        if title.isupper() or title.islower():
            title = title.title()
        
        logger.info('✅ Generated course title (%s chars): %s', len(title), ("" if title is None else str(title).replace("\n", "").replace("\r", "")))
        return title
        
    except Exception as e:
        logger.error('Error generating course title: %s', e)
        
        # Intelligent fallback: Create title from document names
        if len(source_docs) == 1:
            # Single document: use cleaned filename
            title = source_docs[0].replace('_', ' ').replace('-', ' ').rsplit('.', 1)[0]
            title = title.title()
        else:
            # Multiple documents: try to find common theme or use generic multi-doc title
            # Extract keywords from filenames
            keywords = []
            for doc in source_docs:
                name = doc.replace('_', ' ').replace('-', ' ').rsplit('.', 1)[0]
                words = [w for w in name.split() if len(w) > 3]  # Skip short words
                keywords.extend(words[:2])  # Take first 2 meaningful words from each
            
            if keywords:
                # Use first few keywords
                title = ' '.join(keywords[:4]).title()
                if len(source_docs) > 1:
                    title += f" and Related Topics"
            else:
                title = f"Multi-Document Course ({len(source_docs)} Documents)"
        
        # Ensure reasonable length
        if len(title) > 70:
            title = title[:67] + '...'
        
        logger.info('⚠️ Using fallback title: %s', ("" if title is None else str(title).replace("\n", "").replace("\r", "")))
        return title

def fix_malformed_json(json_string: str) -> str:
    r"""
    Fix ONLY invalid backslash escape sequences. Do NOT touch quotes.
    Valid JSON escapes: backslash-quote, backslash-backslash, backslash-slash,
    backslash-b, backslash-f, backslash-n, backslash-r, backslash-t, \uXXXX
    """
    # Normalise literal newlines/tabs
    cleaned = json_string.replace('\r\n', '\\n').replace('\r', '\\n').replace('\n', '\\n')
    cleaned = cleaned.replace('\t', '\\t')
    
    result = []
    i = 0
    n = len(cleaned)
    in_string = False
    
    while i < n:
        ch = cleaned[i]
        if in_string:
            if ch == '\\':
                if i + 1 < n:
                    nxt = cleaned[i + 1]
                    if nxt in ('"', '\\', '/', 'b', 'f', 'n', 'r', 't'):
                        result.append(ch); result.append(nxt); i += 2
                    elif nxt == 'u' and i + 5 < n:
                        hex_digits = cleaned[i+2:i+6]
                        if len(hex_digits) == 4 and all(c in '0123456789abcdefABCDEF' for c in hex_digits):
                            result.append(ch); result.append(nxt); result.append(hex_digits); i += 6
                        else:
                            result.append('\\\\'); i += 1
                    else:
                        result.append('\\\\'); i += 1
                else:
                    result.append('\\\\'); i += 1
            elif ch == '"':
                in_string = False; result.append(ch); i += 1
            else:
                result.append(ch); i += 1
        else:
            if ch == '"':
                in_string = True; result.append(ch); i += 1
            else:
                result.append(ch); i += 1
    return ''.join(result)


def extract_modules_from_text(response_content: str, doc_name: str) -> list:
    """Last-resort regex extraction of modules from raw LLM response text."""
    import re as _re

    def _read_json_string(s: str, start_idx: int) -> tuple[str, int]:
        """
        Read a JSON string value starting at start_idx (which must point to the opening quote).
        Returns decoded text and the index after the closing quote.
        """
        if start_idx >= len(s) or s[start_idx] != '"':
            return "", start_idx

        i = start_idx + 1
        buf = []
        while i < len(s):
            ch = s[i]
            if ch == '\\' and i + 1 < len(s):
                nxt = s[i + 1]
                if nxt == 'n':
                    buf.append('\n')
                elif nxt == 't':
                    buf.append('\t')
                elif nxt == 'r':
                    buf.append('\r')
                elif nxt == '"':
                    buf.append('"')
                elif nxt == '\\':
                    buf.append('\\')
                else:
                    buf.append(nxt)
                i += 2
            elif ch == '"':
                return ''.join(buf), i + 1
            else:
                buf.append(ch)
                i += 1
        return ''.join(buf), i

    modules = []
    logger.warning("[extract_modules_from_text] Attempting regex extraction for '%s'", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))

    # Try seg_index-based extraction first
    seg_hits = list(_re.finditer(r'"seg_index"\s*:\s*(\d+)', response_content))
    if seg_hits:
        for idx, hit in enumerate(seg_hits):
            seg_idx = int(hit.group(1))
            chunk_start = hit.start()
            chunk_end = seg_hits[idx + 1].start() if idx + 1 < len(seg_hits) else len(response_content)
            chunk = response_content[chunk_start:chunk_end]
            title = f"Module {seg_idx}"
            tm = _re.search(r'"title"\s*:\s*"([^"]{1,300})"', chunk)
            if tm:
                title = tm.group(1)
            content = ""
            cm = _re.search(r'"content"\s*:\s*"', chunk)
            if cm:
                j = cm.end()
                buf = []
                while j < len(chunk):
                    c = chunk[j]
                    if c == '\\' and j + 1 < len(chunk):
                        nxt = chunk[j + 1]
                        if nxt == 'n':
                            buf.append('\n')
                        elif nxt == 't':
                            buf.append('\t')
                        elif nxt == 'r':
                            buf.append('\r')
                        elif nxt == '"':
                            buf.append('"')
                        elif nxt == '\\':
                            buf.append('\\')
                        else:
                            buf.append(nxt)
                        j += 2
                    elif c == '"':
                        break
                    else:
                        buf.append(c)
                        j += 1
                content = ''.join(buf)
            if content or title != f"Module {seg_idx}":
                modules.append({"seg_index": seg_idx, "title": title, "content": content})
        if modules:
            logger.info("[extract_modules_from_text] Extracted %s modules via seg_index for '%s'", len(modules), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            return modules

    # Fallback 2: title/content object extraction without seg_index
    title_hits = list(_re.finditer(r'"title"\s*:\s*"', response_content))
    if title_hits:
        for i, title_hit in enumerate(title_hits, 1):
            title_start_quote = title_hit.end() - 1
            title, after_title = _read_json_string(response_content, title_start_quote)
            if not title:
                continue

            content_key_match = _re.search(r'"content"\s*:\s*"', response_content[after_title:])
            if not content_key_match:
                continue

            content_start_quote = after_title + content_key_match.end() - 1
            content, _ = _read_json_string(response_content, content_start_quote)

            if content.strip():
                modules.append({"seg_index": len(modules) + 1, "title": title.strip(), "content": content})

        if modules:
            logger.info("[extract_modules_from_text] Extracted %s modules via title/content fallback for '%s'", len(modules), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            return modules

    # Fallback: look for numbered sections or headings
    section_hits = list(_re.finditer(r'(?:^|\n)#+\s+(.+)|(?:^|\n)(\d+)\.\s+(.+)', response_content))
    for i, m in enumerate(section_hits):
        title = (m.group(1) or m.group(3) or f"Section {i+1}").strip()
        start = m.end()
        end = section_hits[i + 1].start() if i + 1 < len(section_hits) else len(response_content)
        content = response_content[start:end].strip()
        if content:
            modules.append({"seg_index": i + 1, "title": title, "content": content})

    logger.info("[extract_modules_from_text] Extracted %s modules via fallback for '%s'", len(modules), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
    return modules


def aggressive_json_cleanup(raw_response: str) -> list:
    """Bypass json.loads — extract modules via regex. Returns list of dicts."""
    import re as _re

    def _read_json_string(s: str, start_idx: int) -> tuple[str, int]:
        if start_idx >= len(s) or s[start_idx] != '"':
            return "", start_idx
        i = start_idx + 1
        buf = []
        while i < len(s):
            ch = s[i]
            if ch == '\\' and i + 1 < len(s):
                nxt = s[i + 1]
                if nxt == 'n':
                    buf.append('\n')
                elif nxt == 't':
                    buf.append('\t')
                elif nxt == 'r':
                    buf.append('\r')
                elif nxt == '"':
                    buf.append('"')
                elif nxt == '\\':
                    buf.append('\\')
                else:
                    buf.append(nxt)
                i += 2
            elif ch == '"':
                return ''.join(buf), i + 1
            else:
                buf.append(ch)
                i += 1
        return ''.join(buf), i

    modules = []
    seg_hits = list(_re.finditer(r'"seg_index"\s*:\s*(\d+)', raw_response))
    if seg_hits:
        for idx, hit in enumerate(seg_hits):
            seg_idx = int(hit.group(1))
            chunk_start = hit.start()
            chunk_end = seg_hits[idx + 1].start() if idx + 1 < len(seg_hits) else len(raw_response)
            chunk = raw_response[chunk_start:chunk_end]
            title = f"Module {seg_idx}"
            tm = _re.search(r'"title"\s*:\s*"([^"]{1,300})"', chunk)
            if tm:
                title = tm.group(1)
            content = ""
            cm = _re.search(r'"content"\s*:\s*"', chunk)
            if cm:
                content_start = cm.end() - 1
                content, _ = _read_json_string(chunk, content_start)
            if content or title != f"Module {seg_idx}":
                modules.append({"seg_index": seg_idx, "title": title, "content": content})
        return modules

    # If seg_index is missing, recover from title/content pairs.
    title_hits = list(_re.finditer(r'"title"\s*:\s*"', raw_response))
    for title_hit in title_hits:
        title_start = title_hit.end() - 1
        title, after_title = _read_json_string(raw_response, title_start)
        if not title:
            continue
        content_match = _re.search(r'"content"\s*:\s*"', raw_response[after_title:])
        if not content_match:
            continue
        content_start = after_title + content_match.end() - 1
        content, _ = _read_json_string(raw_response, content_start)
        if content.strip():
            modules.append({
                "seg_index": len(modules) + 1,
                "title": title.strip(),
                "content": content
            })

    return modules


def fix_malformed_json(json_string: str) -> str:
    r"""
    Fix invalid backslash escape sequences inside JSON string values.
    Only operates on content inside quoted strings — never touches structural whitespace.
    Valid JSON escapes: \" \\ \/ \b \f \n \r \t \uXXXX
    """
    result = []
    i = 0
    n = len(json_string)
    in_string = False

    while i < n:
        ch = json_string[i]
        if in_string:
            if ch == '\\':
                if i + 1 < n:
                    nxt = json_string[i + 1]
                    if nxt in ('"', '\\', '/', 'b', 'f', 'n', 'r', 't'):
                        # Valid escape — keep as-is
                        result.append(ch); result.append(nxt); i += 2
                    elif nxt == 'u' and i + 5 < n and all(
                        c in '0123456789abcdefABCDEF' for c in json_string[i+2:i+6]
                    ):
                        # Valid unicode escape
                        result.append(ch); result.append(nxt)
                        result.append(json_string[i+2:i+6]); i += 6
                    else:
                        # Invalid escape — double the backslash
                        result.append('\\\\'); i += 1
                else:
                    result.append('\\\\'); i += 1
            elif ch == '"':
                # End of string
                in_string = False; result.append(ch); i += 1
            elif ch == '\n':
                # Literal newline inside a string — escape it
                result.append('\\n'); i += 1
            elif ch == '\r':
                result.append('\\r'); i += 1
            elif ch == '\t':
                result.append('\\t'); i += 1
            else:
                result.append(ch); i += 1
        else:
            # Outside a string — pass through unchanged (including whitespace)
            if ch == '"':
                in_string = True; result.append(ch); i += 1
            else:
                result.append(ch); i += 1

    return ''.join(result)


def aggressive_json_cleanup(raw_response: str) -> list:
    """Bypass json.loads — extract modules via regex. Returns list of dicts."""
    import re as _re
    modules = []
    seg_hits = list(_re.finditer(r'"seg_index"\s*:\s*(\d+)', raw_response))
    if not seg_hits:
        return modules
    for idx, hit in enumerate(seg_hits):
        seg_idx = int(hit.group(1))
        chunk_start = hit.start()
        chunk_end = seg_hits[idx + 1].start() if idx + 1 < len(seg_hits) else len(raw_response)
        chunk = raw_response[chunk_start:chunk_end]
        title = f"Module {seg_idx}"
        tm = _re.search(r'"title"\s*:\s*"([^"]{1,300})"', chunk)
        if tm:
            title = tm.group(1)
        content = ""
        cm = _re.search(r'"content"\s*:\s*"', chunk)
        if cm:
            j = cm.end()
            buf = []
            while j < len(chunk):
                c = chunk[j]
                if c == '\\' and j + 1 < len(chunk):
                    nxt = chunk[j + 1]
                    if nxt == 'n':
                        buf.append('\n')
                    elif nxt == 't':
                        buf.append('\t')
                    elif nxt == 'r':
                        buf.append('\r')
                    elif nxt == '"':
                        buf.append('"')
                    elif nxt == '\\':
                        buf.append('\\')
                    else:
                        buf.append(nxt)
                    j += 2
                elif c == '"':
                    break
                else:
                    buf.append(c)
                    j += 1
            content = ''.join(buf)
        if content or title != f"Module {seg_idx}":
            modules.append({"seg_index": seg_idx, "title": title, "content": content})
    return modules


def _extract_user_instruction_block(full_prompt: str) -> Optional[str]:
    """
    Extract the section after:
      User's Custom Instructions: --- ... ---
      or User's New Instructions for Improvement: --- ... ---
    Linear string scan (no ReDoS / Sonar S2631).
    Returns the inner text (stripped), or None if the section is absent.
    """
    if not full_prompt:
        return None
    headers = (
        "User's Custom Instructions:",
        "User's New Instructions for Improvement:",
    )
    header_at = -1
    header_len = 0
    for header in headers:
        idx = full_prompt.find(header)
        if idx < 0:
            continue
        if header_at < 0 or idx < header_at:
            header_at = idx
            header_len = len(header)
    if header_at < 0:
        return None

    i = header_at + header_len
    n = len(full_prompt)
    while i < n and full_prompt[i].isspace():
        i += 1
    if i + 3 > n or full_prompt[i : i + 3] != "---":
        return None
    i += 3
    while i < n and full_prompt[i].isspace():
        i += 1
    content_start = i

    close = full_prompt.find("---", content_start)
    if close < 0:
        return None
    return full_prompt[content_start:close].strip()


async def execute_agent_on_text(agent_to_use, full_prompt: str, doc_name: str, job_id: str, display_name: Optional[str] = None) -> List[Dict]:
    from services.agents import (
        answer_validation_agent,
        custom_instruction_agent,
        question_generation_agent,
        regeneration_agent,
    )

    # doc_name is the randomized storage name (VAPT #4); progress messages are read by a
    # human, so they quote the filename that was uploaded.
    doc_label = display_name or doc_name

    def update_progress(message: str):
        raise_if_job_cancelled(job_id)
        update_job_status(job_id, progress_message=message)
        logger.info('Job %s [%s]: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")), ("" if datetime.now(timezone.utc).strftime('%H:%M:%S') is None else str(datetime.now(timezone.utc).strftime('%H:%M:%S')).replace("\n", "").replace("\r", "")), ("" if message is None else str(message).replace("\n", "").replace("\r", "")))

    try:
        # Show that AI is actively working with document size info
        doc_size_kb = len(full_prompt) / 1024
        estimated_time = "30-60 seconds" if doc_size_kb < 50 else "1-2 minutes" if doc_size_kb < 100 else "2-4 minutes"
        
        # Try to detect requested module count from the prompt
        import re
        
        requested_modules = None
        
        # First, look for explicit CRITICAL INSTRUCTION (from our own system)
        # Be EXTREMELY specific to match only our actual CRITICAL INSTRUCTION, not examples or other text
        # Only match the exact format we create in process_document_to_reports_async
        critical_patterns = [
            r'\*\*CRITICAL INSTRUCTION\*\*:\s*The user has EXPLICITLY requested EXACTLY\s+(\d+)\s+module\(s\)\.\s*You MUST generate EXACTLY\s+\d+\s+module\(s\),\s*NO MORE,\s*NO LESS\.\s*This is a strict requirement',
        ]
        
        requested_modules = None
        for pattern in critical_patterns:
            critical_match = re.search(pattern, full_prompt, re.IGNORECASE | re.DOTALL)
            if critical_match:
                requested_modules = int(critical_match.group(1))
                logger.info('✓ Detected module count from CRITICAL INSTRUCTION: %s using pattern: %s', ("" if requested_modules is None else str(requested_modules).replace("\n", "").replace("\r", "")), ("" if pattern is None else str(pattern).replace("\n", "").replace("\r", "")))
                break
        
        # PRIORITY 1: Extract user's custom instructions from the prompt (this should take precedence)
        user_instruction = _extract_user_instruction_block(full_prompt)
        if user_instruction is not None:
            logger.info('📝 Found user instruction in prompt: %s...', ("" if user_instruction[:150] is None else str(user_instruction[:150]).replace("\n", "").replace("\r", "")))
            
            # Use LLM to extract module count from user instruction
            user_requested_modules = await asyncio.to_thread(extract_module_count_with_llm, user_instruction)
            if user_requested_modules:
                logger.info('✓ LLM extracted module count from user instruction: %s', ("" if user_requested_modules is None else str(user_requested_modules).replace("\n", "").replace("\r", "")))
                requested_modules = user_requested_modules  # User instruction takes priority
            else:
                logger.info("✗ LLM could not extract module count from user instruction")
        else:
            logger.info("✗ No user instruction section found in prompt")
        
        # PRIORITY 2: Only check CRITICAL INSTRUCTION if no user instruction was found
        if not requested_modules:
            logger.info("✗ No user-requested module count found, checking for CRITICAL INSTRUCTION")
            for pattern in critical_patterns:
                critical_match = re.search(pattern, full_prompt, re.IGNORECASE | re.DOTALL)
                if critical_match:
                    requested_modules = int(critical_match.group(1))
                    logger.info('✓ Detected module count from CRITICAL INSTRUCTION: %s using pattern: %s', ("" if requested_modules is None else str(requested_modules).replace("\n", "").replace("\r", "")), ("" if pattern is None else str(pattern).replace("\n", "").replace("\r", "")))
                    break
        
        if requested_modules:
            update_progress(f"🤖 AI is generating {requested_modules} module(s) for '{doc_label}' ({doc_size_kb:.1f} KB)... Estimated time: {estimated_time}")
        else:
            update_progress(f"🤖 AI is generating modules for '{doc_label}' ({doc_size_kb:.1f} KB)... Estimated time: {estimated_time}")
        
        # Run AI generation in background while showing progress updates
        async def run_agent_with_progress():
            # Start the agent in a separate task
            agent_task = asyncio.create_task(asyncio.to_thread(agent_to_use.run, full_prompt))
            
            # Show progress updates while AI is working
            progress_messages = [
                "🤖 AI is analyzing document structure...",
                "🤖 AI is extracting key information...",
                "🤖 AI is organizing content into modules...",
                "🤖 AI is finalizing module structure...",
            ]
            
            if requested_modules:
                # Show simulated per-module progress
                progress_messages = []
                for i in range(1, requested_modules + 1):
                    progress_messages.append(f"🤖 AI is working on module {i} of {requested_modules}...")
            
            message_index = 0
            while not agent_task.done():
                await asyncio.sleep(8)  # Update every 8 seconds
                if not agent_task.done() and message_index < len(progress_messages):
                    update_progress(progress_messages[message_index])
                    message_index += 1
                elif not agent_task.done():
                    # Cycle back or show generic message
                    if requested_modules:
                        update_progress(f"🤖 AI is still generating {requested_modules} module(s)... Almost done!")
                    else:
                        update_progress(f"🤖 AI is still working... Almost done!")
            
            return await agent_task
        
        agent_response = await run_agent_with_progress()
        response_content = agent_response.content.strip()
        
        update_progress(f"✓ AI completed generation for '{doc_label}'. Processing response...")

        # Check if the agent is one that is supposed to return JSON
        if agent_to_use in [custom_instruction_agent, regeneration_agent, question_generation_agent, answer_validation_agent]:
            # Remove markdown code blocks if present (```json ... ```)
            if '```json' in response_content:
                # Extract content between ```json and ```
                start_marker = '```json'
                end_marker = '```'
                start_idx = response_content.find(start_marker)
                if start_idx != -1:
                    start_idx += len(start_marker)
                    end_idx = response_content.find(end_marker, start_idx)
                    if end_idx != -1:
                        response_content = response_content[start_idx:end_idx].strip()
                        logger.info("Extracted JSON from markdown code block for '%s'", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            elif '```' in response_content:
                # Handle generic code blocks
                response_content = response_content.replace('```', '').strip()
                logger.info("Removed generic code block markers for '%s'", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            
            # Find the start of the JSON array or object
            json_start = -1
            if '[' in response_content:
                json_start = response_content.find('[')
            if '{' in response_content and (json_start == -1 or response_content.find('{') < json_start):
                 json_start = response_content.find('{')

            if json_start == -1:
                logger.error("Agent did not return a valid JSON structure for '%s'. Raw response: %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if response_content is None else str(response_content).replace("\n", "").replace("\r", "")))
                raise ValueError("Failed to generate modules from agent due to invalid format.")
            
            json_string = response_content[json_start:]
            
            # Find the end of the JSON structure
            if json_string.startswith('['):
                # Find matching closing bracket
                bracket_count = 0
                for i, char in enumerate(json_string):
                    if char == '[':
                        bracket_count += 1
                    elif char == ']':
                        bracket_count -= 1
                        if bracket_count == 0:
                            json_string = json_string[:i+1]
                            break
            elif json_string.startswith('{'):
                # Find matching closing brace
                brace_count = 0
                for i, char in enumerate(json_string):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            json_string = json_string[:i+1]
                            break
            
            # PRE-PROCESS: Fix invalid escape sequences BEFORE attempting json.loads
            # CRITICAL: Strip leading/trailing whitespace FIRST so fix_malformed_json
            # doesn't convert leading newlines into \n text outside a string
            json_string = json_string.strip()
            # Also strip any remaining markdown artifacts
            if json_string.startswith('`'):
                json_string = json_string.lstrip('`').strip()
            json_string = fix_malformed_json(json_string)
            # Strip again after fix — fix_malformed_json may have introduced leading \n text
            json_string = json_string.strip()
            # If the string starts with literal \n or \r (two chars), strip them
            while json_string.startswith('\\n') or json_string.startswith('\\r') or json_string.startswith('\\t'):
                json_string = json_string[2:].strip()
            # Log what we're about to parse
            logger.info('[JSON PARSE] Attempting to parse %s chars, starts with: %s', len(json_string), ("" if repr(json_string[:30]) is None else str(repr(json_string[:30])).replace("\n", "").replace("\r", "")))
            
            # Try to parse JSON with better error handling
            try:
                modules_data = json.loads(json_string, strict=False)
            except json.JSONDecodeError as json_error:
                logger.warning("Initial JSON parsing failed for '%s': %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if json_error is None else str(json_error).replace("\n", "").replace("\r", "")))
                # Try aggressive cleanup — returns a list directly
                try:
                    modules_data = aggressive_json_cleanup(json_string)
                    if modules_data:
                        logger.info("Successfully extracted %s modules via aggressive cleanup for '%s'", len(modules_data), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                    else:
                        raise ValueError("Aggressive cleanup returned empty result")
                except Exception as fix_error:
                    logger.error("Failed to fix and parse JSON for '%s': %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if fix_error is None else str(fix_error).replace("\n", "").replace("\r", "")))
                    # As a last resort, try to extract modules using regex
                    modules_data = extract_modules_from_text(response_content, doc_name)
                    if not modules_data:
                        raise json_error  # Re-raise original error if all fixes fail
            
            if not isinstance(modules_data, (list, dict)): 
                raise ValueError("Parsed JSON from agent is not a list or dictionary.")
        else:
            try:
                modules_data = json.loads(fix_malformed_json(response_content), strict=False)
            except json.JSONDecodeError as json_error:
                logger.warning("JSON parsing failed for '%s': %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if json_error is None else str(json_error).replace("\n", "").replace("\r", "")))
                try:
                    modules_data = aggressive_json_cleanup(response_content)
                    if not modules_data:
                        raise ValueError("Empty result")
                    logger.info("Successfully extracted modules via aggressive cleanup for '%s'", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                except Exception:
                    modules_data = extract_modules_from_text(response_content, doc_name)
                    if not modules_data:
                        raise json_error

        # This part is specific to module generation agents
        if agent_to_use in [custom_instruction_agent, regeneration_agent]:
            all_reports = []
            total_modules = len(modules_data)
            
            # Show clear message about total modules generated
            update_progress(f"✓ AI generated {total_modules} module(s) for '{doc_label}'. Now processing each module...")
            
            # Add a delay to ensure this message is visible (frontend polls every 1 second)
            await asyncio.sleep(1.2)
            
            for i, module in enumerate(modules_data, 1):
                if "title" not in module or "content" not in module:
                    logger.warning("Skipping a module for '%s' due to missing 'title' or 'content' keys.", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                    continue
                
                # Show progress for each module being processed with title truncated
                module_title = module.get('title', f'Module {i}')
                title_preview = module_title[:50] + '...' if len(module_title) > 50 else module_title
                
                # Clear, prominent progress message
                update_progress(f"📝 Processing Module {i} of {total_modules}: {title_preview}")
                
                # Delay to ensure frontend polling catches the update (frontend polls every 1 second)
                await asyncio.sleep(1.2)
                
                raw_content = module.get("content", "No content generated.")
                raw_content = deduplicate_images_in_module(raw_content)
                raw_content = fix_inverted_image_and_heading_blocks(raw_content)

                all_reports.append({ 
                    "report_id": str(uuid.uuid4()), 
                    "doc_name": doc_name, 
                    # Uploaded filename, for the curation UI only. doc_name is the
                    # randomized storage name (VAPT #4) and stays the join key.
                    "doc_display_name": doc_label,
                    "seg_idx": i,  # Local index within this document
                    "title": module.get("title", f"Untitled Module {i}"), 
                    "edited_content": raw_content
                })
            
            update_progress(f"✅ Successfully generated and saved {total_modules} module(s) for '{doc_label}'!")
            
            # Track module text tokens for publish-time analytics
            from services.token_tracking import (
                estimate_tokens_from_words,
                record_module_text_tokens,
            )

            estimated_tokens = estimate_tokens_from_words(
                f"{full_prompt}\n{response_content}"
            )
            record_module_text_tokens(estimated_tokens, job_id=job_id, doc_name=doc_name)
            logger.info(
                "Estimated %s module text tokens for generating %s modules",
                estimated_tokens,
                total_modules,
            )
            
            return all_reports
        else: # For other agents that might return a different structure
            return modules_data

    except json.JSONDecodeError as e:
        logger.error("Failed to parse JSON from agent for '%s': %s. Raw response: %s...", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), e, ("" if response_content[:1000] is None else str(response_content[:1000]).replace("\n", "").replace("\r", "")))
        
        # Try one more time with the fallback extraction method
        try:
            modules_data = extract_modules_from_text(response_content, doc_name)
            if modules_data:
                logger.info("Successfully recovered %s modules using fallback extraction for '%s'", len(modules_data), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                # Continue with the recovered data
                if agent_to_use in [custom_instruction_agent, regeneration_agent]:
                    all_reports = []
                    total_modules = len(modules_data)
                    
                    update_progress(f"✓ AI generated {total_modules} module(s) for '{doc_label}'. Now processing each module...")
                    await asyncio.sleep(1.2)
                    
                    for i, module in enumerate(modules_data, 1):
                        if "title" not in module or "content" not in module:
                            logger.warning("Skipping a module for '%s' due to missing 'title' or 'content' keys.", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                            continue
                        
                        module_title = module.get('title', f'Module {i}')
                        title_preview = module_title[:50] + '...' if len(module_title) > 50 else module_title
                        update_progress(f"📝 Processing Module {i} of {total_modules}: {title_preview}")
                        await asyncio.sleep(1.2)
                        
                        raw_content = module.get("content", "No content generated.")
                        raw_content = deduplicate_images_in_module(raw_content)
                        raw_content = fix_inverted_image_and_heading_blocks(raw_content)

                        all_reports.append({ 
                            "report_id": str(uuid.uuid4()), 
                            "doc_name": doc_name, 
                            "doc_display_name": doc_label,
                            "seg_idx": i, 
                            "title": module.get("title", f"Untitled Module {i}"), 
                            "edited_content": raw_content
                        })
                    
                    update_progress(f"✅ Successfully generated and saved {total_modules} module(s) for '{doc_label}'!")
                    return all_reports
                else:
                    return modules_data
            else:
                raise ValueError("Failed to parse response from module generation agent.")
        except Exception as fallback_error:
            logger.error("Fallback extraction also failed for '%s': %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if fallback_error is None else str(fallback_error).replace("\n", "").replace("\r", "")))
            raise ValueError("Failed to parse response from module generation agent.")
    except Exception as e:
        logger.error("Error executing agent for '%s': %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise ValueError(f"An error occurred while processing instructions for '{doc_label}'.")


def parse_per_document_instructions(user_prompt: str, doc_name: str) -> Optional[str]:
    """
    Extract the instruction that applies to a specific document from the user's prompt.

    Supports ALL file formats: PDF, DOCX, PPTX, DOC, PPT, PNG, JPG, JPEG, etc.

    Supported prompt formats:
      Per-document (colon style):
        'For Speech Therapy.pdf: Create 3 modules. For Acupuncture.pdf: Create 2 modules.'
        'For Speech Therapy: Create 3 modules. For Acupuncture: Create 2 modules.'
      Per-document (natural language):
        "Based on the provided 'mcp.pdf', please develop four separate modules..."
        "Referring to the 'citi_arch.docx', please create two more modules..."
      Global (applies to every document):
        'Create 3 modules focusing on key concepts'
        'Extract financial metrics and performance indicators'

    Returns:
      - The specific instruction string for this document (per-doc or global)
      - None if the prompt has per-doc format but this document is not mentioned
    """
    if not user_prompt:
        return None

    prompt = user_prompt.strip()

    # Build candidate names to try (most-specific first).
    # Swap underscores <-> spaces so 'Speech_Therapy' matches 'Speech Therapy' and vice versa.
    doc_name_no_ext = doc_name.rsplit('.', 1)[0] if '.' in doc_name else doc_name
    doc_name_space = doc_name.replace('_', ' ')
    doc_name_no_ext_space = doc_name_no_ext.replace('_', ' ')

    candidates = []
    for name in [doc_name, doc_name_space, doc_name_no_ext, doc_name_no_ext_space]:
        if name and name not in candidates:
            candidates.append(name)

    file_ext = doc_name.rsplit('.', 1)[1].upper() if '.' in doc_name else 'UNKNOWN'
    logger.info("[PER-DOC INSTRUCTIONS] Parsing for '%s' (%s), candidates: %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if file_ext is None else str(file_ext).replace("\n", "").replace("\r", "")), ("" if candidates is None else str(candidates).replace("\n", "").replace("\r", "")))
    logger.info('[PER-DOC INSTRUCTIONS] Prompt (first 300): %s', ("" if prompt[:300] is None else str(prompt[:300]).replace("\n", "").replace("\r", "")))

    def _cleanup_instruction_text(text: str) -> Optional[str]:
        """
        Normalize extracted per-document text and remove global trailing totals like
        "in total I need only five modules" that should not be applied per file.
        """
        if not text:
            return None

        cleaned = text.strip()
        cleaned = re.sub(r'\s*(additionally|and\s+also|also)\s*$', '', cleaned, flags=re.IGNORECASE).strip()
        # Strip a trailing bridge phrase before the next file's "referring to" clause was cut off
        cleaned = re.sub(
            r'\s*(Additionally|Plus|Also),?\s*$',
            '',
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

        # Remove trailing global total clause in mixed prompts, e.g.
        # "... for this I need one module in total I need five modules"
        cleaned = re.sub(
            r'\s+in\s+total\s+i\s+(?:need|want)\s+.*$',
            '',
            cleaned,
            flags=re.IGNORECASE | re.DOTALL
        ).strip(" .,-")

        return cleaned if cleaned else None

    # Boundary lookahead: stop at the next *per-document* lead-in or end of string.
    # IMPORTANT: Do not use ``\bfor\b\s+...`` here — it matches common English such as
    # "one for each of the following" and truncates the instruction for the first file.
    BOUNDARY = (
        r'(?='
        r'\bbased\s+on\s+the\s+provided\b'
        r'|\breferring\s+to\s+the\b'
        r'|\band\s+referring\s+to\s+the\b'
        # Next explicit file clause, e.g. ``For Report.pdf:`` or ``For "my doc.docx":``
        r'|\bfor\s+[\'"][^\'"\n]{0,240}?\.(?:pdf|docx|pptx|png|jpe?g|doc|ppt)[\'"]?\s*:'
        r'|\bfor\s+[A-Za-z0-9_.\- ]{1,240}\.(?:pdf|docx|pptx|png|jpe?g|doc|ppt)\s*:'
        r'|\Z)'
    )

    def _extract(pattern: str) -> Optional[str]:
        m = re.search(pattern, prompt, re.IGNORECASE | re.DOTALL)
        if m:
            return _cleanup_instruction_text(m.group(1))
        return None

    # -----------------------------------------------------------------------
    # PASS 1: Per-document matching — try every candidate name
    # -----------------------------------------------------------------------
    for name in candidates:
        esc = re.escape(name)

        # Style A: 'For <name>: <instruction>'  (colon separator)
        result = _extract(rf'\bfor\b\s+[\'"\s]*{esc}[\'"\s]*:\s*(.+?){BOUNDARY}')
        if result:
            logger.info("[PER-DOC INSTRUCTIONS] Colon-style match for '%s': %s", ("" if name is None else str(name).replace("\n", "").replace("\r", "")), ("" if result[:120] is None else str(result[:120]).replace("\n", "").replace("\r", "")))
            return result

        # Style B: 'Based on the provided <name>, <instruction>'
        result = _extract(rf'based\s+on\s+the\s+provided\s+[\'"\s]*{esc}[\'"\s]*,?\s*(.+?){BOUNDARY}')
        if result:
            logger.info("[PER-DOC INSTRUCTIONS] Based-on match for '%s': %s", ("" if name is None else str(name).replace("\n", "").replace("\r", "")), ("" if result[:120] is None else str(result[:120]).replace("\n", "").replace("\r", "")))
            return result

        # Style C: 'Referring to the <name>, <instruction>'
        result = _extract(rf'referring\s+to\s+the\s+[\'"\s]*{esc}[\'"\s]*,?\s*(.+?){BOUNDARY}')
        if result:
            logger.info("[PER-DOC INSTRUCTIONS] Referring-to match for '%s': %s", ("" if name is None else str(name).replace("\n", "").replace("\r", "")), ("" if result[:120] is None else str(result[:120]).replace("\n", "").replace("\r", "")))
            return result

        # Style D: 'For <name> <instruction>' (no colon — looser match)
        result = _extract(rf'\bfor\b\s+[\'"\s]*{esc}[\'"\s]*\s+(.+?){BOUNDARY}')
        if result:
            logger.info("[PER-DOC INSTRUCTIONS] Loose-for match for '%s': %s", ("" if name is None else str(name).replace("\n", "").replace("\r", "")), ("" if result[:120] is None else str(result[:120]).replace("\n", "").replace("\r", "")))
            return result

        # Style E: 'document called/named <name> ...' (natural conversational style)
        # Example:
        # I have provided document called "mcp.pdf" ... and I also provided one more
        # document called "optimization.pdf" and for this I need one module.
        doc_called_boundary = (
            r'(?=\band\s+i\s+also\s+provided\b'
            r'|\bi\s+also\s+provided\b'
            r'|\bone\s+more\s+document\s+called\b'
            r'|\banother\s+document\s+called\b'
            r'|\bdocument\s+(?:called|named)\s+[\'"\w]'
            r'|\Z)'
        )
        result = _extract(
            rf'(?:document\s+(?:called|named)\s+[\'"\s]*{esc}[\'"\s]*\s*(?:is|it|,)?\s*)(.+?){doc_called_boundary}'
        )
        if result:
            logger.info("[PER-DOC INSTRUCTIONS] Document-called match for '%s': %s", ("" if name is None else str(name).replace("\n", "").replace("\r", "")), ("" if result[:120] is None else str(result[:120]).replace("\n", "").replace("\r", "")))
            return result

    # -----------------------------------------------------------------------
    # PASS 2: Detect whether the prompt is per-doc format at all.
    # If yes  -> this doc was not mentioned -> return None (use default processing)
    # If no   -> the whole prompt is a global instruction -> return it as-is
    # -----------------------------------------------------------------------
    per_doc_signal = re.search(
        r'(?:\bfor\b\s+[^:]+:)'
        r'|(?:based\s+on\s+the\s+provided)'
        r'|(?:referring\s+to\s+the)'
        r'|(?:document\s+(?:called|named))',
        prompt,
        re.IGNORECASE
    )

    if per_doc_signal:
        logger.warning(
            f"[PER-DOC INSTRUCTIONS] Per-doc format detected but '{doc_name}' not mentioned. "
            f"Using default processing for this document."
        )
        return None

    # Global instruction — applies to every document
    logger.info("[PER-DOC INSTRUCTIONS] Global instruction, applying to '%s'", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
    return prompt


def _extract_docx_plain_text(docx_bytes: bytes) -> str:
    """Extract readable text from DOCX when PDF rendering fails or is unavailable."""
    if not DOCX_AVAILABLE:
        return ""
    try:
        doc = Document(io.BytesIO(docx_bytes))
        parts: list[str] = []
        for paragraph in doc.paragraphs:
            paragraph_text = (paragraph.text or "").strip()
            if paragraph_text:
                parts.append(paragraph_text)
        for table in doc.tables:
            for row in table.rows:
                row_cells = []
                for cell in row.cells:
                    cell_text = (cell.text or "").strip()
                    if cell_text:
                        row_cells.append(cell_text)
                if row_cells:
                    parts.append(" | ".join(row_cells))
        return "\n\n".join(parts)
    except Exception as exc:
        logger.warning("[DOCX] Plain-text fallback failed: %s", log_safe(exc))
        return ""


def _is_group_shape(shape) -> bool:
    """True for a PowerPoint group shape, without importing the enum at module scope."""
    try:
        return bool(getattr(shape, "shapes", None)) and int(
            getattr(shape, "shape_type", -1) or -1
        ) == 6  # MSO_SHAPE_TYPE.GROUP
    except (TypeError, ValueError):
        # shape_type can be None or a non-numeric placeholder on odd shapes; having a
        # .shapes collection is itself sufficient evidence of a group.
        return bool(getattr(shape, "shapes", None))


def _shape_text_lines(shape) -> List[str]:
    """
    Text of one PowerPoint shape, including grouped shapes and tables.

    Plain `shape.text` is empty for a GraphicFrame holding a table and for a group, so a
    slide whose body is a table or a grouped diagram would otherwise read as blank.
    """
    lines: List[str] = []

    # Grouped shapes: recurse, otherwise everything inside the group is lost.
    if _is_group_shape(shape):
        for child in getattr(shape, "shapes", []) or []:
            lines.extend(_shape_text_lines(child))
        return lines

    if getattr(shape, "has_table", False):
        try:
            for row in shape.table.rows:
                cells = [(cell.text or "").strip() for cell in row.cells]
                cells = [c for c in cells if c]
                if cells:
                    lines.append(" | ".join(cells))
        except Exception as exc:  # pragma: no cover - malformed table
            logger.debug("[PPT] Could not read a slide table: %s", log_safe(exc))
        return lines

    if getattr(shape, "has_chart", False):
        try:
            chart_title = shape.chart.chart_title
            if chart_title is not None and chart_title.has_text_frame:
                title_text = (chart_title.text_frame.text or "").strip()
                if title_text:
                    lines.append(f"Chart: {title_text}")
        except Exception as exc:  # pragma: no cover - malformed chart
            logger.debug("[PPT] Could not read a slide chart title: %s", log_safe(exc))

    text = (getattr(shape, "text", "") or "").strip()
    if text:
        lines.append(text)
    return lines


def _count_pptx_slides(pptx_bytes: bytes) -> int:
    """
    Number of slides in a .pptx, or 0 if it cannot be determined.

    Used as the reference for module counting: the count of rendered snapshots is NOT the
    slide count, because LibreOffice's PNG export can return fewer images than the deck has
    slides. Returns 0 for the legacy binary .ppt format, which python-pptx cannot open.
    """
    if not PPTX_AVAILABLE or not pptx_bytes:
        return 0
    try:
        return len(Presentation(io.BytesIO(pptx_bytes)).slides)
    except Exception as exc:
        logger.debug("[PPT] Could not count slides: %s", log_safe(exc))
        return 0


def _count_pdf_pages(pdf_bytes: bytes) -> int:
    """
    Pages in the LibreOffice-converted PDF, or 0 if it cannot be opened.

    One PDF page == one slide, so this is how many slides the PDF route can snapshot. It is
    compared against the direct PNG export so a truncated export does not lock out the fuller
    render.
    """
    if not pdf_bytes:
        return 0
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return len(doc)
    except Exception as exc:
        logger.debug("[PPT] Could not count PDF pages: %s", log_safe(exc))
        return 0


def _direct_slide_export_is_truncated(
    exported_count: int, slide_count: int, pdf_page_count: int
) -> bool:
    """
    True when the direct PNG export missed slides that the PDF render would cover.

    LibreOffice's `--convert-to png` often writes only the first slide. Accepting that export
    skipped the PDF page loop, so every slide after the first got no snapshot and the deck was
    processed as a one-slide document. Only switch when the PDF genuinely covers more slides -
    a smaller or equal PDF render would lose images rather than add them.
    """
    if exported_count <= 0 or slide_count <= 0:
        return False
    return exported_count < slide_count and pdf_page_count > exported_count


def _extract_pptx_slide_texts(pptx_bytes: bytes) -> Dict[int, str]:
    """
    Native text of every slide, keyed by 1-based slide number.

    Read straight from the .pptx rather than from the rendered PDF, because slide snapshots
    are images: the text on them is pixels, so without this the model receives only AI
    captions and the actual slide wording never reaches module generation.
    """
    if not PPTX_AVAILABLE or not pptx_bytes:
        return {}
    try:
        prs = Presentation(io.BytesIO(pptx_bytes))
    except Exception as exc:
        logger.warning("[PPT] Could not open presentation for text extraction: %s", log_safe(exc))
        return {}

    slide_texts: Dict[int, str] = {}
    for slide_num, slide in enumerate(prs.slides, 1):
        lines: List[str] = []
        try:
            for shape in slide.shapes:
                lines.extend(_shape_text_lines(shape))
            # Speaker notes usually carry the narration for a sparse slide.
            if getattr(slide, "has_notes_slide", False):
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                if notes:
                    lines.append(f"Speaker notes: {notes}")
        except Exception as exc:
            logger.warning(
                "[PPT] Text extraction failed on slide %s: %s", slide_num, log_safe(exc)
            )
        # Preserve order while dropping shapes that repeat the same string.
        seen: set = set()
        deduped = [ln for ln in lines if ln and not (ln in seen or seen.add(ln))]
        if deduped:
            slide_texts[slide_num] = "\n".join(deduped)
    return slide_texts


def _extract_pptx_plain_text(pptx_bytes: bytes) -> str:
    """Last-resort text extraction when PDF/slide rendering fails on Linux."""
    slide_texts = _extract_pptx_slide_texts(pptx_bytes)
    if not slide_texts:
        return ""
    return "\n\n".join(
        f"Slide {num}\n{slide_texts[num]}" for num in sorted(slide_texts)
    )


def _ensure_office_pdf_content(
    file_content: bytes,
    original_file_bytes: Optional[bytes],
    orig_ext: str,
    doc_name: str,
) -> bytes:
    """Re-convert Office uploads when the stored PDF is missing or unreadable."""
    office_exts = ("doc", "docx", "ppt", "pptx")
    if orig_ext not in office_exts:
        return file_content
    if pdf_bytes_are_usable(file_content):
        return file_content
    if not original_file_bytes:
        raise ValueError(
            f"Could not convert '{doc_name}' to a readable PDF on this server"
        )
    suffix = f".{orig_ext}"
    logger.warning(
        "[OFFICE] Converted PDF unusable for '%s'; re-converting via LibreOffice (%s)",
        log_safe(doc_name),
        orig_ext,
    )
    try:
        if orig_ext in ("doc", "docx"):
            new_content = convert_docx_to_pdf(original_file_bytes, source_suffix=suffix)
        else:
            new_content = convert_pptx_to_pdf(original_file_bytes, source_suffix=suffix)
    except Exception as exc:
        logger.error(
            "[OFFICE] Re-conversion failed for '%s': %s",
            log_safe(doc_name),
            log_safe(exc),
        )
        raise ValueError(
            f"Could not convert '{doc_name}' to a readable PDF on this server"
        ) from exc
    if not pdf_bytes_are_usable(new_content):
        raise ValueError(
            f"Could not convert '{doc_name}' to a readable PDF on this server"
        )
    return new_content


async def process_document_to_reports_async(job_id: str, doc_name: str, file_content: bytes, user_id: str, content_type: str, user_provided_prompt: Optional[str] = None, start_module_number: int = 1, doc_index: int = 0, total_docs: int = 1, original_filename: Optional[str] = None, original_file_bytes: Optional[bytes] = None) -> List[Dict]:
    from services.agents import custom_instruction_agent, report_agent, title_agent

    def update_progress(message: str):
        raise_if_job_cancelled(job_id)
        update_job_status(job_id, progress_message=message)
        logger.info('Job %s [%s]: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")), ("" if datetime.now(timezone.utc).strftime('%H:%M:%S') is None else str(datetime.now(timezone.utc).strftime('%H:%M:%S')).replace("\n", "").replace("\r", "")), ("" if message is None else str(message).replace("\n", "").replace("\r", "")))
    
    # CRITICAL FIX: Use original_filename for instruction matching if provided.
    # The user types the original filename (e.g. "Speech Therapy.pdf") in their prompt,
    # but doc_name is the secure version (e.g. "Speech_Therapy.pdf"). We must match
    # against the original name so per-document instructions are found correctly.
    # Try original_filename first, fall back to doc_name, fall back to name without extension.
    instruction_match_name = original_filename or doc_name

    # doc_name is the randomized storage name (VAPT #4: Double Extension File Upload), which
    # is meaningless to a user watching the progress feed. Every user-visible message quotes
    # this label instead; doc_name stays the key for S3 and metadata.
    doc_label = original_filename or doc_name

    document_specific_prompt = None
    if user_provided_prompt:
        # Try matching with original filename first (most likely to match user's input)
        document_specific_prompt = parse_per_document_instructions(user_provided_prompt, instruction_match_name)
        # If not found and original differs from secure name, also try secure name
        if document_specific_prompt is None and instruction_match_name != doc_name:
            document_specific_prompt = parse_per_document_instructions(user_provided_prompt, doc_name)
        # Also try without extension as a last resort
        if document_specific_prompt is None:
            name_no_ext = instruction_match_name.rsplit('.', 1)[0] if '.' in instruction_match_name else instruction_match_name
            document_specific_prompt = parse_per_document_instructions(user_provided_prompt, name_no_ext)
    
    if document_specific_prompt:
        # Show the specific instruction being applied
        display_instruction = document_specific_prompt[:100] + "..." if len(document_specific_prompt) > 100 else document_specific_prompt
        update_progress(f"📝 '{doc_label}': Using custom instruction - {display_instruction}")
    
    from services.token_tracking import _token_doc_name, _token_job_id

    _job_ctx_token = _token_job_id.set(job_id)
    _doc_ctx_token = _token_doc_name.set(doc_name)
    try:
        # Initialize requested_count to avoid UnboundLocalError
        requested_count = None
        
        update_progress(f"📖 '{doc_label}': Reading and extracting content...")
        text = ""
        # Store images with their context for intelligent mapping
        extracted_images = []  # List of {s3_key, page_num, caption, context_text}
        _source_name = original_filename or doc_name
        _orig_ext = (
            _source_name.rsplit(".", 1)[-1].lower() if "." in _source_name else ""
        )
        _ppt_page_snapshots = _orig_ext in ("ppt", "pptx")
        # The deck's OWN slide count, read before any rendering. Snapshot rendering can
        # return fewer images than there are slides, so this is what module counting and
        # the completeness warnings are measured against.
        _ppt_slide_count = (
            _count_pptx_slides(original_file_bytes)
            if _ppt_page_snapshots and original_file_bytes
            else 0
        )
        _ppt_true_slide_count = _ppt_slide_count
        _docx_selective_snapshots = _orig_ext in ("doc", "docx")
        _docx_primary_text = (
            _extract_docx_plain_text(original_file_bytes)
            if _docx_selective_snapshots and original_file_bytes
            else ""
        )
        if _docx_primary_text.strip():
            text = _docx_primary_text + "\n\n"
            logger.info(
                "[DOCX] Loaded python-docx text (%s chars) for '%s'",
                len(_docx_primary_text),
                log_safe(_source_name),
            )

        office_mimes = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-powerpoint",
            "application/octet-stream",
        )
        if _orig_ext in ("doc", "docx", "ppt", "pptx") and content_type != "application/pdf":
            if original_file_bytes:
                try:
                    file_content, _inline_pdf_name = convert_document_to_pdf(
                        original_file_bytes,
                        original_filename or f"{doc_name}.{_orig_ext}",
                    )
                    content_type = "application/pdf"
                    logger.info(
                        "[OFFICE] Inline conversion succeeded for '%s' (%s bytes)",
                        log_safe(_source_name),
                        len(file_content),
                    )
                except Exception as inline_conv_exc:
                    logger.warning(
                        "[OFFICE] Inline conversion failed for '%s': %s",
                        log_safe(_source_name),
                        log_safe(inline_conv_exc),
                    )
                    if _docx_primary_text.strip():
                        file_content = b""
                    else:
                        raise ValueError(
                            f"Could not convert '{doc_label}' to a readable PDF on this server"
                        ) from inline_conv_exc
            elif not _docx_primary_text.strip():
                raise ValueError(f"Unsupported file type: {content_type}")

        if content_type == "application/pdf" and _orig_ext in ("doc", "docx", "ppt", "pptx"):
            try:
                file_content = _ensure_office_pdf_content(
                    file_content,
                    original_file_bytes,
                    _orig_ext,
                    doc_name,
                )
            except ValueError as office_pdf_exc:
                if _docx_selective_snapshots and _docx_primary_text.strip():
                    logger.warning(
                        "[DOCX] PDF validation failed for '%s'; continuing with docx-native text",
                        log_safe(_source_name),
                    )
                    file_content = b""
                else:
                    raise

        if content_type == 'application/pdf' and file_content:
            if _ppt_page_snapshots:
                logger.info(
                    "[PAGE SNAPSHOT] PPT mode for '%s': one layout snapshot per slide/page",
                    log_safe(_source_name),
                )
            elif _docx_selective_snapshots:
                logger.info(
                    "[PAGE SNAPSHOT] DOCX mode for '%s': snapshots on diagram-heavy pages only",
                    log_safe(_source_name),
                )

            # PPT/PPTX: export each slide as PNG from the original file (best layout fidelity).
            slide_pngs = None
            if _ppt_page_snapshots and original_file_bytes:
                _ppt_suffix = f".{_orig_ext}" if _orig_ext else ".pptx"
                slide_pngs = render_pptx_slides_as_png_list(original_file_bytes, _ppt_suffix)
                # A PARTIAL export must not lock out the PDF page loop below. If the direct
                # export covers fewer slides than the converted PDF has pages, the PDF route
                # gives a snapshot for EVERY slide, so it wins - otherwise every slide after
                # the first got no image at all and the deck read as a one-slide document.
                if slide_pngs:
                    _pdf_pages = _count_pdf_pages(file_content)
                    if _direct_slide_export_is_truncated(
                        len(slide_pngs), _ppt_slide_count or 0, _pdf_pages
                    ):
                        logger.warning(
                            "[PPT SLIDES] Direct export covered %s of %s slide(s) for '%s'; "
                            "using the %s-page PDF render instead",
                            len(slide_pngs),
                            _ppt_slide_count,
                            log_safe(_source_name),
                            _pdf_pages,
                        )
                        slide_pngs = None
                if slide_pngs:
                    total_slides = len(slide_pngs)
                    update_progress(
                        f"📄 '{doc_label}': Processing {total_slides} slide(s) as layout images..."
                    )
                    logger.info(
                        "[PPT SLIDES] Using %s LibreOffice PNG export(s) for '%s'",
                        total_slides,
                        log_safe(_source_name),
                    )
                    for slide_num, png_bytes in enumerate(slide_pngs):
                        slide_label = slide_num + 1
                        slide_context = f"Slide {slide_label} of {total_slides}"
                        update_progress(
                            f"🖼️ '{doc_label}': Slide {slide_label}/{total_slides} (layout snapshot)..."
                        )
                        try:
                            text += _upload_layout_snapshot_image(
                                png_bytes,
                                slide_label,
                                doc_name,
                                user_id,
                                slide_context,
                                extracted_images,
                                snapshot_kind="slide",
                                caption_hint_suffix=f"slide{slide_label}_layout_snapshot",
                            )
                        except JobCancelledError:
                            raise
                        except Exception as slide_exc:
                            logger.warning(
                                "[PPT SLIDES] Slide %s failed for '%s': %s",
                                slide_label,
                                log_safe(_source_name),
                                log_safe(slide_exc),
                            )
                            text += f"\n\nSlide {slide_label} (layout capture unavailable)\n"
                else:
                    logger.warning(
                        "[PPT SLIDES] PNG export failed for '%s'; falling back to PDF page snapshots",
                        log_safe(_source_name),
                    )

            saved_image_xrefs = set()
            saved_s3_keys = set()
            ppt_slides_done = bool(_ppt_page_snapshots and slide_pngs)
            if not ppt_slides_done:
                try:
                    pdf_doc = fitz.open(stream=file_content, filetype="pdf")
                except Exception as pdf_open_exc:
                    logger.error(
                        "Could not open converted PDF for '%s': %s",
                        log_safe(doc_name),
                        log_safe(pdf_open_exc),
                        exc_info=True,
                    )
                    if not text.strip():
                        if _docx_selective_snapshots and original_file_bytes:
                            fallback_text = _extract_docx_plain_text(original_file_bytes)
                            if fallback_text.strip():
                                text = fallback_text + "\n\n"
                                logger.info(
                                    "[DOCX] PDF open failed; using python-docx text fallback for '%s'",
                                    log_safe(_source_name),
                                )
                            else:
                                raise ValueError(
                                    f"Could not read converted PDF for '{doc_label}'"
                                ) from pdf_open_exc
                        elif _ppt_page_snapshots and original_file_bytes:
                            fallback_text = _extract_pptx_plain_text(original_file_bytes)
                            if fallback_text.strip():
                                text = fallback_text + "\n\n"
                                logger.info(
                                    "[PPT] PDF open failed; using python-pptx text fallback for '%s'",
                                    log_safe(_source_name),
                                )
                            else:
                                raise ValueError(
                                    f"Could not read converted PDF for '{doc_label}'"
                                ) from pdf_open_exc
                        else:
                            raise ValueError(
                                f"Could not read converted PDF for '{doc_label}'"
                            ) from pdf_open_exc
                    pdf_doc = None

                if pdf_doc is not None:
                    with pdf_doc as doc:
                        total_pages = len(doc)
                        update_progress(f"📄 '{doc_label}': Processing {total_pages} page(s)...")

                        for page_num, page in enumerate(doc):
                            raise_if_job_cancelled(job_id)
                            # ── Text extraction (filter footnotes/watermarks) ──────────
                            raw_blocks = page.get_text("blocks")
                            page_text_parts = []
                            for b in raw_blocks:
                                txt = b[4].strip() if len(b) > 4 else ""
                                if not _is_footnote_text(txt):
                                    page_text_parts.append(txt)
                            page_text = "\n".join(page_text_parts)

                            if _ppt_page_snapshots:
                                update_progress(
                                    f"🖼️ '{doc_label}': Capturing slide {page_num + 1}/{total_pages} as layout snapshot..."
                                )
                                try:
                                    text += _process_pdf_page_snapshot(
                                        page,
                                        page_num,
                                        doc_name,
                                        user_id,
                                        page_text,
                                        extracted_images,
                                        snapshot_kind="slide",
                                    )
                                except JobCancelledError:
                                    raise
                                except Exception as slide_exc:
                                    logger.warning(
                                        "[PPT SLIDES] PDF slide snapshot failed page %s for '%s': %s",
                                        page_num + 1,
                                        log_safe(doc_name),
                                        log_safe(slide_exc),
                                    )
                                    text += page_text + "\n\n"
                                continue

                            text += page_text + "\n\n"

                            if _docx_selective_snapshots and _docx_pdf_page_needs_snapshot(
                                page, page_num
                            ):
                                update_progress(
                                    f"🖼️ '{doc_label}': Capturing complex page {page_num + 1}/{total_pages} as snapshot..."
                                )
                                try:
                                    text += _process_pdf_page_snapshot(
                                        page,
                                        page_num,
                                        doc_name,
                                        user_id,
                                        page_text,
                                        extracted_images,
                                        snapshot_kind="page",
                                    )
                                except JobCancelledError:
                                    raise
                                except Exception as snap_exc:
                                    logger.warning(
                                        "[DOCX SNAPSHOT] Page %s snapshot failed for '%s': %s",
                                        page_num + 1,
                                        log_safe(doc_name),
                                        log_safe(snap_exc),
                                    )
                                continue

                            # ── PASS 1: Embedded raster images (native PDF path only) ───
                            images = page.get_images(full=True)
                            if images:
                                update_progress(f"🖼️ '{doc_label}': Found {len(images)} embedded image(s) on page {page_num+1}/{total_pages}...")
                            for img_index, img in enumerate(images):
                                xref = img[0]
                                if xref in saved_image_xrefs:
                                    continue
                        
                                try:
                                    img_bbox = page.get_image_bbox(img)
                                    caption = _find_caption_for_image(page, img_bbox)

                                    base_image = doc.extract_image(xref)
                                    image_bytes = base_image["image"]
                                    image_ext = base_image["ext"]
                            
                                    if len(image_bytes) < 1000:
                                        logger.warning('Skipping tiny image (%s bytes) on page %s', len(image_bytes), ("" if page_num+1 is None else str(page_num+1).replace("\n", "").replace("\r", "")))
                                        continue
                            
                                    logger.info('Performing AI analysis for embedded image on page %s', ("" if page_num+1 is None else str(page_num+1).replace("\n", "").replace("\r", "")))
                                    temp_s3_key = f"{user_id}/extracted_images/{secure_filename(doc_name)}/page{page_num+1}_img{img_index}.{image_ext}"
                                    image_analysis = analyze_image_content_with_ai(image_bytes, page_text[:500], temp_s3_key)
                            
                                    # Discard watermarks, logos, and decorative images
                                    if _is_irrelevant_image(image_analysis):
                                        logger.info('[PASS1] Discarding irrelevant/watermark image on page %s: %s', ("" if page_num+1 is None else str(page_num+1).replace("\n", "").replace("\r", "")), ("" if image_analysis.get('caption','') is None else str(image_analysis.get('caption','')).replace("\n", "").replace("\r", "")))
                                        continue

                                    caption = image_analysis.get('caption', caption)
                                    if not caption or len(caption.strip()) < 5:
                                        caption = f"page{page_num+1} img{img_index}"
                            
                                    clean_caption = re.sub(r'[^\w\s-]', '', caption)
                                    clean_caption = re.sub(r'\s+', '_', clean_caption.strip())
                                    image_filename = f"{clean_caption[:50]}.{image_ext}"
                                    s3_image_key = f"{user_id}/extracted_images/{secure_filename(doc_name)}/{secure_filename(image_filename)}"
                                    if s3_image_key in saved_s3_keys:
                                        # Avoid duplicate insertion when captions collide or the same asset repeats.
                                        logger.info(
                                            "[PASS1] Skipping duplicate S3 key on page %s: %s",
                                            page_num + 1,
                                            s3_image_key,
                                        )
                                        saved_image_xrefs.add(xref)
                                        continue
                                    image_analysis['s3_key'] = s3_image_key
                            
                                    s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=s3_image_key, Body=image_bytes, ContentType=f"image/{image_ext}")
                                    save_image_analysis_to_s3(user_id, doc_name, image_analysis)
                            
                                    extracted_images.append({
                                        's3_key': s3_image_key,
                                        'page_num': page_num + 1,
                                        'extraction_index': len(extracted_images),
                                        'caption': caption,
                                        'context_text': page_text[:500],
                                        'ai_analysis': image_analysis,
                                        'keywords': image_analysis.get('keywords', []),
                                        'topics': image_analysis.get('topics', []),
                                        'content_description': image_analysis.get('content', ''),
                                        'image_type': image_analysis.get('type', 'unknown')
                                    })
                                    saved_image_xrefs.add(xref)
                                    saved_s3_keys.add(s3_image_key)
                                    # Preserve document reading order: image markers follow the page text they belong to
                                    text += f"\n[IMAGE: {s3_image_key}]\n"
                                    if caption:
                                        text += f"*{caption}*\n"

                                except Exception as e:
                                    logger.error('Failed to extract embedded image xref %s from page %s: %s', ("" if xref is None else str(xref).replace("\n", "").replace("\r", "")), ("" if page_num+1 is None else str(page_num+1).replace("\n", "").replace("\r", "")), e)

                            # ── PASS 2: Vector diagrams (Gamma / Canva style PDFs) ────
                            # DOCX-derived PDFs frequently contain both embedded rasters and vector clusters
                            # for the same figure/table; extracting both causes duplicates.
                            if _docx_selective_snapshots and images:
                                vector_diagrams = []
                            else:
                                vector_diagrams = _extract_vector_diagrams_from_page(page, page_num)
                            if vector_diagrams:
                                update_progress(f"📐 '{doc_label}': Found {len(vector_diagrams)} vector diagram(s) on page {page_num+1}/{total_pages}...")
                            for vd_index, vd in enumerate(vector_diagrams):
                                try:
                                    image_bytes = vd["image_bytes"]
                                    image_ext   = vd["image_ext"]
                                    caption_hint = vd["caption_hint"]

                                    temp_s3_key = f"{user_id}/extracted_images/{secure_filename(doc_name)}/{caption_hint}.{image_ext}"
                                    image_analysis = analyze_image_content_with_ai(image_bytes, page_text[:500], temp_s3_key)

                                    # Discard watermarks, logos, and decorative vector diagrams
                                    if _is_irrelevant_image(image_analysis):
                                        logger.info('[PASS2] Discarding irrelevant vector diagram on page %s: %s', ("" if page_num+1 is None else str(page_num+1).replace("\n", "").replace("\r", "")), ("" if image_analysis.get('caption','') is None else str(image_analysis.get('caption','')).replace("\n", "").replace("\r", "")))
                                        continue

                                    caption = image_analysis.get('caption') or caption_hint
                                    clean_caption = re.sub(r'[^\w\s-]', '', caption)
                                    clean_caption = re.sub(r'\s+', '_', clean_caption.strip())
                                    image_filename = f"{clean_caption[:50]}.{image_ext}"
                                    s3_image_key = f"{user_id}/extracted_images/{secure_filename(doc_name)}/{secure_filename(image_filename)}"
                                    if s3_image_key in saved_s3_keys:
                                        logger.info(
                                            "[PASS2] Skipping duplicate S3 key on page %s: %s",
                                            page_num + 1,
                                            s3_image_key,
                                        )
                                        continue
                                    image_analysis['s3_key'] = s3_image_key

                                    s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=s3_image_key, Body=image_bytes, ContentType=f"image/{image_ext}")
                                    save_image_analysis_to_s3(user_id, doc_name, image_analysis)

                                    extracted_images.append({
                                        's3_key': s3_image_key,
                                        'page_num': page_num + 1,
                                        'extraction_index': len(extracted_images),
                                        'caption': caption,
                                        'context_text': page_text[:500],
                                        'ai_analysis': image_analysis,
                                        'keywords': image_analysis.get('keywords', []),
                                        'topics': image_analysis.get('topics', []),
                                        'content_description': image_analysis.get('content', ''),
                                        'image_type': 'vector_diagram',
                                    })
                                    logger.info('[VECTOR DIAGRAM] Saved diagram from page %s: %s', ("" if page_num+1 is None else str(page_num+1).replace("\n", "").replace("\r", "")), ("" if s3_image_key is None else str(s3_image_key).replace("\n", "").replace("\r", "")))
                                    saved_s3_keys.add(s3_image_key)
                                    text += f"\n[IMAGE: {s3_image_key}]\n"
                                    if caption:
                                        text += f"*{caption}*\n"

                                except Exception as e:
                                    logger.error('[VECTOR DIAGRAM] Failed to process diagram on page %s: %s', ("" if page_num+1 is None else str(page_num+1).replace("\n", "").replace("\r", "")), e)

        elif (
            _docx_selective_snapshots
            and _docx_primary_text.strip()
            and text.strip()
        ):
            logger.info(
                "[DOCX] Using docx-native text only for '%s' (PDF pipeline skipped)",
                log_safe(_source_name),
            )
        elif content_type.startswith('image/'):
            update_progress(f"Processing uploaded image '{doc_label}'...")
            
            # Save the uploaded image to S3
            image_ext = doc_name.split('.')[-1] if '.' in doc_name else 'png'
            s3_image_key = f"{user_id}/extracted_images/{secure_filename(doc_name)}/{secure_filename(doc_name)}"
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME, 
                Key=s3_image_key, 
                Body=file_content, 
                ContentType=content_type
            )
            logger.info('Uploaded standalone image to S3: %s', ("" if s3_image_key is None else str(s3_image_key).replace("\n", "").replace("\r", "")))
            
            # Extract text from the image using Bedrock
            update_progress(f"Analyzing image content using AI...")
            text = extract_text_from_image_with_bedrock(file_content, is_pdf_extraction=False)
            
            # Add the image to extracted_images list so it's included in modules
            extracted_images.append({
                's3_key': s3_image_key,
                'page_num': 1,
                'caption': doc_name.replace('_', ' ').replace('-', ' ').rsplit('.', 1)[0],
                'context_text': text[:500] if text else "Uploaded image",
                'description': "Source image uploaded by user"
            })
            text = (text or "").rstrip() + f"\n\n[IMAGE: {s3_image_key}]\n"

            logger.info('Successfully processed standalone image: %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
        else:
            raise ValueError(f"Unsupported file type: {content_type}")

        if _ppt_page_snapshots:
            layout_slides = _filter_ppt_layout_slide_images(extracted_images)
            # Slide snapshots are images, so their text is pixels. Read each slide's own
            # words from the .pptx and interleave them, otherwise the assignment below throws
            # away every slide's content and leaves only AI captions - which made a whole
            # deck generate modules from the title slide alone. This runs even with no
            # snapshots at all, so the deck's wording still reaches generation.
            _ppt_slide_texts = _extract_pptx_slide_texts(original_file_bytes or b"")
            # The number of RENDERED snapshots is not the number of slides: LibreOffice can
            # export fewer PNGs than the deck has. Module count must follow the deck.
            _ppt_true_slide_count = max(
                len(_ppt_slide_texts), len(layout_slides), _ppt_slide_count or 0
            )
            if layout_slides or _ppt_slide_texts:
                if layout_slides:
                    extracted_images = layout_slides
                _ppt_prior_text = text
                text = _build_ppt_slide_source_text(layout_slides, _ppt_slide_texts)
                logger.info(
                    "[PPT SLIDES] %s full-slide image(s) and text from %s slide(s) of %s for "
                    "'%s'; module count from your instructions (not 1:1 with slides)",
                    len(layout_slides),
                    len(_ppt_slide_texts),
                    _ppt_true_slide_count,
                    log_safe(_source_name),
                )
                if len(layout_slides) < _ppt_true_slide_count:
                    logger.warning(
                        "[PPT SLIDES] Only %s of %s slides produced a snapshot for '%s'; "
                        "those slides contribute text but no image",
                        len(layout_slides),
                        _ppt_true_slide_count,
                        log_safe(_source_name),
                    )
                if not _ppt_slide_texts:
                    # python-pptx cannot open the legacy binary .ppt format, so keep whatever
                    # the PDF/page pass already extracted rather than replacing real content
                    # with captions only.
                    if _ppt_prior_text.strip():
                        text += (
                            "\n\n--- Presentation text ---\n" + _ppt_prior_text.strip() + "\n"
                        )
                        logger.info(
                            "[PPT SLIDES] No native slide text for '%s'; kept %s chars of "
                            "extracted text",
                            log_safe(_source_name),
                            len(_ppt_prior_text.strip()),
                        )
                    else:
                        logger.warning(
                            "[PPT SLIDES] No native slide text for '%s'; modules will rely on "
                            "slide-image analysis only",
                            log_safe(_source_name),
                        )
            else:
                logger.warning(
                    "[PPT SLIDES] No layout snapshots for '%s'",
                    log_safe(_source_name),
                )

        cleaned_text = clean_text(text)
        if not cleaned_text and original_file_bytes:
            if _ppt_page_snapshots:
                fallback_text = _extract_pptx_plain_text(original_file_bytes)
                if fallback_text.strip():
                    cleaned_text = clean_text(fallback_text)
                    logger.info(
                        "[PPT] Using python-pptx text fallback for '%s'",
                        log_safe(_source_name),
                    )
            elif _docx_selective_snapshots:
                fallback_text = _extract_docx_plain_text(original_file_bytes)
                if fallback_text.strip():
                    cleaned_text = clean_text(fallback_text)
                    logger.info(
                        "[DOCX] Using python-docx text fallback for '%s'",
                        log_safe(_source_name),
                    )
        if not cleaned_text:
            logger.warning("In '%s': No text extracted. Skipping.", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            return []
        base_cleaned_text = cleaned_text

        # CRITICAL FIX: Build image catalog from ACTUAL S3 contents instead of extracted_images
        # This ensures we only reference images that actually exist in S3
        logger.info('[S3 CATALOG] Building image catalog from ACTUAL S3 contents for user %s, doc %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
        actual_s3_images = get_actual_images_from_s3(user_id, doc_name)
        actual_s3_images = order_images_for_module_catalog(extracted_images, actual_s3_images)
        if _ppt_page_snapshots and extracted_images:
            actual_s3_images = _restrict_catalog_to_ppt_layout_images(
                actual_s3_images, extracted_images
            )
        ordered_source_rows = merge_extracted_with_catalog_order(extracted_images, actual_s3_images)
        if not ordered_source_rows and extracted_images:
            ordered_source_rows = list(extracted_images)
        if _ppt_page_snapshots and extracted_images:
            ordered_source_rows = _restrict_catalog_to_ppt_layout_images(
                ordered_source_rows, extracted_images
            )

        if actual_s3_images:
            image_catalog = "\n\n--- AVAILABLE IMAGES ---\n" + IMAGE_CATALOG_ORDER_INSTRUCTION
            for img in actual_s3_images:
                image_catalog += f"[IMAGE: {img['s3_key']}]\n"
                image_catalog += f"Caption: {img['caption']}\n"
                image_catalog += f"Page: {img['page_num']}\n"
                image_catalog += f"Description: Image from {doc_name}, page {img['page_num']}\n\n"
            image_catalog += "--- END OF IMAGES ---\n"
            cleaned_text += image_catalog
            logger.info('✓ Added ACTUAL S3 image catalog with %s images to document text', len(actual_s3_images))
            logger.info('ACTUAL S3 image catalog preview:\\n%s', ("" if image_catalog[:500] is None else str(image_catalog[:500]).replace("\n", "").replace("\r", "")))
            
            # Debug logging for troubleshooting
            logger.info("[ACTUAL S3 CATALOG DEBUG] ===== COMPLETE ACTUAL IMAGE CATALOG =====")
            logger.info('[ACTUAL S3 CATALOG DEBUG] User ID: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
            logger.info('[ACTUAL S3 CATALOG DEBUG] Document name: %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            logger.info('[ACTUAL S3 CATALOG DEBUG] Number of ACTUAL images: %s', len(actual_s3_images))
            for i, img in enumerate(actual_s3_images, 1):
                logger.info('[ACTUAL S3 CATALOG DEBUG] ACTUAL Image %s: %s', ("" if i is None else str(i).replace("\n", "").replace("\r", "")), ("" if img['s3_key'] is None else str(img['s3_key']).replace("\n", "").replace("\r", "")))
                logger.info('[ACTUAL S3 CATALOG DEBUG]   Caption: %s', ("" if img['caption'] is None else str(img['caption']).replace("\n", "").replace("\r", "")))
                logger.info('[ACTUAL S3 CATALOG DEBUG]   Page: %s', ("" if img['page_num'] is None else str(img['page_num']).replace("\n", "").replace("\r", "")))
            logger.info("[ACTUAL S3 CATALOG DEBUG] ===== END ACTUAL CATALOG DEBUG =====")
            logger.info('[ACTUAL S3 CATALOG DEBUG] Full ACTUAL catalog text:\\n%s', ("" if image_catalog is None else str(image_catalog).replace("\n", "").replace("\r", "")))
        else:
            logger.info('✗ No ACTUAL images found in S3 for document %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            # Still add the extracted_images for backward compatibility if S3 scan failed
            if extracted_images:
                logger.warning("[FALLBACK] Using extracted_images as fallback since S3 scan found no images")
                image_catalog = "\n\n--- AVAILABLE IMAGES ---\n" + IMAGE_CATALOG_ORDER_INSTRUCTION
                for img in extracted_images:
                    image_catalog += f"[IMAGE: {img['s3_key']}]\n"
                    image_catalog += f"Caption: {img['caption']}\n"
                    image_catalog += f"Page: {img['page_num']}\n"
                    if img['description']:
                        image_catalog += f"Description: {img['description']}\n"
                    image_catalog += "\n"
                image_catalog += "--- END OF IMAGES ---\n"
                cleaned_text += image_catalog
                logger.info('✓ Added FALLBACK image catalog with %s images to document text', len(extracted_images))

        if document_specific_prompt:
            # Use LLM (Nova Pro) to extract module count from custom instructions
            # This provides better natural language understanding than regex
            logger.info('[CUSTOM INSTRUCTION] Document: %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            logger.info('[CUSTOM INSTRUCTION] Extracted instruction: %s', ("" if document_specific_prompt is None else str(document_specific_prompt).replace("\n", "").replace("\r", "")))
            logger.info("[CUSTOM INSTRUCTION] Using LLM to extract module count...")
            
            requested_count = extract_module_count_with_llm(document_specific_prompt)

            if _ppt_page_snapshots and not requested_count:
                # The deck's slide count, NOT len(extracted_images): a deck whose snapshot
                # export returned one image would otherwise be treated as a one-slide deck
                # and generate a single module from the title slide.
                n_slides = max(_ppt_true_slide_count, len(extracted_images) or 0)
                if n_slides > 1:
                    requested_count = _default_ppt_module_count(n_slides)
                    logger.info(
                        "[PPT] Custom prompt has no module count; using %s modules for %s slides",
                        requested_count,
                        n_slides,
                    )

            if requested_count:
                logger.info("[CUSTOM INSTRUCTION] ✅ Module count extracted: %s for '%s'", ("" if requested_count is None else str(requested_count).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                update_progress(f"📊 '{doc_label}': Custom instruction requests {requested_count} module(s)")
                update_progress(f"Following custom instructions for '{doc_label}' - Generating {requested_count} module(s)...")
                ppt_group_note = (
                    _ppt_group_note(
                        max(_ppt_true_slide_count, len(extracted_images) or 0),
                        len(extracted_images) or 0,
                        requested_count,
                    )
                    if _ppt_page_snapshots
                    else ""
                )
                module_instruction = (
                    f"\n\n**🚨 CRITICAL INSTRUCTION - ABSOLUTE PRIORITY 🚨**: The user has EXPLICITLY requested "
                    f"EXACTLY {requested_count} module(s). You MUST generate EXACTLY {requested_count} module(s), "
                    f"NO MORE, NO LESS.{ppt_group_note} Count your modules before submitting."
                )
            else:
                logger.warning('[CUSTOM INSTRUCTION] ⚠️ Could not extract module count from: %s', ("" if document_specific_prompt is None else str(document_specific_prompt).replace("\n", "").replace("\r", "")))
                update_progress(f"Following custom instructions for '{doc_label}'...")
                module_instruction = ""
                if _ppt_page_snapshots and len(extracted_images) > 1:
                    default_n = _default_ppt_module_count(len(extracted_images))
                    module_instruction = (
                        f"\n\n**🚨 CRITICAL**: This PowerPoint has {len(extracted_images)} slides as full-page "
                        f"images. Create about {default_n} modules by grouping related slides — NOT "
                        f"{len(extracted_images)} modules (one per slide).\n"
                    )
            
            # CRITICAL FIX: Add enhanced image catalog with AI analysis for custom instructions
            # This ensures images are properly mapped even when custom instructions are used
            enhanced_image_catalog = ""
            if extracted_images:
                logger.info('[CUSTOM INSTRUCTION IMAGE MAPPING] Adding %s images with AI analysis to prompt', len(extracted_images))
                enhanced_image_catalog = "\n\n--- AVAILABLE IMAGES ---\n" + IMAGE_CATALOG_ORDER_INSTRUCTION
                enhanced_image_catalog += "**CRITICAL: You MUST use these EXACT image paths. DO NOT create fake paths.**\n"
                enhanced_image_catalog += "**IMPORTANT: Include relevant images in your modules to enhance understanding.**\n\n"

                ordered_prompt_images = list(ordered_source_rows) if ordered_source_rows else []

                for img_data in ordered_prompt_images:
                    s3_key = img_data['s3_key']
                    enhanced_image_catalog += f"[IMAGE: {s3_key}]\n"
                    
                    # Add AI analysis if available (provides better context for mapping)
                    if 'ai_analysis' in img_data:
                        analysis = img_data['ai_analysis']
                        enhanced_image_catalog += f"  Caption: {analysis.get('caption', 'Image')}\n"
                        enhanced_image_catalog += f"  Type: {analysis.get('type', 'unknown')}\n"
                        enhanced_image_catalog += f"  Content: {analysis.get('content', 'No description')}\n"
                        enhanced_image_catalog += f"  Keywords: {', '.join(analysis.get('keywords', []))}\n"
                        enhanced_image_catalog += f"  Topics: {', '.join(analysis.get('topics', []))}\n"
                    else:
                        # Fallback for images without AI analysis
                        enhanced_image_catalog += f"  Caption: {img_data.get('caption', 'Image')}\n"
                    
                    if 'page_num' in img_data:
                        enhanced_image_catalog += f"  Page: {img_data['page_num']}\n"
                    
                    enhanced_image_catalog += "\n"
                
                enhanced_image_catalog += "--- END OF AVAILABLE IMAGES ---\n"
                enhanced_image_catalog += "\n**REMINDER: Copy these paths EXACTLY. Include relevant images in your modules based on their content, keywords, and topics.**\n"
                
                logger.info("[CUSTOM INSTRUCTION IMAGE MAPPING] Enhanced image catalog created with AI analysis")
            else:
                logger.info("[CUSTOM INSTRUCTION IMAGE MAPPING] No images available for '%s'", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))

            # Prevent prompt bloat: cleaned_text may already include an image catalog.
            # Adding enhanced_image_catalog again duplicates large blocks and increases
            # truncation risk in model output.
            if enhanced_image_catalog and "--- AVAILABLE IMAGES ---" in cleaned_text:
                logger.info(
                    f"[CUSTOM INSTRUCTION IMAGE MAPPING] Skipping duplicate enhanced catalog for '{doc_label}' "
                    f"because cleaned_text already contains an image catalog"
                )
                enhanced_image_catalog = ""
            
            full_prompt = f"""{CUSTOM_INSTRUCTION_AGENT_INSTRUCTIONS}

**IMPORTANT CONTEXT:**
- This document is part of a larger course that may include multiple source documents
- These custom instructions are specifically for THIS document: {doc_name}
- If you received document-specific instructions, follow them precisely for this document
- Maintain consistency with the overall course structure while following document-specific requirements
- **CRITICAL FOR MULTI-DOCUMENT COURSES**: This is document {doc_index+1} of {total_docs} in a multi-document course
- **MODULE NUMBERING**: The modules you generate should be numbered starting from Module {start_module_number}
  - If you generate 4 modules, they should be: Module {start_module_number}, Module {start_module_number+1}, Module {start_module_number+2}, Module {start_module_number+3}
  - Do NOT restart numbering from Module 1 for this document
  - Include the module number in each module's title (e.g., "Module {start_module_number}: Topic Name")

Source Document Name: {doc_name}

Source Document Text:
---
{cleaned_text}
{enhanced_image_catalog}
---

**🎯 USER'S CUSTOM INSTRUCTIONS FOR THIS DOCUMENT (FOLLOW THESE EXACTLY):**
---
{document_specific_prompt}
---{module_instruction}

**YOUR TASK:** Carefully read the user's custom instructions above and generate modules that EXACTLY match their requirements for THIS specific document. Pay special attention to any requests about module count, structure, content focus, style, or special requirements. If images are available in the catalog above, include relevant images in your modules using the EXACT paths provided. Map images intelligently based on their content, keywords, and topics. **REMEMBER: Number your modules starting from Module {start_module_number}, not Module 1.**{PPT_MULTI_MODULE_INSTRUCTION if _ppt_page_snapshots else ""}"""
            modules = await execute_agent_on_text(custom_instruction_agent, full_prompt, doc_name, job_id, doc_label)
            if ordered_source_rows:
                modules = rebalance_custom_instruction_modules_images(
                    modules, ordered_source_rows, document_specific_prompt
                )
            catalog_keys_order = [
                r.get("s3_key") for r in (ordered_source_rows or []) if r.get("s3_key")
            ]
            cached_doc_analyses = load_image_analysis_from_s3(user_id, doc_name)
            for mod in modules:
                mod["edited_content"] = filter_non_existent_images(mod.get("edited_content") or "")
                mod["edited_content"] = deduplicate_images_in_module(mod.get("edited_content") or "")
                if catalog_keys_order:
                    mod["edited_content"] = reorder_image_tags_in_content_by_catalog(
                        mod.get("edited_content") or "",
                        catalog_keys_order,
                        ordered_source_rows or [],
                    )
                mod["edited_content"] = fix_inverted_image_and_heading_blocks(mod.get("edited_content") or "")
                mod["edited_content"] = inject_image_analysis_below_image_tags(
                    mod.get("edited_content") or "",
                    ordered_source_rows or [],
                    cached_doc_analyses,
                )
            if _ppt_page_snapshots:
                modules = _ensure_ppt_slide_images_in_modules(modules, extracted_images)
                modules = _finalize_ppt_module_output(modules)
            return modules

        update_progress(f"Splitting '{doc_label}' into chunks...")
        initial_chunks = split_text_into_chunks(cleaned_text, DEFAULT_CHUNK_SIZE_WORDS, DEFAULT_CHUNK_OVERLAP_WORDS)
        if not initial_chunks:
            logger.warning("In '%s': Text too short to process automatically. Skipping.", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            return []

        update_progress(f"🔍 '{doc_label}': Analyzing content structure...")
        chunk_embeddings = await asyncio.to_thread(embedding_model.embed_documents, initial_chunks)
        chunk_embeddings = np.array(chunk_embeddings)
        if chunk_embeddings is None or chunk_embeddings.size == 0:
            raise ValueError(f"Failed to generate embeddings for '{doc_label}'")

        update_progress(f"✂️ '{doc_label}': Intelligently segmenting into modules...")

        if _ppt_page_snapshots and not requested_count:
            # Measured against the deck, not against however many snapshots rendered.
            n_slides = max(
                _ppt_true_slide_count,
                len(_filter_ppt_layout_slide_images(extracted_images))
                or len(extracted_images),
            )
            if n_slides > 1:
                logger.info(
                    "[PPT] No module count in prompt: using content-based segmentation for %s slides "
                    "(not 1 module per slide)",
                    n_slides,
                )
                update_progress(
                    f"📊 '{doc_label}': Grouping {n_slides} slide(s) into modules by topic..."
                )
        
        # Use intelligent segmentation if user requested specific module count
        if requested_count:
            logger.info('[SEGMENTATION] Using intelligent segmentation for %s modules', ("" if requested_count is None else str(requested_count).replace("\n", "").replace("\r", "")))
            segments = segment_text_intelligently(initial_chunks, requested_count, DEFAULT_MIN_SEGMENT_WORDS)
        else:
            # Use automatic boundary detection for default segmentation
            logger.info("[SEGMENTATION] Using automatic boundary detection")
            boundaries = find_segment_boundaries(chunk_embeddings, DEFAULT_SEGMENTATION_THRESHOLD_MULTIPLIER)
            segments = group_chunks_into_segments(initial_chunks, boundaries, DEFAULT_MIN_SEGMENT_WORDS)
        
        if not segments and initial_chunks:
            full_text_segment = " ".join(initial_chunks)
            if count_words(full_text_segment) >= DEFAULT_MIN_SEGMENT_WORDS:
                segments = [full_text_segment]
        if not segments:
            logger.warning("In '%s': No valid segments found. Skipping.", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            return []

        # CRITICAL FIX: Add image catalog to segments BEFORE mapping
        # This ensures images are available for mapping regardless of custom instructions
        logger.info("[IMAGE CATALOG] Adding image catalog to segments for mapping")
        
        # Build image catalog from actual S3 images
        image_catalog_text = ""
        if actual_s3_images:
            image_catalog_text = "\n\n--- AVAILABLE IMAGES ---\n" + IMAGE_CATALOG_ORDER_INSTRUCTION
            for img in actual_s3_images:
                image_catalog_text += f"[IMAGE: {img['s3_key']}]\n"
                image_catalog_text += f"Caption: {img['caption']}\n"
                image_catalog_text += f"Page: {img['page_num']}\n"
                image_catalog_text += f"Description: Image from {doc_name}, page {img['page_num']}\n\n"
            image_catalog_text += "--- END OF IMAGES ---\n"
            logger.info('[IMAGE CATALOG] Image catalog created with %s images', len(actual_s3_images))
        elif extracted_images:
            logger.info('[IMAGE CATALOG] Using extracted_images as fallback (%s images)', len(extracted_images))
            image_catalog_text = "\n\n--- AVAILABLE IMAGES ---\n" + IMAGE_CATALOG_ORDER_INSTRUCTION
            for img in extracted_images:
                image_catalog_text += f"[IMAGE: {img['s3_key']}]\n"
                image_catalog_text += f"Caption: {img.get('caption', 'Image')}\n"
                image_catalog_text += f"Page: {img.get('page_num', 'N/A')}\n\n"
            image_catalog_text += "--- END OF IMAGES ---\n"
        else:
            logger.info('[IMAGE CATALOG] No images available for %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
        
        # Add image catalog to each segment for context during generation
        if image_catalog_text:
            segments = [segment + image_catalog_text for segment in segments]
            logger.info('[IMAGE CATALOG] Added image catalog to all %s segments', len(segments))

        # INTELLIGENT IMAGE MAPPING: Create perfect 1:1 mapping between segments and images
        rows_for_mapping = ordered_source_rows if ordered_source_rows else extracted_images
        logger.info(
            f"[INTELLIGENT MAPPING] Starting intelligent image mapping for {len(segments)} segments "
            f"and {len(rows_for_mapping)} images (catalog-ordered)"
        )

        # Prepare image analyses for mapping
        image_analyses = []
        for reading_order, img_data in enumerate(rows_for_mapping):
            doc_page = img_data.get('page_num')
            if 'ai_analysis' in img_data:
                ia = dict(img_data['ai_analysis'])
                ia['document_page'] = doc_page
                ia['reading_order'] = reading_order
                if img_data.get('is_page_snapshot'):
                    ia['is_page_snapshot'] = True
                    ia['is_decorative'] = False
                image_analyses.append(ia)
            else:
                # Fallback for images without AI analysis
                image_analyses.append({
                    's3_key': img_data['s3_key'],
                    'caption': img_data.get('caption', 'Image'),
                    'keywords': img_data.get('keywords', []),
                    'topics': img_data.get('topics', []),
                    'type': img_data.get('image_type', 'unknown'),
                    'content': img_data.get('content_description', ''),
                    'document_page': doc_page,
                    'reading_order': reading_order,
                })
        
        # Create intelligent mapping
        image_mapping = create_intelligent_image_mapping(segments, image_analyses)
        
        async def generate_title_and_report_with_images(index, segment_text):
            try:
                # Get mapped images for this segment
                segment_images = image_mapping.get(index, [])
                
                # Create enhanced segment text with image information
                enhanced_segment = segment_text
                
                if segment_images:
                    logger.info('[MODULE %s] Assigned %s image(s)', ("" if index+1 is None else str(index+1).replace("\n", "").replace("\r", "")), len(segment_images))
                    
                    # Add image catalog to the segment
                    image_catalog = "\n\n--- AVAILABLE IMAGES FOR THIS MODULE ---\n" + IMAGE_CATALOG_ORDER_INSTRUCTION
                    for img_info in segment_images:
                        s3_key = img_info['s3_key']
                        analysis = img_info['analysis']
                        image_catalog += f"[IMAGE: {s3_key}]\n"
                        image_catalog += f"Caption: {analysis.get('caption', 'Image')}\n"
                        image_catalog += f"Type: {analysis.get('type', 'unknown')}\n"
                        image_catalog += f"Content: {analysis.get('content', 'No description')}\n"
                        image_catalog += f"Keywords: {', '.join(analysis.get('keywords', []))}\n\n"
                    image_catalog += "--- END OF IMAGES ---\n"
                    
                    enhanced_segment += image_catalog
                else:
                    logger.info('[MODULE %s] No images assigned', ("" if index+1 is None else str(index+1).replace("\n", "").replace("\r", "")))
                
                # Generate title and report with image context
                ppt_extra = PPT_MULTI_MODULE_INSTRUCTION if _ppt_page_snapshots else ""
                title_prompt = f"{TITLE_AGENT_INSTRUCTIONS}\n\nHere is the text:\n\n{enhanced_segment}"
                report_prompt = (
                    f"{REPORT_AGENT_INSTRUCTIONS}{ppt_extra}\n\n"
                    f"Here is the `Source Text Segment`:\n\n{enhanced_segment}"
                )
                
                title_task = asyncio.to_thread(title_agent.run, title_prompt)
                report_task = asyncio.to_thread(report_agent.run, report_prompt)
                
                title_response, report_response = await asyncio.gather(title_task, report_task)
                
                # Verify all assigned images are used in the generated content
                generated_content = report_response.content
                used_images = extract_image_tags(generated_content)
                
                # Intelligently integrate mapped images the model omitted (preserve document reading order)
                missing_blocks: List[str] = []
                for img_info in sorted(segment_images, key=reading_order_key_from_segment_image):
                    s3_key = img_info['s3_key']
                    if s3_key in generated_content:
                        continue
                    logger.warning("[MODULE %s] AI didn't use assigned image %s, appending in document order", ("" if index+1 is None else str(index+1).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))

                    analysis = img_info['analysis']
                    image_caption = analysis.get('caption', 'Image')
                    image_type = analysis.get('type', 'unknown')
                    image_content = analysis.get('content', '')
                    image_purpose = analysis.get('purpose', '')

                    is_layout_slide = (
                        (analysis or {}).get("is_page_snapshot")
                        or _ppt_layout_snapshot_key(s3_key)
                    )
                    if _is_irrelevant_image(analysis) and not is_layout_slide:
                        logger.info('[MODULE %s] Skipping irrelevant image during integration: %s', ("" if index+1 is None else str(index+1).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                        try:
                            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
                            logger.info('[MODULE %s] Deleted irrelevant image from S3: %s', ("" if index+1 is None else str(index+1).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                        except Exception:
                            pass
                        continue

                    type_headers = {
                        'diagram':              '### Architecture Diagram',
                        'flowchart':            '### Process Flow',
                        'bar_chart':            '### Data Visualization',
                        'line_graph':           '### Performance Trend',
                        'architecture_diagram': '### System Architecture',
                        'process_flow':         '### Workflow Overview',
                        'table':                '### Reference Table',
                        'screenshot':           '### Interface Example',
                        'illustration':         '### Conceptual Illustration',
                    }
                    section_header = type_headers.get(image_type.lower(), f'### {image_caption[:60]}')

                    desc_parts = []
                    if image_purpose:
                        desc_parts.append(image_purpose.rstrip('.') + '.')
                    elif image_content:
                        desc_parts.append(image_content[:200].rstrip('.') + '.')

                    desc_text = ' '.join(desc_parts) if desc_parts else ''

                    integration_text = (
                        f"{section_header}\n\n"
                        f"{desc_text}\n\n"
                        f"[IMAGE: {s3_key}]\n\n"
                        f"*{image_caption}*"
                    )
                    missing_blocks.append(integration_text.strip())

                if missing_blocks:
                    slide_section = (
                        "## Presentation slides\n\n"
                        if _ppt_page_snapshots
                        else "## Visual reference (source order)\n\n"
                    )
                    generated_content = (
                        generated_content.rstrip()
                        + "\n\n"
                        + slide_section
                        + "\n\n".join(missing_blocks)
                        + "\n"
                    )
                    logger.info(
                        f"[MODULE {index+1}] Appended {len(missing_blocks)} missing image(s) in reading order"
                    )

                return index, (title_response.content, generated_content, segment_images)
            except Exception as e:
                logger.error('Error generating title/report for segment %s: %s', ("" if index is None else str(index).replace("\n", "").replace("\r", "")), e, exc_info=True)
                return index, (None, None, [])

        tasks_to_run = [generate_title_and_report_with_images(i, segment) for i, segment in enumerate(segments)]
        temp_results = {}
        completion_count = 0
        total_segments = len(segments)
        total_images_used = 0
        
        # Update progress with correct module count after segmentation
        update_progress(f"✅ Created {total_segments} modules for '{doc_label}' - Starting generation...")
        
        for future in asyncio.as_completed(tasks_to_run):
            completion_count += 1
            update_progress(f"🤖 '{doc_label}': Generating module {completion_count}/{total_segments} with AI...")
            index, result = await future
            
            if len(result) == 3:
                title, report_markdown, assigned_images = result
            else:
                title, report_markdown = result
                assigned_images = []
            
            if not title or not report_markdown:
                logger.warning("Skipping segment %d for doc '%s' due to empty response from agent.", index, doc_name)
                continue
            
            # Count images used in this module
            images_in_content = count_image_tags(report_markdown)
            total_images_used += images_in_content
            
            logger.info('[MODULE %s] Generated with %s image(s), %s assigned', ("" if index+1 is None else str(index+1).replace("\n", "").replace("\r", "")), ("" if images_in_content is None else str(images_in_content).replace("\n", "").replace("\r", "")), len(assigned_images))
            
            # Final validation - ensure no fake paths and optimize for trainees
            validated_content = filter_non_existent_images(report_markdown)
            # CRITICAL FIX: Remove duplicate images from the same module
            validated_content = deduplicate_images_in_module(validated_content)
            validated_content = fix_inverted_image_and_heading_blocks(validated_content)

            # Additional validation for trainee image inclusion
            final_content = ensure_trainee_image_inclusion(validated_content, user_id, doc_name)
            
            temp_results[index] = {
                "report_id": str(uuid.uuid4()),
                "doc_name": doc_name,
                # Uploaded filename, for the curation UI's "Source:" line only. doc_name is the
                # randomized storage name (VAPT #4) and stays the join key.
                "doc_display_name": doc_label,
                "seg_idx": index + 1,
                "title": title.strip().strip('"') or f"Untitled Segment {index + 1}",
                "edited_content": final_content,
                "assigned_images": len(assigned_images),
                "used_images": images_in_content
            }
        
        # Final verification
        logger.info('[MAPPING VERIFICATION] Total images available: %s', len(image_analyses))
        logger.info('[MAPPING VERIFICATION] Total images used in modules: %s', ("" if total_images_used is None else str(total_images_used).replace("\n", "").replace("\r", "")))
        logger.info(
            '[MAPPING VERIFICATION] Mapping accuracy: %s%%',
            f"{(total_images_used / len(image_analyses) * 100) if image_analyses else 100:.1f}",
        )
        
        if total_images_used != len(image_analyses):
            logger.warning('[MAPPING WARNING] Image count mismatch! Available: %s, Used: %s', len(image_analyses), ("" if total_images_used is None else str(total_images_used).replace("\n", "").replace("\r", "")))
        else:
            logger.info("[MAPPING SUCCESS] Perfect 1:1 image mapping achieved!")
        
        # Prepare final results
        final_modules = [temp_results[i] for i in sorted(temp_results.keys())]

        if not final_modules and segments:
            # doc_label carries the user-supplied filename, so it is sanitized before it
            # reaches the log (Sonar S5145 - log injection).
            logger.warning(
                "[FALLBACK] Image-aware generation returned 0 modules for '%s'. "
                "Retrying with plain-text module generation.",
                log_safe(doc_label),
            )
            fallback_prompt = f"""{CUSTOM_INSTRUCTION_AGENT_INSTRUCTIONS}

Source Document Name: {doc_name}

Source Document Text:
---
{base_cleaned_text}
---

**FALLBACK TASK:** Generate EXACTLY {len(segments)} module(s) as a JSON array.
Each item must contain `title` and `content`.
Do not use markdown code fences.
If image usage is uncertain, omit image tags instead of inventing or forcing paths."""
            final_modules = await execute_agent_on_text(custom_instruction_agent, fallback_prompt, doc_name, job_id, doc_label)
            logger.info(
                f"[FALLBACK] Plain-text fallback generated {len(final_modules)} module(s) for '{doc_label}'"
            )
        
        # VERIFICATION: Ensure 200% accuracy in image mapping
        verification_result = verify_image_mapping_accuracy(final_modules, image_analyses)
        
        # FIX: Apply fixes if needed to achieve perfect mapping
        if not verification_result['is_perfect_mapping']:
            logger.warning("[ACCURACY] Image mapping not perfect, applying fixes...")
            final_modules = fix_image_mapping_issues(final_modules, image_analyses, verification_result)
            
            # Re-verify after fixes
            final_verification = verify_image_mapping_accuracy(final_modules, image_analyses)
            if final_verification['is_perfect_mapping']:
                logger.info("[SUCCESS] 200% accuracy achieved after fixes!")
            else:
                logger.error("[ERROR] Could not achieve perfect mapping even after fixes")
        else:
            logger.info("[SUCCESS] 200% accuracy achieved on first attempt!")

        catalog_keys_order = [img.get("s3_key") for img in actual_s3_images if img.get("s3_key")]
        if not catalog_keys_order and ordered_source_rows:
            catalog_keys_order = [r.get("s3_key") for r in ordered_source_rows if r.get("s3_key")]
        if catalog_keys_order:
            for m in final_modules:
                m["edited_content"] = reorder_image_tags_in_content_by_catalog(
                    m.get("edited_content") or "",
                    catalog_keys_order,
                    ordered_source_rows,
                )
                m["edited_content"] = fix_inverted_image_and_heading_blocks(m.get("edited_content") or "")

        if _ppt_page_snapshots:
            final_modules = _ensure_ppt_slide_images_in_modules(final_modules, extracted_images)
            final_modules = _finalize_ppt_module_output(final_modules)

        return final_modules
    except JobCancelledError:
        logger.info(
            "Document processing cancelled for '%s'",
            log_safe(doc_name),
        )
        raise
    except Exception as e:
        logger.error("Failed during document processing for '%s': %s", log_safe(doc_name), log_safe(e), exc_info=True)
        safe_detail = log_safe(str(e))[:240]
        if safe_detail and safe_detail not in doc_name:
            raise ValueError(f"Failed to process file '{doc_label}': {safe_detail}") from e
        raise ValueError(f"Failed to process file '{doc_label}'") from e
    finally:
        _token_job_id.reset(_job_ctx_token)
        _token_doc_name.reset(_doc_ctx_token)

def _delete_modules_for_course(user_id: str, doc_name: str, course_id: str):
    """
    Deletes modules for a specific course only (not all modules with the same source_doc).
    This allows multiple courses to use the same document without interfering with each other.
    """
    logger.info("[DELETE MODULES] Deleting modules for course '%s', document '%s', user '%s'", ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
    
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        report_pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user_id}/generated_reports/")
        keys_to_delete = []
        
        for page in report_pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                        
                        # Only delete if BOTH source_doc AND course_id match
                        if meta_data.get("source_doc") == doc_name and meta_data.get("course_id") == course_id:
                            keys_to_delete.append({'Key': key})
                            keys_to_delete.append({'Key': key.replace('.json', '.md')})
                            logger.debug('[DELETE MODULES] Marking for deletion: %s', ("" if key is None else str(key).replace("\n", "").replace("\r", "")))
                    except (ClientError, json.JSONDecodeError) as e:
                        logger.warning('[DELETE MODULES] Error reading metadata %s: %s', ("" if key is None else str(key).replace("\n", "").replace("\r", "")), e)
                        continue
        
        if keys_to_delete:
            s3_client.delete_objects(Bucket=S3_BUCKET_NAME, Delete={'Objects': keys_to_delete})
            logger.info("[DELETE MODULES] Deleted %s module(s) for course '%s', document '%s'", len(keys_to_delete)//2, ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
        else:
            logger.info("[DELETE MODULES] No modules found to delete for course '%s', document '%s'", ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            
    except BotoCoreError as e:
        logger.error("[DELETE MODULES] S3 error while deleting modules for course '%s': %s", ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), e, exc_info=True)

def _delete_specific_modules_for_course(user_id: str, doc_name: str, course_id: str, seg_indices_to_delete: set):
    """
    CRITICAL FIX: Only deletes modules with specific seg_idx values.
    This preserves modules that were skipped during regeneration.
    
    Args:
        user_id: The user ID
        doc_name: The document name
        course_id: The course ID
        seg_indices_to_delete: Set of seg_idx values to delete (modules being replaced)
    """
    logger.info("[DELETE SPECIFIC MODULES] Deleting specific modules for course '%s', document '%s', seg_indices: %s", ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if sorted(seg_indices_to_delete) is None else str(sorted(seg_indices_to_delete)).replace("\n", "").replace("\r", "")))
    
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        report_pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user_id}/generated_reports/")
        keys_to_delete = []
        
        for page in report_pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                        
                        # Only delete if source_doc AND seg_index match.
                        # CRITICAL FIX: support both "seg_index" and "seg_idx" key names (legacy vs new)
                        # Also support legacy modules that don't have course_id in metadata
                        meta_seg = meta_data.get("seg_index", meta_data.get("seg_idx"))
                        meta_course_id = meta_data.get("course_id")
                        source_doc_matches = meta_data.get("source_doc") == doc_name
                        course_matches = (meta_course_id == course_id) or (meta_course_id is None)
                        seg_matches = meta_seg in seg_indices_to_delete
                        
                        if source_doc_matches and course_matches and seg_matches:
                            keys_to_delete.append({'Key': key})
                            keys_to_delete.append({'Key': key.replace('.json', '.md')})
                            logger.info('[DELETE SPECIFIC MODULES] Marking for deletion: %s (seg=%s, course_id=%s)', ("" if key is None else str(key).replace("\n", "").replace("\r", "")), ("" if meta_seg is None else str(meta_seg).replace("\n", "").replace("\r", "")), ("" if meta_course_id is None else str(meta_course_id).replace("\n", "").replace("\r", "")))
                    except (ClientError, json.JSONDecodeError) as e:
                        logger.warning('[DELETE SPECIFIC MODULES] Error reading metadata %s: %s', ("" if key is None else str(key).replace("\n", "").replace("\r", "")), e)
                        continue
        
        if keys_to_delete:
            s3_client.delete_objects(Bucket=S3_BUCKET_NAME, Delete={'Objects': keys_to_delete})
            logger.info("[DELETE SPECIFIC MODULES] Deleted %s module(s) for course '%s', document '%s'", len(keys_to_delete)//2, ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
        else:
            logger.info("[DELETE SPECIFIC MODULES] No modules found to delete for course '%s', document '%s' with seg_indices %s", ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if sorted(seg_indices_to_delete) is None else str(sorted(seg_indices_to_delete)).replace("\n", "").replace("\r", "")))
            
    except BotoCoreError as e:
        logger.error("[DELETE SPECIFIC MODULES] S3 error while deleting modules for course '%s': %s", ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), e, exc_info=True)

def _is_document_used_by_other_courses(user_id: str, doc_name: str, current_course_id: str, db: Session) -> bool:
    """
    Checks if a document is used by other courses besides the current one.
    Returns True if the document is referenced by other courses.
    """
    try:
        # Find all courses by this user
        all_courses = db.query(Course).filter(Course.author_id == user_id).all()
        
        for course in all_courses:
            # Skip the current course being deleted
            if course.id == current_course_id:
                continue
            
            # Parse source_documents
            source_docs = []
            if course.source_documents:
                try:
                    raw_docs = json.loads(course.source_documents)
                    source_docs = [secure_filename(d) for d in raw_docs] if isinstance(raw_docs, list) else [course.source_doc_name] if course.source_doc_name else []
                except:
                    source_docs = [course.source_doc_name] if course.source_doc_name else []
            else:
                source_docs = [course.source_doc_name] if course.source_doc_name else []
            
            # Check if this document is used
            if doc_name in source_docs:
                logger.info(
                    "Document %s is used by course %s (ID: %s)",
                    ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
                    ("" if course.course_title is None else str(course.course_title).replace("\n", "").replace("\r", "")),
                    ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")),
                )
                return True
        
        return False
    except Exception as e:
        logger.error('Error checking document usage: %s', e)
        return False  # Assume not used if error occurs


def _backup_shared_document_to_s3(user_id: str, doc_name: str) -> bool:
    """
    Creates a backup copy of a shared document in S3 before deletion.
    Stores it in a special 'shared_documents' folder with timestamp.
    Returns True if backup was successful.
    """
    try:
        source_key = f"{user_id}/uploaded_docs/{doc_name}"
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_key = f"{user_id}/shared_documents/{secure_filename(doc_name)}_{timestamp}"
        
        # Copy the document to backup location
        s3_client.copy_object(
            Bucket=S3_BUCKET_NAME,
            CopySource={'Bucket': S3_BUCKET_NAME, 'Key': source_key},
            Key=backup_key
        )
        logger.info(
            "Backed up shared document %s to %s",
            ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
            ("" if backup_key is None else str(backup_key).replace("\n", "").replace("\r", "")),
        )
        
        # Also backup extracted images if they exist
        try:
            s3 = boto3.resource('s3')
            bucket = s3.Bucket(S3_BUCKET_NAME)
            image_prefix = f"{user_id}/extracted_images/{secure_filename(doc_name)}/"
            images = list(bucket.objects.filter(Prefix=image_prefix))
            
            if images:
                for img_obj in images:
                    # Extract the image filename from the full key
                    img_filename = img_obj.key.split('/')[-1]
                    backup_img_key = f"{user_id}/shared_documents/{secure_filename(doc_name)}_{timestamp}_images/{img_filename}"
                    
                    s3_client.copy_object(
                        Bucket=S3_BUCKET_NAME,
                        CopySource={'Bucket': S3_BUCKET_NAME, 'Key': img_obj.key},
                        Key=backup_img_key
                    )
                logger.info(
                    "Backed up %s images for shared document %s",
                    len(images),
                    ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
                )
        except Exception as img_error:
            logger.warning(
                "Could not backup images for %s: %s",
                ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
                img_error,
            )
        
        return True
    except Exception as e:
        logger.error("Failed to backup shared document %s: %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), e)
        return False


def delete_source_document_completely(
    owner_user_id: str,
    doc_name: str,
    db: Session,
    shared_report_notify_message: str,
) -> list:
    """
    Fully delete a course (by primary source doc) for a trainer: DB rows, S3 modules/files,
    and Pinecone embeddings for every source document attached to that course.
    Used by both My Modules (trainer) and Super Admin user-content delete.
    Raises ValueError when doc_name is invalid.
    """
    import time

    if not doc_name or ".." in doc_name:
        raise ValueError("Invalid document name")

    safe_doc = secure_filename(doc_name) or doc_name
    all_source_docs = [safe_doc]
    course_id_to_delete = None

    course = (
        db.query(Course)
        .filter(Course.author_id == owner_user_id, Course.source_doc_name == doc_name)
        .first()
    )
    if not course and safe_doc != doc_name:
        course = (
            db.query(Course)
            .filter(Course.author_id == owner_user_id, Course.source_doc_name == safe_doc)
            .first()
        )

    if course:
        course_id_to_delete = course.id
        logger.info(
            "[DELETE COURSE] Deleting course ID: %s, doc: %s",
            log_safe(course_id_to_delete),
            log_safe(doc_name),
        )

        if course.source_documents:
            try:
                additional_docs = json.loads(course.source_documents)
                if isinstance(additional_docs, list) and additional_docs:
                    all_source_docs = [secure_filename(d) for d in additional_docs if d]
            except json.JSONDecodeError:
                logger.warning(
                    "[DELETE COURSE] Could not parse source_documents JSON, using primary doc only"
                )

        if safe_doc not in all_source_docs:
            all_source_docs.insert(0, safe_doc)
        # de-dupe while preserving order
        seen = set()
        unique_docs = []
        for d in all_source_docs:
            if d and d not in seen:
                seen.add(d)
                unique_docs.append(d)
        all_source_docs = unique_docs
        logger.info(
            "[DELETE COURSE] Will delete embeddings/files for docs: %s",
            log_safe(all_source_docs),
        )

        # Notifications go first, while the labels they are matched on (course title, uploaded
        # filenames) still exist AND while nothing else is pending on this session: the sweep
        # commits, and rolls back if it fails, so it must not be able to discard the row
        # deletions below.
        delete_notifications_for_course(db, course, all_source_docs)

        cache_entries = (
            db.query(TutorResponseCache)
            .filter(TutorResponseCache.course_id == course_id_to_delete)
            .all()
        )
        for cache_entry in cache_entries:
            try:
                s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
            except Exception as cache_exc:
                logger.warning(
                    "[CACHE CLEANUP] Failed to delete S3 cache %s: %s",
                    log_safe(cache_entry.response_s3_key),
                    log_safe(cache_exc),
                )
            db.delete(cache_entry)

        try:
            paginator = s3_client.get_paginator("list_objects_v2")
            report_pages = paginator.paginate(
                Bucket=S3_BUCKET_NAME, Prefix=f"{owner_user_id}/generated_reports/"
            )
            keys_to_delete = []
            for page in report_pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(".json"):
                        continue
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                        meta_data = json.loads(meta_obj["Body"].read().decode("utf-8"))
                        module_course_id = meta_data.get("course_id")
                        module_source_doc = meta_data.get("source_doc")
                        if (module_course_id == course_id_to_delete) or (
                            not module_course_id and module_source_doc in all_source_docs
                        ):
                            keys_to_delete.append({"Key": key})
                            keys_to_delete.append({"Key": key.replace(".json", ".md")})
                    except (ClientError, json.JSONDecodeError) as read_exc:
                        logger.warning(
                            "[DELETE MODULES] Error reading module %s: %s",
                            log_safe(key),
                            log_safe(read_exc),
                        )
            if keys_to_delete:
                for i in range(0, len(keys_to_delete), 1000):
                    batch = keys_to_delete[i : i + 1000]
                    s3_client.delete_objects(Bucket=S3_BUCKET_NAME, Delete={"Objects": batch})
                logger.info(
                    "[DELETE MODULES] Deleted %s module file pair(s) for course %s",
                    len(keys_to_delete) // 2,
                    log_safe(course_id_to_delete),
                )
        except BotoCoreError as s3_exc:
            logger.error(
                "[DELETE MODULES] S3 error while deleting modules: %s",
                log_safe(s3_exc),
                exc_info=True,
            )

        shared_course_ids = [
            row.id
            for row in db.query(SharedCourse.id)
            .filter(SharedCourse.course_id == course_id_to_delete)
            .all()
        ]
        if shared_course_ids:
            db.query(SharedCourseEdit).filter(
                SharedCourseEdit.shared_course_id.in_(shared_course_ids)
            ).delete(synchronize_session=False)
        db.query(SharedCourse).filter(SharedCourse.course_id == course_id_to_delete).delete(
            synchronize_session=False
        )
        db.query(TutorResponseCache).filter(
            TutorResponseCache.course_id == course_id_to_delete
        ).delete(synchronize_session=False)
        db.query(TokenUsage).filter(TokenUsage.course_id == course_id_to_delete).delete(
            synchronize_session=False
        )
        db.query(TraineeQuestionSet).filter(
            TraineeQuestionSet.course_id == course_id_to_delete
        ).delete(synchronize_session=False)
        db.query(TraineeAssessment).filter(
            TraineeAssessment.course_id == course_id_to_delete
        ).delete(synchronize_session=False)
        db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.course_id == course_id_to_delete
        ).delete(synchronize_session=False)
        db.query(AssessmentQuestion).filter(
            AssessmentQuestion.course_id == course_id_to_delete
        ).delete(synchronize_session=False)
        db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id_to_delete
        ).delete(synchronize_session=False)
        # ORM cascades cover TraineeAttempt / CourseProgress
        db.delete(course)
        db.commit()
        logger.info(
            "[DELETE COURSE] Deleted course DB row %s",
            log_safe(course_id_to_delete),
        )

    shared_reports = (
        db.query(SharedReport)
        .filter(
            SharedReport.source_doc_name == doc_name,
            SharedReport.sharer_id == owner_user_id,
        )
        .all()
    )
    if not shared_reports and safe_doc != doc_name:
        shared_reports = (
            db.query(SharedReport)
            .filter(
                SharedReport.source_doc_name == safe_doc,
                SharedReport.sharer_id == owner_user_id,
            )
            .all()
        )
    if shared_reports:
        for share in shared_reports:
            db.add(
                Notification(
                    user_id=share.recipient_id,
                    message=shared_report_notify_message,
                )
            )
            db.delete(share)
        db.commit()
        logger.info(
            "[DELETE COURSE] Deleted %s shared report record(s) for %s",
            len(shared_reports),
            log_safe(doc_name),
        )

    logger.info(
        "[DELETE COURSE] Starting full data deletion for %s document(s)",
        len(all_source_docs),
    )
    docs_wiped = []
    for source_doc in all_source_docs:
        logger.info(
            "[DELETE COURSE] Deleting data for document: %s",
            log_safe(source_doc),
        )
        wiped = _delete_data_for_source_document(
            owner_user_id,
            source_doc,
            full_delete=True,
            db=db,
            excluding_course_id=course_id_to_delete,
        )
        if wiped:
            docs_wiped.append(source_doc)

    try:
        time.sleep(2)
        total_remaining = 0
        for source_doc in docs_wiped:
            # Listing IDs by prefix is the only enumeration a serverless index supports;
            # a metadata-filtered query returns nothing here (see the note at the top of
            # this module), which is why deletion used to look successful while the
            # embeddings were still in the index.
            remaining_ids = list_vector_ids_for_source_document(owner_user_id, source_doc)
            if remaining_ids:
                logger.error(
                    "[DELETE VERIFICATION] %s embedding(s) still exist for '%s' - retrying",
                    len(remaining_ids),
                    log_safe(source_doc),
                )
                delete_vectors_for_source_document(owner_user_id, source_doc)
                total_remaining += count_remaining_vectors_for_source_document(
                    owner_user_id, source_doc
                )
            else:
                logger.info(
                    "[DELETE VERIFICATION] All embeddings deleted for '%s'",
                    log_safe(source_doc),
                )
        if total_remaining:
            logger.warning(
                "[DELETE VERIFICATION] %s embeddings may still remain across documents",
                total_remaining,
            )
    except Exception as verify_error:
        logger.warning(
            "[DELETE VERIFICATION] Could not verify deletion: %s",
            log_safe(verify_error),
        )

    # Backstop: the loop above can only reach the documents this course listed. Anything left
    # in the namespace whose document has no course, no module and no video-analysis report is
    # unreachable content that would otherwise keep inflating "Indexed Chunks" for ever.
    delete_orphan_vectors_for_user(owner_user_id, db)

    return all_source_docs


def _delete_data_for_source_document(
    user_id: str,
    doc_name: str,
    full_delete: bool = False,
    db: Session = None,
    skip_shared_check: bool = False,
    excluding_course_id: str = None,
):
    """
    Deletes data for a source document.
    - If full_delete is False (commit operation), only deletes generated reports and vectors.
    - If full_delete is True (source delete operation), deletes everything including the uploaded doc and extracted images.
    - IMPORTANT: If document is shared across multiple courses, creates a backup and skips
      vector/file wipe so other courses keep their knowledge base.
    - skip_shared_check: If True, skips the shared document check (used during re-upload scenarios).
    - excluding_course_id: Course being removed (works even after the Course row is deleted).
    """
    logger.info(
        "Deleting data for source document %s for user %s. Full delete: %s",
        log_safe(doc_name),
        log_safe(user_id),
        full_delete,
    )

    preserve_for_other_courses = False
    if full_delete and db and not skip_shared_check:
        course_id_for_check = excluding_course_id
        if not course_id_for_check:
            course_being_deleted = (
                db.query(Course)
                .filter(Course.author_id == user_id, Course.source_doc_name == doc_name)
                .first()
            )
            if course_being_deleted:
                course_id_for_check = course_being_deleted.id
        if course_id_for_check and _is_document_used_by_other_courses(
            user_id, doc_name, course_id_for_check, db
        ):
            logger.warning(
                "Document %s is used by other courses. Preserving vectors and source assets.",
                log_safe(doc_name),
            )
            backup_success = _backup_shared_document_to_s3(user_id, doc_name)
            if backup_success:
                logger.info(
                    "Backup created for shared document %s. Skipping vector/file wipe.",
                    log_safe(doc_name),
                )
                preserve_for_other_courses = True
            else:
                logger.error(
                    "Failed to backup shared document %s. Aborting vector/file wipe.",
                    log_safe(doc_name),
                )
                return False

    if preserve_for_other_courses:
        # Course-scoped modules should already be removed by delete_source_document_completely.
        return False

    # Always delete reports and vectors when not shared with other courses
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        report_pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user_id}/generated_reports/")
        keys_to_delete = []
        for page in report_pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                        if meta_data.get("source_doc") == doc_name:
                            keys_to_delete.append({'Key': key})
                            keys_to_delete.append({'Key': key.replace('.json', '.md')})
                    except (ClientError, json.JSONDecodeError):
                        continue
        if keys_to_delete:
            s3_client.delete_objects(Bucket=S3_BUCKET_NAME, Delete={'Objects': keys_to_delete})
            logger.info(
                "Deleted %s report(s) from S3 for document %s.",
                len(keys_to_delete) // 2,
                log_safe(doc_name),
            )
    except BotoCoreError as e:
        logger.error(
            "S3 error while deleting reports for %s: %s",
            log_safe(doc_name),
            log_safe(e),
            exc_info=True,
        )

    logger.info(
        "Deleting Pinecone vectors for source %s in namespace %s",
        log_safe(doc_name),
        log_safe(user_id),
    )
    delete_vectors_for_source_document(user_id, doc_name)
    remaining = count_remaining_vectors_for_source_document(user_id, doc_name)
    if remaining:
        # Serverless deletes are eventually consistent, so a small non-zero count right
        # after the call is normal; anything larger means the delete did not land.
        logger.warning(
            "%s Pinecone vector(s) still listed for %s in namespace %s right after delete.",
            remaining,
            log_safe(doc_name),
            log_safe(user_id),
        )
    else:
        logger.info(
            "Verified: no Pinecone vectors remain for %s in namespace %s.",
            log_safe(doc_name),
            log_safe(user_id),
        )

    if full_delete:
        try:
            s3 = boto3.resource('s3')
            bucket = s3.Bucket(S3_BUCKET_NAME)
            
            # Delete extracted images
            extracted_images_deleted = list(bucket.objects.filter(Prefix=f"{user_id}/extracted_images/{secure_filename(doc_name)}/"))
            if extracted_images_deleted:
                bucket.objects.filter(Prefix=f"{user_id}/extracted_images/{secure_filename(doc_name)}/").delete()
                logger.info(
                    "Deleted %s extracted images for %s",
                    len(extracted_images_deleted),
                    ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
                )
            
            # Delete uploaded source document
            uploaded_docs_deleted = list(bucket.objects.filter(Prefix=f"{user_id}/uploaded_docs/{secure_filename(doc_name)}"))
            if uploaded_docs_deleted:
                bucket.objects.filter(Prefix=f"{user_id}/uploaded_docs/{secure_filename(doc_name)}").delete()
                logger.info(
                    "Deleted %s uploaded docs for %s",
                    len(uploaded_docs_deleted),
                    ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
                )
            
            # CRITICAL FIX: Delete image analysis cache
            image_analysis_deleted = list(bucket.objects.filter(Prefix=f"{user_id}/image_analysis/{secure_filename(doc_name)}/"))
            if image_analysis_deleted:
                bucket.objects.filter(Prefix=f"{user_id}/image_analysis/{secure_filename(doc_name)}/").delete()
                logger.info(
                    "Deleted %s image analysis files for %s",
                    len(image_analysis_deleted),
                    ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
                )
            
            # ADDITIONAL FIX: Delete tutor response cache files for this document
            tutor_cache_deleted = list(bucket.objects.filter(Prefix=f"tutor_cache/"))
            tutor_cache_count = 0
            for obj in tutor_cache_deleted:
                # Check if this cache file is related to courses from this document
                if doc_name in obj.key:
                    obj.delete()
                    tutor_cache_count += 1
            if tutor_cache_count > 0:
                logger.info(
                    "Deleted %s tutor cache files for %s",
                    tutor_cache_count,
                    ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
                )

            logger.info(
                "Performed full delete of extracted images, source doc, image analysis, and tutor cache for %s.",
                log_safe(doc_name),
            )
        except BotoCoreError as e:
            logger.error(
                "S3 error during full delete for %s: %s",
                log_safe(doc_name),
                log_safe(e),
                exc_info=True,
            )

    return True

def _reindex_source_document(user_id: str, source_doc_name: str) -> int:
    logger.info(
        "Re-indexing source %s for user %s",
        ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")),
        ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
    )
    related_reports_content = []
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user_id}/generated_reports/")
        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                        if meta_data.get('source_doc') == source_doc_name:
                            md_key = key.replace('.json', '.md')
                            report_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=md_key)
                            related_reports_content.append(report_obj['Body'].read().decode('utf-8'))
                    except (s3_client.exceptions.NoSuchKey, json.JSONDecodeError, KeyError) as e:
                        logger.warning(
                            "Skipping corrupt or missing S3 metadata/report file %s: %s",
                            ("" if key is None else str(key).replace("\n", "").replace("\r", "")),
                            ("" if e is None else str(e).replace("\n", "").replace("\r", "")),
                        )
    except BotoCoreError as e:
        logger.error(
            "Failed to list S3 objects for re-indexing user %s: %s",
            ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
            e,
        )
        return 0
    if not related_reports_content:
        logger.warning(
            "No report content found for %s during re-index.",
            ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")),
        )
        return 0
    logger.info(
        "Deleting old vectors for source %s in namespace %s.",
        ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")),
        ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
    )
    # Delete by ID prefix, not by metadata filter: the filter form is unsupported on a
    # serverless index, so re-indexing a document that now yields FEWER chunks used to
    # leave the surplus vectors behind and keep answering from deleted content.
    delete_vectors_for_source_document(user_id, source_doc_name)
    full_content = "\n\n---\n\n".join(related_reports_content)
    new_chunks = split_text_into_chunks(full_content, DEFAULT_RAG_CHUNK_SIZE_WORDS, DEFAULT_RAG_CHUNK_OVERLAP_WORDS)
    if not new_chunks:
        logger.warning(
            "No chunks generated from content of %s. Nothing to index.",
            ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")),
        )
        return 0
    logger.info(
        "Generating embeddings for %s new chunks from %s.",
        len(new_chunks),
        ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")),
    )
    try:
        vectors = _embed_chunks_in_batches(new_chunks)
    except Exception as e:
        logger.error(
            "Embedding generation failed for %s: %s",
            ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")),
            e,
            exc_info=True,
        )
        raise
    vectors_to_upsert = []
    for i, chunk in enumerate(new_chunks):
        vectors_to_upsert.append({
            "id": f"{source_doc_name}-{i}",
            "values": vectors[i],
            "metadata": {
                "source": source_doc_name,
                "text": _pinecone_metadata_text(chunk),
            },
        })
    logger.info(
        "Upserting %s vectors to Pinecone namespace %s.",
        len(vectors_to_upsert),
        ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
    )
    upserted = 0
    batch_size = 100
    try:
        for i in range(0, len(vectors_to_upsert), batch_size):
            batch = vectors_to_upsert[i : i + batch_size]
            pinecone_index.upsert(vectors=batch, namespace=user_id)
            upserted += len(batch)
        logger.info("Upsert successful (%s vectors).", upserted)
    except Exception as e:
        logger.error(
            "Pinecone upsert failed for user %s: %s",
            ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
            e,
            exc_info=True,
        )
        raise
    return upserted

def background_processing_task(job_id, user_id, files_data, prompt_by_user):
    logger.info('Background task started for job_id: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")))
    update_job_status(job_id, status='running', progress_message='Agent is initializing...')
    db = SessionLocal()
    try:
        # This lookup used to sit above the try, so the one thing that can fail before any
        # work starts - checking out a connection to run it - leaked the session for the life
        # of this thread and left the job stuck reporting "running". Inside the try it is
        # covered by the finally below, and the except marks the job failed like any other
        # error. The early return still releases the connection: finally runs on return too.
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            update_job_status(job_id, status='failed', error='User not found for background job.')
            return
        async def do_processing():
            all_reports_flat = []
            all_text_content = ""  # Collect all text for title generation

            # Parse custom instructions once to show what will be applied
            if prompt_by_user:
                update_job_status(job_id, progress_message=f'📋 Custom instructions detected. Processing documents SEQUENTIALLY (one at a time)...')

            # CRITICAL FIX: Process documents SEQUENTIALLY when custom instructions are provided
            # This ensures one document is FULLY completed before starting the next
            for i, file_data in enumerate(files_data):
                raise_if_job_cancelled(job_id)
                # Handle both old format (3/4 items) and new format (5 items with original_bytes)
                if len(file_data) == 5:
                    filename, file_content, content_type, original_filename, original_bytes = file_data
                elif len(file_data) == 4:
                    filename, file_content, content_type, original_filename = file_data
                    original_bytes = file_content
                else:
                    filename, file_content, content_type = file_data
                    original_filename = filename
                    original_bytes = file_content

                _orig_ext_job = (
                    original_filename.rsplit(".", 1)[-1].lower()
                    if "." in original_filename
                    else ""
                )
                if _orig_ext_job in ("docx", "doc", "pptx", "ppt"):
                    raise_if_job_cancelled(job_id)
                    update_job_status(
                        job_id,
                        progress_message=(
                            f"📄 Converting '{original_filename}' to PDF "
                            f"(this may take a minute on large documents)..."
                        ),
                    )
                    try:
                        file_content, _converted_name = convert_document_to_pdf(
                            original_bytes, original_filename
                        )
                        content_type = "application/pdf"
                        files_data[i] = (
                            filename,
                            file_content,
                            content_type,
                            original_filename,
                            original_bytes,
                        )
                        logger.info(
                            "[OFFICE] Converted '%s' to PDF (%s bytes) in background job",
                            log_safe(original_filename),
                            len(file_content),
                        )
                    except HTTPException:
                        raise ValueError(
                            f"Failed to convert '{original_filename}' to PDF."
                        )
                    except Exception as conv_exc:
                        logger.error(
                            "Background conversion failed for '%s': %s",
                            log_safe(original_filename),
                            log_safe(conv_exc),
                            exc_info=True,
                        )
                        raise ValueError(
                            f"Failed to convert '{original_filename}' to PDF."
                        ) from conv_exc

                secure_name = secure_filename(filename)

                # Show which document is being processed
                update_job_status(job_id, progress_message=f"📄 Document {i+1}/{len(files_data)}: '{original_filename}'")

                # Parse and show document-specific instructions
                doc_specific_instruction = parse_per_document_instructions(prompt_by_user, original_filename) if prompt_by_user else None
                if doc_specific_instruction:
                    # Truncate long instructions for display
                    display_instruction = doc_specific_instruction[:150] + "..." if len(doc_specific_instruction) > 150 else doc_specific_instruction
                    update_job_status(job_id, progress_message=f"✨ Applying custom instruction for '{original_filename}': {display_instruction}")
                elif prompt_by_user:
                    # Show general instruction if no document-specific one
                    display_instruction = prompt_by_user[:150] + "..." if len(prompt_by_user) > 150 else prompt_by_user
                    update_job_status(job_id, progress_message=f"✨ Applying general instruction: {display_instruction}")

                update_job_status(job_id, progress_message=f"🔧 Preparing workspace for '{original_filename}'...")
                _delete_data_for_source_document(user_id, secure_name, full_delete=True, db=db, skip_shared_check=True)

                update_job_status(job_id, progress_message=f"🚀 Processing '{original_filename}' ({i+1}/{len(files_data)})...")
                s3_key = f"{user_id}/uploaded_docs/{secure_name}"

                # Save the ORIGINAL file to S3 (preserves alignment/formatting for View Docs)
                orig_ext = original_filename.rsplit('.', 1)[-1].lower() if '.' in original_filename else ''
                orig_content_type_map = {
                    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                    'ppt':  'application/vnd.ms-powerpoint',
                    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    'doc':  'application/msword',
                    'pdf':  'application/pdf',
                    'png':  'image/png',
                    'jpg':  'image/jpeg',
                    'jpeg': 'image/jpeg',
                }
                orig_ct = orig_content_type_map.get(orig_ext, content_type)
                s3_client.put_object(
                    Bucket=S3_BUCKET_NAME, Key=s3_key,
                    Body=original_bytes,
                    ServerSideEncryption='AES256',
                    ContentType=orig_ct
                )
                logger.info("[S3] Saved original '%s' (%s bytes) to %s", ("" if original_filename is None else str(original_filename).replace("\n", "").replace("\r", "")), len(original_bytes), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))

                # If the file was converted to PDF, also save the PDF for internal processing
                if orig_ext in ('pptx', 'ppt', 'docx', 'doc') and original_bytes is not file_content:
                    pdf_key = s3_key.rsplit('.', 1)[0] + '_converted.pdf'
                    s3_client.put_object(
                        Bucket=S3_BUCKET_NAME, Key=pdf_key,
                        Body=file_content,
                        ServerSideEncryption='AES256',
                        ContentType='application/pdf'
                    )
                    logger.info('[S3] Saved converted PDF to %s', ("" if pdf_key is None else str(pdf_key).replace("\n", "").replace("\r", "")))

                # CRITICAL: Process this document COMPLETELY before moving to next
                # This includes: extraction, analysis, segmentation, and module generation
                update_job_status(job_id, progress_message=f"⏳ '{original_filename}': Starting complete processing pipeline...")
                
                # CRITICAL FIX: Pass cumulative module count so AI knows what module numbers to use
                # For first document: start_module_number = 1
                # For second document: start_module_number = 1 + (modules from first doc)
                start_module_number = len(all_reports_flat) + 1
                logger.info("[SEQUENTIAL PROCESSING] Processing '%s' (doc %s/%s), starting module number: %s", ("" if original_filename is None else str(original_filename).replace("\n", "").replace("\r", "")), ("" if i+1 is None else str(i+1).replace("\n", "").replace("\r", "")), len(files_data), ("" if start_module_number is None else str(start_module_number).replace("\n", "").replace("\r", "")))
                
                reports_for_doc = await process_document_to_reports_async(
                    job_id,
                    secure_name,
                    file_content,
                    user_id,
                    content_type,
                    prompt_by_user,
                    start_module_number,
                    i,
                    len(files_data),
                    original_filename,
                    original_file_bytes=original_bytes
                    if _orig_ext_job in ("ppt", "pptx", "doc", "docx")
                    else None,
                )

                # CRITICAL: Wait for this document to be FULLY processed before continuing
                if reports_for_doc:
                    # Log the order of modules returned from processing
                    logger.info("[SEQUENTIAL PROCESSING] Modules returned from processing '%s':", ("" if original_filename is None else str(original_filename).replace("\n", "").replace("\r", "")))
                    for idx, report in enumerate(reports_for_doc):
                        logger.info("[SEQUENTIAL PROCESSING]   Position %s: '%s' (seg_idx=%s)", ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), ("" if report.get('title', 'Untitled') is None else str(report.get('title', 'Untitled')).replace("\n", "").replace("\r", "")), ("" if report.get('seg_idx', 0) is None else str(report.get('seg_idx', 0)).replace("\n", "").replace("\r", "")))
                    
                    # CRITICAL FIX: Add document_order to each report for proper sorting
                    # This ensures modules are grouped by document, not interleaved
                    for report in reports_for_doc:
                        report['document_order'] = i  # Track which document this module came from
                    
                    all_reports_flat.extend(reports_for_doc)
                    update_job_status(job_id, progress_message=f"✅ '{original_filename}': COMPLETED - Generated {len(reports_for_doc)} module(s)")
                    logger.info("[SEQUENTIAL PROCESSING] ✅ Document '%s' fully completed with %s modules", ("" if original_filename is None else str(original_filename).replace("\n", "").replace("\r", "")), len(reports_for_doc))
                    logger.info("[MODULE GROUPING] Document '%s' (order=%s) modules added to position %s to %s", ("" if original_filename is None else str(original_filename).replace("\n", "").replace("\r", "")), ("" if i is None else str(i).replace("\n", "").replace("\r", "")), len(all_reports_flat) - len(reports_for_doc) + 1, len(all_reports_flat))
                else:
                    update_job_status(job_id, progress_message=f"⚠️ '{original_filename}': No modules generated")
                    logger.warning("[SEQUENTIAL PROCESSING] ⚠️ Document '%s' generated no modules", ("" if original_filename is None else str(original_filename).replace("\n", "").replace("\r", "")))

                # Collect text content for title generation (collect more content for better analysis)
                if reports_for_doc:
                    for report in reports_for_doc:
                        # Collect module title and substantial content (12000 chars ~2400 words per module)
                        module_title = report.get('title', '')
                        if module_title:
                            all_text_content += f"MODULE: {module_title}\n"
                        all_text_content += report.get('content', '')[:12000] + "\n\n"

                # Show progress between documents
                if i < len(files_data) - 1:
                    update_job_status(job_id, progress_message=f"🔄 Moving to next document ({i+2}/{len(files_data)})...")

            # CRITICAL FIX: Renumber seg_idx to be continuous across all documents in the course
            # This ensures module numbers are sequential (1, 2, 3, 4, 5, 6) instead of restarting per document
            logger.info('[MODULE RENUMBERING] Renumbering %s modules for continuous numbering across course', len(all_reports_flat))
            
            # Log BEFORE sorting
            logger.info("[MODULE SORTING] BEFORE SORT - Current order:")
            for idx, report in enumerate(all_reports_flat):
                logger.info("[MODULE SORTING]   Position %s: '%s' (doc_order=%s, seg_idx=%s)", ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), ("" if report.get('title', 'Untitled') is None else str(report.get('title', 'Untitled')).replace("\n", "").replace("\r", "")), ("" if report.get('document_order', 0) is None else str(report.get('document_order', 0)).replace("\n", "").replace("\r", "")), ("" if report.get('seg_idx', 0) is None else str(report.get('seg_idx', 0)).replace("\n", "").replace("\r", "")))
            
            # CRITICAL: Sort by document_order first to ensure proper grouping
            all_reports_flat.sort(key=lambda r: (r.get('document_order', 0), r.get('seg_idx', 0)))
            logger.info('[MODULE SORTING] AFTER SORT - Sorted %s modules by (document_order, seg_idx)', len(all_reports_flat))
            
            # Log AFTER sorting
            logger.info("[MODULE SORTING] AFTER SORT - New order:")
            for idx, report in enumerate(all_reports_flat):
                logger.info("[MODULE SORTING]   Position %s: '%s' (doc_order=%s, seg_idx=%s)", ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), ("" if report.get('title', 'Untitled') is None else str(report.get('title', 'Untitled')).replace("\n", "").replace("\r", "")), ("" if report.get('document_order', 0) is None else str(report.get('document_order', 0)).replace("\n", "").replace("\r", "")), ("" if report.get('seg_idx', 0) is None else str(report.get('seg_idx', 0)).replace("\n", "").replace("\r", "")))
            
            for idx, report in enumerate(all_reports_flat, 1):
                old_seg_idx = report.get('seg_idx', idx)
                report['seg_idx'] = idx  # Renumber to be continuous across entire course
                logger.info("[MODULE RENUMBERING] Module '%s': seg_idx %s → %s (doc_order=%s)", ("" if report.get('title', 'Untitled') is None else str(report.get('title', 'Untitled')).replace("\n", "").replace("\r", "")), ("" if old_seg_idx is None else str(old_seg_idx).replace("\n", "").replace("\r", "")), ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), ("" if report.get('document_order', 0) is None else str(report.get('document_order', 0)).replace("\n", "").replace("\r", "")))

            logger.info('[SEQUENTIAL PROCESSING] ✅ All %s documents processed sequentially. Total modules: %s', len(files_data), len(all_reports_flat))
            
            # VERIFICATION: Log the final module order to ensure proper grouping
            logger.info("[MODULE ORDER VERIFICATION] ===== FINAL MODULE ORDER =====")
            for idx, report in enumerate(all_reports_flat, 1):
                logger.info("[MODULE ORDER] Position %s: '%s' (from %s, doc_order=%s)", ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), ("" if report.get('title', 'Untitled') is None else str(report.get('title', 'Untitled')).replace("\n", "").replace("\r", "")), ("" if report.get('doc_name', 'unknown') is None else str(report.get('doc_name', 'unknown')).replace("\n", "").replace("\r", "")), ("" if report.get('document_order', 0) is None else str(report.get('document_order', 0)).replace("\n", "").replace("\r", "")))
            logger.info("[MODULE ORDER VERIFICATION] ===== END MODULE ORDER =====")
            
            return all_reports_flat, all_text_content

        all_reports, combined_content = asyncio.run(do_processing())
        if not all_reports:
            raise ValueError("No valid modules were generated. Check logs for details.")
        update_job_status(job_id, progress_message='Agent is finalizing and saving modules...')

        # CRITICAL FIX: Create ONE course for ALL documents
        # Collect all document names.
        #
        # source_documents must hold the STORAGE name (file_data[0]), because that is the
        # name every other join key already uses: the S3 object under
        # {user_id}/uploaded_docs/, the module metadata "source_doc" written by the commit
        # endpoint, and Course.source_doc_name below. Storage names are randomized
        # ("<uuid>.<ext>", VAPT #4: Double Extension File Upload), so using the ORIGINAL
        # filename here silently broke every one of those lookups: /workflow/commit found no
        # course for documents 2..N and saved their modules with course_id=None (they then
        # disappeared from My Modules), and View Docs head_object() on the original name
        # returned NoSuchKey and rendered "(not found)" for every document.
        #
        # The original filenames are kept alongside, positionally aligned, purely so the UI
        # can show what the user actually uploaded - they can never be derived from a UUID.
        all_doc_names = []
        all_doc_display_names = []
        for file_data in files_data:
            if len(file_data) >= 4:
                filename, file_content, content_type, original_filename = file_data[:4]
            else:
                filename, file_content, content_type = file_data
                original_filename = filename
            all_doc_names.append(secure_filename(filename))
            all_doc_display_names.append(os.path.basename(str(original_filename or filename)))

        # Use first document as the primary source_doc_name for backward compatibility
        primary_doc = secure_filename(files_data[0][0] if len(files_data[0]) == 3 else files_data[0][0])

        # Generate AI-powered course title. Pass the DISPLAY names: the fallback path builds
        # a title out of the filenames, and a randomized "<uuid>.<ext>" storage name would
        # produce a hex-string course title.
        update_job_status(job_id, progress_message='Generating course title...')
        course_title = generate_course_title_from_content(combined_content, all_doc_display_names)
        logger.info('Generated course title: %s', ("" if course_title is None else str(course_title).replace("\n", "").replace("\r", "")))

        # Create or update ONE course for all documents
        course = db.query(Course).filter(
            Course.source_doc_name == primary_doc,
            Course.author_id == user_id
        ).first()

        if not course:
            course = Course(
                author_id=user_id,
                source_doc_name=primary_doc,
                process_id=user.process_id,
                initial_attempts=1,
                is_live=False,
                course_title=course_title,
                source_documents=json.dumps(all_doc_names),  # Storage names (join key)
                source_document_display_names=json.dumps(all_doc_display_names),
            )
            db.add(course)
            logger.info("✅ Created ONE course '%s' for %s document(s): %s", ("" if course_title is None else str(course_title).replace("\n", "").replace("\r", "")), len(all_doc_names), ("" if all_doc_names is None else str(all_doc_names).replace("\n", "").replace("\r", "")))
        else:
            # Update existing course
            course.course_title = course_title
            course.source_documents = json.dumps(all_doc_names)
            course.source_document_display_names = json.dumps(all_doc_display_names)
            logger.info("✅ Updated course '%s' with %s document(s): %s", ("" if course_title is None else str(course_title).replace("\n", "").replace("\r", "")), len(all_doc_names), ("" if all_doc_names is None else str(all_doc_names).replace("\n", "").replace("\r", "")))

        # Save PDF hash for the primary document (for duplicate detection)
        primary_file_data = files_data[0]
        if len(primary_file_data) >= 4:
            _, file_content, content_type = primary_file_data[0], primary_file_data[1], primary_file_data[2]
        else:
            _, file_content, content_type = primary_file_data

        if content_type == 'application/pdf':
            from services.tutor_cache import calculate_pdf_hash

            file_hash = calculate_pdf_hash(file_content)
            if not course.pdf_hash:
                course.pdf_hash = file_hash
                logger.info("✅ Saved PDF hash for course '%s'", ("" if course_title is None else str(course_title).replace("\n", "").replace("\r", "")))

        db.commit()

        message = f"Agent successfully processed {len(files_data)} document(s) into 1 course with {len(all_reports)} modules."
        
        # FINAL VERIFICATION: Log the exact order of modules being returned to frontend
        logger.info("[FINAL RESPONSE] ===== MODULES BEING RETURNED TO FRONTEND =====")
        for idx, report in enumerate(all_reports, 1):
            logger.info("[FINAL RESPONSE] Position %s: '%s' (seg_idx=%s, doc_name=%s)", ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), ("" if report.get('title', 'Untitled') is None else str(report.get('title', 'Untitled')).replace("\n", "").replace("\r", "")), ("" if report.get('seg_idx', 0) is None else str(report.get('seg_idx', 0)).replace("\n", "").replace("\r", "")), ("" if report.get('doc_name', 'unknown') is None else str(report.get('doc_name', 'unknown')).replace("\n", "").replace("\r", "")))
        logger.info("[FINAL RESPONSE] ===== END RESPONSE =====")
        
        # The curation screen prints "Source: <doc_display_name>"; storage names are randomized
        # (VAPT #4), so every report carries the filename its document was uploaded under.
        apply_report_display_names(all_reports, dict(zip(all_doc_names, all_doc_display_names)))

        response_model = ProcessResponse(job_id=job_id, message=message, generated_reports=all_reports, source_doc_name=primary_doc)
        update_job_status(job_id, status='completed', result=response_model.model_dump())
        logger.info('Background task finished successfully for job_id: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")))
    except JobCancelledError:
        logger.info(
            "Background task cancelled for job_id: %s",
            log_safe(job_id),
        )
        try:
            for file_data in files_data:
                if len(file_data) >= 4:
                    filename = file_data[0]
                else:
                    filename = file_data[0]
                secure_name = secure_filename(filename)
                _delete_data_for_source_document(
                    user_id, secure_name, full_delete=True, db=db, skip_shared_check=True
                )
        except Exception as cleanup_exc:
            logger.warning(
                "Cancel cleanup failed for job %s: %s",
                log_safe(job_id),
                log_safe(cleanup_exc),
            )
        update_job_status(job_id, status='cancelled', progress_message='Cancelled by user.')
    except Exception as e:
        logger.error('Background task failed for job_id %s: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        update_job_status(job_id, status='failed', error=str(e))
    finally:
        db.close()


def _regeneration_image_doc_matches(doc_name: str, s3_key: str) -> bool:
    """True when an S3 image key belongs to the given source document."""
    if not doc_name or not s3_key:
        return False
    sk = s3_key.replace("\\", "/")
    doc_variants = {
        secure_filename(doc_name),
        normalize_document_name(doc_name),
    }
    for variant in doc_variants:
        if variant and f"/extracted_images/{variant}/" in sk:
            return True
    return False


def _images_from_module_contents(
    modules_metadata: List[Dict],
    modules_to_regenerate: set,
) -> List[Dict]:
    """Collect [IMAGE:] keys already present in modules slated for regeneration."""
    seen: set = set()
    rows: List[Dict] = []
    for module in modules_metadata:
        if module.get("seg_idx") not in modules_to_regenerate:
            continue
        for key in extract_image_tags(module.get("content") or ""):
            key = key.strip()
            if not key or key in seen or not is_plausible_s3_image_key(key):
                continue
            seen.add(key)
            rows.append(
                {
                    "s3_key": key,
                    "page_num": 1,
                    "caption": f"Image from module: {(module.get('title') or 'Module').replace(chr(10), ' ').replace(chr(13), ' ')[:200]}",
                }
            )
    return rows


def _enrich_regeneration_extracted_images(
    user_id: str,
    docs_to_regenerate: List[str],
    pdf_extracted: List[Dict],
    modules_metadata: List[Dict],
    modules_to_regenerate: set,
) -> List[Dict]:
    """
    Merge PDF re-extraction, actual S3 scan, and images from existing module markdown
    so regeneration keeps the same image catalog as initial generation.
    """
    by_key: Dict[str, Dict] = {}
    for im in pdf_extracted or []:
        sk = (im.get("s3_key") or "").strip()
        if sk:
            by_key[sk] = dict(im)

    for doc_name in docs_to_regenerate:
        doc_pdf = [
            im
            for im in pdf_extracted or []
            if _regeneration_image_doc_matches(doc_name, im.get("s3_key") or "")
        ]
        s3_actual = get_actual_images_from_s3(user_id, doc_name)
        ordered = order_images_for_module_catalog(doc_pdf, s3_actual)
        merged = merge_extracted_with_catalog_order(doc_pdf, ordered)
        for im in merged or ordered or s3_actual or doc_pdf:
            sk = (im.get("s3_key") or "").strip()
            if sk:
                by_key[sk] = dict(im)

    for im in _images_from_module_contents(modules_metadata, modules_to_regenerate):
        sk = im["s3_key"]
        if sk not in by_key:
            by_key[sk] = im

    return list(by_key.values())


def _preserve_images_from_original_modules(
    all_reports: List[Dict],
    existing_modules_metadata: List[Dict],
    modules_to_regenerate: set,
) -> None:
    """Re-inject [IMAGE:] tags from pre-regeneration content when the agent omitted them."""
    source_by_seg = {
        m["seg_idx"]: m.get("content") or ""
        for m in existing_modules_metadata
        if m.get("seg_idx") in modules_to_regenerate
    }
    for report in all_reports:
        if not isinstance(report, dict):
            continue
        seg = report.get("seg_idx")
        source_content = source_by_seg.get(seg)
        if not source_content:
            continue
        edited = report.get("edited_content") or ""
        for key in extract_image_tags(source_content):
            if key in edited or not is_plausible_s3_image_key(key):
                continue
            try:
                s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=key)
            except ClientError:
                continue
            edited = f"{edited.rstrip()}\n\n[IMAGE: {key}]\n"
        report["edited_content"] = edited.strip()


def _build_regeneration_image_catalog_rows(
    user_id: str,
    all_extracted_images: List[Dict],
    doc_names_scope: List[str],
) -> List[Dict]:
    """
    Rows compatible with rebalance_custom_instruction_modules_images / inject_image_analysis_below_image_tags.
    Merges per-image AI analysis from S3 cache (same as initial upload pipeline).
    """
    if not all_extracted_images or not doc_names_scope:
        return []
    caches: Dict[str, Dict[str, dict]] = {}
    for doc in doc_names_scope:
        caches[doc] = load_image_analysis_from_s3(user_id, doc)
    rows_out: List[Dict] = []
    for im in all_extracted_images:
        sk = (im.get("s3_key") or "").replace("\\", "/")
        matched_doc = None
        for doc in doc_names_scope:
            if _regeneration_image_doc_matches(doc, sk):
                matched_doc = doc
                break
        if not matched_doc:
            continue
        row: Dict = {
            "s3_key": sk,
            "page_num": im.get("page_num"),
            "caption": im.get("caption", "Image"),
        }
        cached_ia = caches.get(matched_doc, {}).get(sk)
        if isinstance(cached_ia, dict):
            row["ai_analysis"] = cached_ia
        rows_out.append(row)
    return rows_out


def _remap_regenerated_modules_to_course_slots(
    all_reports: List[Dict],
    existing_modules_metadata: List[Dict],
    modules_to_regenerate: set,
) -> None:
    """
    execute_agent_on_text assigns seg_idx 1..N; regeneration must restore real seg_idx,
    doc_name, and document_order so partial regen, commit, and multi-doc courses stay correct.
    """
    if not all_reports:
        return
    fr = sorted(
        [m for m in existing_modules_metadata if m["seg_idx"] in modules_to_regenerate],
        key=lambda m: (m.get("document_order", 0), m["seg_idx"]),
    )
    n_out = len(all_reports)
    n_tgt = len(fr)
    ts_list = sorted({int(m["seg_idx"]) for m in fr})

    def _apply(r: Dict, meta: Dict) -> None:
        r["seg_idx"] = meta["seg_idx"]
        r["doc_name"] = meta["doc_name"]
        r["document_order"] = meta.get("document_order", 0)

    if n_out == n_tgt:
        for i, r in enumerate(all_reports):
            if isinstance(r, dict):
                _apply(r, fr[i])
        logger.info('[REGEN] Remapped %s module(s) to original seg_idx/doc slots (1:1)', ("" if n_out is None else str(n_out).replace("\n", "").replace("\r", "")))
        return

    if n_out < n_tgt:
        for i, r in enumerate(all_reports):
            if not isinstance(r, dict):
                continue
            seg = ts_list[i] if i < len(ts_list) else ts_list[-1]
            meta = next((m for m in fr if m["seg_idx"] == seg), fr[0])
            r["seg_idx"] = seg
            r["doc_name"] = meta["doc_name"]
            r["document_order"] = meta.get("document_order", 0)
        logger.info('[REGEN] Remapped %s module(s) onto first segment slot(s) of %s replaced (merge/shrink)', ("" if n_out is None else str(n_out).replace("\n", "").replace("\r", "")), ("" if n_tgt is None else str(n_tgt).replace("\n", "").replace("\r", "")))
        return

    if n_out > n_tgt and n_tgt == 1:
        base = int(fr[0]["seg_idx"])
        meta0 = fr[0]
        for i, r in enumerate(all_reports):
            if isinstance(r, dict):
                r["seg_idx"] = base + i
                r["doc_name"] = meta0["doc_name"]
                r["document_order"] = meta0.get("document_order", 0)
        logger.warning(
            f"[REGEN] More outputs ({n_out}) than replaced slots ({n_tgt}); assigned seg_idx {base}..{base + n_out - 1} — verify ordering."
        )
        return

    logger.warning(
        f"[REGEN] Module count mismatch: agent returned {n_out}, expected slots {n_tgt}; seg_idx left as from agent"
    )


def background_regeneration_task(job_id, user_id, source_doc_name, follow_up_prompt, is_super_admin_action=False, super_admin_id=None):
    logger.info('[REGENERATE] Background task started for job_id: %s, user_id: %s, source: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")), ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")))
    logger.info('[REGENERATE] Follow-up prompt: %s...', ("" if follow_up_prompt[:200] is None else str(follow_up_prompt[:200]).replace("\n", "").replace("\r", "")))
    update_job_status(job_id, status='running', progress_message='Regeneration agent is initializing...')
    try:
        # CRITICAL FIX: Get course first to check for multi-document support
        db = SessionLocal()
        try:
            course = db.query(Course).filter(Course.source_doc_name == source_doc_name).first()
            if not course:
                raise ValueError(f"Course '{source_doc_name}' not found in database")
            
            # Parse source_documents for multi-document courses
            course_docs_list = []
            if course.source_documents:
                try:
                    source_docs_list = json.loads(course.source_documents)
                    if isinstance(source_docs_list, list):
                        # CRITICAL FIX: Apply secure_filename() to handle legacy courses with original filenames
                        course_docs_list = [secure_filename(d) for d in source_docs_list]
                        logger.info('[REGENERATE] Multi-document course detected: %s documents', len(course_docs_list))
                except Exception as e:
                    logger.warning('[REGENERATE] Could not parse source_documents JSON: %s', e)
            
            # Fallback to single source_doc_name if source_documents is empty
            if not course_docs_list:
                course_docs_list = [source_doc_name]
                logger.info('[REGENERATE] Single document course: %s', ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")))
        finally:
            db.close()
        
        # CRITICAL FIX: Parse multi-document instructions to determine which documents to regenerate
        # If user specifies "Based on 'doc1.pdf'" or "For the first document", only regenerate those
        # If user says "Do not modify doc2.pdf", skip that document
        docs_to_regenerate = set(course_docs_list)  # Default: regenerate all
        docs_to_skip = set()
        
        # Check for document-specific instructions
        prompt_lower = follow_up_prompt.lower()
        
        # Pattern 1: "Based on 'filename'" or "For 'filename'"
        doc_pattern = r"(?:based on|for|referring to|regarding)\s+['\"]?([^'\"]+\.(?:pdf|docx|doc|pptx))['\"]?"
        doc_matches = re.findall(doc_pattern, follow_up_prompt, re.IGNORECASE)
        if doc_matches:
            logger.info('[REGENERATE] Found document-specific instructions for: %s', ("" if doc_matches is None else str(doc_matches).replace("\n", "").replace("\r", "")))
            docs_to_regenerate = set()
            for match in doc_matches:
                # Find matching document in course_docs_list
                for doc in course_docs_list:
                    if match.lower() in doc.lower() or doc.lower() in match.lower():
                        docs_to_regenerate.add(doc)
                        logger.info('[REGENERATE] Will regenerate: %s', ("" if doc is None else str(doc).replace("\n", "").replace("\r", "")))
        
        # Pattern 2: "Do not modify" or "Skip" or "Leave unchanged"
        skip_pattern = r"(?:do not modify|skip|leave unchanged|don't change)\s+['\"]?([^'\"]+\.(?:pdf|docx|doc|pptx))['\"]?"
        skip_matches = re.findall(skip_pattern, follow_up_prompt, re.IGNORECASE)
        if skip_matches:
            logger.info('[REGENERATE] Found skip instructions for: %s', ("" if skip_matches is None else str(skip_matches).replace("\n", "").replace("\r", "")))
            for match in skip_matches:
                for doc in course_docs_list:
                    if match.lower() in doc.lower() or doc.lower() in match.lower():
                        docs_to_skip.add(doc)
                        docs_to_regenerate.discard(doc)
                        logger.info('[REGENERATE] Will skip: %s', ("" if doc is None else str(doc).replace("\n", "").replace("\r", "")))
        
        logger.info('[REGENERATE] Documents to regenerate: %s', ("" if docs_to_regenerate is None else str(docs_to_regenerate).replace("\n", "").replace("\r", "")))
        logger.info('[REGENERATE] Documents to skip: %s', ("" if docs_to_skip is None else str(docs_to_skip).replace("\n", "").replace("\r", "")))
        
        # CRITICAL FIX: Load ALL documents for multi-document courses
        all_documents_text = []
        all_extracted_images = []
        
        for doc_name in course_docs_list:
            # CRITICAL FIX: Skip documents that user specified not to modify
            if doc_name in docs_to_skip:
                logger.info('[REGENERATE] Skipping document (user specified): %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                update_job_status(job_id, progress_message=f"Skipping '{doc_name}' (as per instructions)...")
                continue
            
            if doc_name not in docs_to_regenerate:
                logger.info('[REGENERATE] Skipping document (not in regenerate list): %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                update_job_status(job_id, progress_message=f"Skipping '{doc_name}'...")
                continue
            
            update_job_status(job_id, progress_message=f"Loading document: '{doc_name}'...")
            s3_doc_key = f"{user_id}/uploaded_docs/{secure_filename(doc_name)}"
            
            logger.info('[REGENERATE] Loading document from S3 key: %s', ("" if s3_doc_key is None else str(s3_doc_key).replace("\n", "").replace("\r", "")))
            
            try:
                doc_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_doc_key)
                doc_bytes = doc_obj['Body'].read()
                
                # Extract text AND images from document
                doc_text = ""
                doc_images = []
                
                # Determine file type and extract accordingly
                file_ext = doc_name.lower().split('.')[-1]
                
                if file_ext == 'pdf':
                    # Extract from PDF — hybrid: raster images + vector diagrams
                    with fitz.open(stream=doc_bytes, filetype="pdf") as pdf_doc:
                        saved_xrefs_regen = set()
                        for page_num, page in enumerate(pdf_doc):
                            # Text — filter footnotes/watermarks
                            raw_blocks = page.get_text("blocks")
                            page_text_parts = [b[4].strip() for b in raw_blocks if len(b) > 4 and not _is_footnote_text(b[4])]
                            page_text = "\n".join(page_text_parts)
                            doc_text += page_text + "\n\n"

                            # Pass 1: embedded raster images
                            images = page.get_images(full=True)
                            if images:
                                update_job_status(job_id, progress_message=f"Found {len(images)} image(s) on page {page_num+1} of '{doc_name}'...")
                            for img_index, img in enumerate(images):
                                try:
                                    xref = img[0]
                                    if xref in saved_xrefs_regen:
                                        continue
                                    base_image = pdf_doc.extract_image(xref)
                                    image_bytes = base_image["image"]
                                    image_ext = base_image["ext"]
                                    if len(image_bytes) < 1000:
                                        continue
                                    image_filename = f"page{page_num+1}_img{img_index}.{image_ext}"
                                    s3_image_key = f"{user_id}/extracted_images/{secure_filename(doc_name)}/{secure_filename(image_filename)}"
                                    try:
                                        s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_image_key)
                                    except ClientError:
                                        s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=s3_image_key, Body=image_bytes, ContentType=f"image/{image_ext}")
                                    doc_images.append({'s3_key': s3_image_key, 'page_num': page_num + 1, 'caption': f"Image from {doc_name} page {page_num+1}"})
                                    saved_xrefs_regen.add(xref)
                                except Exception as e:
                                    logger.error('[REGENERATE] Failed to extract image: %s', e)

                            # Pass 2: vector diagrams (Gamma / Canva style)
                            vector_diagrams = _extract_vector_diagrams_from_page(page, page_num)
                            for vd in vector_diagrams:
                                try:
                                    image_bytes = vd["image_bytes"]
                                    image_ext   = vd["image_ext"]
                                    caption_hint = vd["caption_hint"]
                                    s3_image_key = f"{user_id}/extracted_images/{secure_filename(doc_name)}/{secure_filename(caption_hint)}.{image_ext}"
                                    try:
                                        s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_image_key)
                                    except ClientError:
                                        s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=s3_image_key, Body=image_bytes, ContentType=f"image/{image_ext}")
                                    doc_images.append({'s3_key': s3_image_key, 'page_num': page_num + 1, 'caption': caption_hint})
                                    logger.info('[REGENERATE] Saved vector diagram: %s', ("" if s3_image_key is None else str(s3_image_key).replace("\n", "").replace("\r", "")))
                                except Exception as e:
                                    logger.error('[REGENERATE] Failed to save vector diagram: %s', e)
                
                elif file_ext in ['docx', 'doc']:
                    # Extract from DOCX (text only for now)
                    try:
                        from docx import Document
                        docx_doc = Document(io.BytesIO(doc_bytes))
                        for para in docx_doc.paragraphs:
                            doc_text += para.text + "\n"
                        logger.info('[REGENERATE] Extracted text from DOCX: %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                    except Exception as e:
                        logger.error('[REGENERATE] Failed to extract DOCX: %s', e)
                        doc_text = f"[Could not extract text from {doc_name}]"
                
                elif file_ext in ['pptx', 'ppt']:
                    # Extract from PPTX (text only for now)
                    try:
                        from pptx import Presentation
                        prs = Presentation(io.BytesIO(doc_bytes))
                        for slide in prs.slides:
                            for shape in slide.shapes:
                                if hasattr(shape, "text"):
                                    doc_text += shape.text + "\n"
                        logger.info('[REGENERATE] Extracted text from PPTX: %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                    except Exception as e:
                        logger.error('[REGENERATE] Failed to extract PPTX: %s', e)
                        doc_text = f"[Could not extract text from {doc_name}]"
                
                elif file_ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp']:
                    # Image file - add to images list
                    s3_image_key = s3_doc_key
                    doc_images.append({
                        's3_key': s3_image_key,
                        'page_num': 1,
                        'caption': f"Image: {doc_name}"
                    })
                    doc_text = f"[Image file: {doc_name}]"
                    logger.info('[REGENERATE] Added image file: %s', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                
                else:
                    logger.warning('[REGENERATE] Unsupported file type: %s', ("" if file_ext is None else str(file_ext).replace("\n", "").replace("\r", "")))
                    doc_text = f"[Unsupported file type: {doc_name}]"
                
                # Clean and store text
                doc_text = clean_text(doc_text)
                s3_actual = get_actual_images_from_s3(user_id, doc_name)
                ordered_s3 = order_images_for_module_catalog(doc_images, s3_actual)
                doc_images = (
                    merge_extracted_with_catalog_order(doc_images, ordered_s3)
                    or ordered_s3
                    or s3_actual
                    or doc_images
                )
                all_documents_text.append(f"\n\n=== DOCUMENT: {doc_name} ===\n\n{doc_text}")
                all_extracted_images.extend(doc_images)
                
                logger.info("[REGENERATE] Loaded document '%s': %s chars, %s images", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), len(doc_text), len(doc_images))
                
            except ClientError as e:
                if e.response['Error']['Code'] == 'NoSuchKey':
                    logger.error('[REGENERATE] Document not found: %s', ("" if s3_doc_key is None else str(s3_doc_key).replace("\n", "").replace("\r", "")))
                    all_documents_text.append(f"\n\n=== DOCUMENT: {doc_name} ===\n\n[Document not found in S3]")
                else:
                    raise
        
        # Combine all documents
        original_doc_text = "\n\n".join(all_documents_text)
        
        # Add image catalog (document order per file, then page / filename for multi-doc)
        if all_extracted_images:
            all_extracted_images = sorted(
                all_extracted_images,
                key=lambda im: (
                    (im.get("s3_key") or "").split("/")[-2].lower() if (im.get("s3_key") or "").count("/") >= 2 else "",
                    int(im.get("page_num") or 9999),
                    parse_image_filename_sort_keys((im.get("s3_key") or "").split("/")[-1])[1],
                    (im.get("s3_key") or "").lower(),
                ),
            )
            image_catalog = "\n\n--- AVAILABLE IMAGES ---\n" + IMAGE_CATALOG_ORDER_INSTRUCTION
            for img in all_extracted_images:
                image_catalog += f"\n[IMAGE: {img['s3_key']}]"
                image_catalog += f"\n  Caption: {img['caption']}"
                image_catalog += f"\n  Page: {img['page_num']}\n"
            image_catalog += "\n--- END OF IMAGES ---\n\n"
            original_doc_text += image_catalog
            logger.info('[REGENERATE] Added %s images to regeneration context', len(all_extracted_images))
        
        logger.info('[REGENERATE] Total content loaded: %s characters from %s document(s)', len(original_doc_text), len(course_docs_list))
        
        update_job_status(job_id, progress_message=f"Loading existing modules for context...")
        
        # Get normalized document names for module matching
        db = SessionLocal()
        doc_label = source_doc_name
        course_display_map = {}
        try:
            course = db.query(Course).filter(Course.source_doc_name == source_doc_name).first()

            # Storage names are randomized (VAPT #4), so progress text uses the uploaded name.
            # The map is kept for the curation screen's per-module "Source:" line further down.
            if course:
                course_display_map = course_document_display_map(course)
                doc_label = friendly_document_name(source_doc_name, course_display_map)

            course_docs_normalized = []
            if course and course.source_documents:
                try:
                    source_docs_list = json.loads(course.source_documents)
                    if isinstance(source_docs_list, list):
                        for doc in source_docs_list:
                            doc_normalized = normalize_document_name(doc)
                            course_docs_normalized.append(doc_normalized)
                        logger.info('[REGENERATE] Normalized %s document names: %s', len(course_docs_normalized), ("" if course_docs_normalized is None else str(course_docs_normalized).replace("\n", "").replace("\r", "")))
                except Exception as e:
                    logger.warning('[REGENERATE] Could not parse source_documents JSON: %s', e)
            
            # Fallback to single source_doc_name
            if not course_docs_normalized:
                course_doc_normalized = normalize_document_name(source_doc_name)
                course_docs_normalized = [course_doc_normalized]
                logger.info('[REGENERATE] Using fallback normalized name: %s', ("" if course_doc_normalized is None else str(course_doc_normalized).replace("\n", "").replace("\r", "")))
        finally:
            db.close()
        
        existing_reports_content = []
        existing_modules_metadata = []  # Track module metadata for selection parsing
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user_id}/generated_reports/")
        for page in pages:
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.json'):
                    meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                    meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                    source_doc = meta_data.get('source_doc', '')
                    
                    # Normalize the metadata source_doc for comparison
                    source_doc_normalized = normalize_document_name(source_doc)
                    
                    # CRITICAL FIX: Check if this module matches ANY of the course documents
                    is_match = False
                    for course_doc_norm in course_docs_normalized:
                        if (source_doc_normalized == course_doc_norm or 
                            course_doc_norm in source_doc_normalized or
                            source_doc_normalized in course_doc_norm):
                            is_match = True
                            break
                    
                    if is_match:
                        md_key = obj['Key'].replace('.json', '.md')
                        report_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=md_key)
                        report_content = report_obj['Body'].read().decode('utf-8')
                        
                        # CRITICAL FIX: metadata is saved as "seg_index" but was read as "seg_idx" — support both
                        seg_idx_val = meta_data.get('seg_index', meta_data.get('seg_idx', 0))
                        report_id = obj['Key'].split('/')[-1].replace('.json', '')
                        
                        # Store module metadata for selection parsing
                        existing_modules_metadata.append({
                            'seg_idx': seg_idx_val,
                            'title': meta_data.get('title', 'Untitled'),
                            'doc_name': source_doc,
                            'content': report_content,
                            'report_id': report_id,
                            'document_order': meta_data.get('document_order', 0),
                            'course_id': meta_data.get('course_id'),
                        })
                        
                        existing_reports_content.append(f"## Module Title: {meta_data.get('title', 'Untitled')}\n\n{report_content}")
                        logger.info('[REGENERATE] Loaded module: %s from %s', ("" if meta_data.get('title') is None else str(meta_data.get('title')).replace("\n", "").replace("\r", "")), ("" if source_doc is None else str(source_doc).replace("\n", "").replace("\r", "")))
        
        # CRITICAL FIX: Parse user instructions to determine which modules to regenerate
        logger.info("[REGENERATE] Parsing module selection from user instructions...")
        selection_result = parse_module_selection_instructions(follow_up_prompt, existing_modules_metadata)
        modules_to_regenerate = selection_result['modules_to_regenerate']
        modules_to_skip = selection_result['modules_to_skip']
        reason = selection_result['reason']
        
        logger.info('[REGENERATE] Module selection result: %s', ("" if reason is None else str(reason).replace("\n", "").replace("\r", "")))
        logger.info('[REGENERATE] Modules to regenerate: %s', ("" if sorted(modules_to_regenerate) is None else str(sorted(modules_to_regenerate)).replace("\n", "").replace("\r", "")))
        logger.info('[REGENERATE] Modules to skip: %s', ("" if sorted(modules_to_skip) is None else str(sorted(modules_to_skip)).replace("\n", "").replace("\r", "")))

        all_extracted_images = _enrich_regeneration_extracted_images(
            user_id,
            list(docs_to_regenerate),
            all_extracted_images,
            existing_modules_metadata,
            modules_to_regenerate,
        )
        logger.info(
            "[REGENERATE] Enriched image catalog: %s image(s) for regeneration pipeline",
            len(all_extracted_images),
        )

        # Filter existing modules to only include those that should be regenerated
        filtered_modules_metadata = [m for m in existing_modules_metadata if m['seg_idx'] in modules_to_regenerate]
        filtered_reports_content = []
        
        for module in filtered_modules_metadata:
            filtered_reports_content.append(f"## Module Title: {module['title']}\n\n{module['content']}")
        
        existing_reports_full_text = "\n\n---\n\n".join(filtered_reports_content)
        logger.info('[REGENERATE] Filtered to %s modules for regeneration (from %s total)', len(filtered_reports_content), len(existing_reports_content))
        
        # Add instruction about which modules are being regenerated
        if selection_result['regenerate_all']:
            module_selection_instruction = f"\n\n**📌 MODULE SELECTION**: The user wants to regenerate ALL modules."
        elif modules_to_skip:
            skip_titles = [m['title'] for m in existing_modules_metadata if m['seg_idx'] in modules_to_skip]
            regen_titles = [m['title'] for m in existing_modules_metadata if m['seg_idx'] in modules_to_regenerate]
            module_selection_instruction = (
                f"\n\n**📌 MODULE SELECTION — CRITICAL**: "
                f"You must ONLY output modules for: {regen_titles}. "
                f"Do NOT output anything for these preserved modules (they will be kept as-is): {skip_titles}. "
                f"Your JSON array must contain ONLY the regenerated/combined modules, NOT the preserved ones."
            )
        else:
            module_selection_instruction = f"\n\n**📌 MODULE SELECTION**: Regenerate modules: {sorted(modules_to_regenerate)}. {reason}."
        
        # Use LLM (Nova Pro) to extract module count from regeneration prompt
        # This provides better natural language understanding than regex
        logger.info('Using LLM to extract module count from regeneration prompt: %s...', ("" if follow_up_prompt[:100] is None else str(follow_up_prompt[:100]).replace("\n", "").replace("\r", "")))
        requested_count = extract_module_count_with_llm(follow_up_prompt)
        
        if requested_count:
            module_count_instruction = f"\n\n**🚨 CRITICAL INSTRUCTION - ABSOLUTE PRIORITY 🚨**: The user has EXPLICITLY requested EXACTLY {requested_count} module(s). You MUST generate EXACTLY {requested_count} module(s), NO MORE, NO LESS. This is a strict requirement that overrides all other considerations. Count your modules before submitting to ensure you have EXACTLY {requested_count}."
            logger.info('User explicitly requested %s module(s) in regeneration prompt', ("" if requested_count is None else str(requested_count).replace("\n", "").replace("\r", "")))
        else:
            # No specific module count requested - let AI decide based on content
            module_count_instruction = ""
        
        full_prompt = (f"{REGENERATION_AGENT_INSTRUCTIONS}\n\n"
                       f"**CONTEXT:**\n"
                       f"- Source Document: {source_doc_name}\n"
                       f"- Task: Regenerate modules based on user's new instructions\n\n"
                       f"Original Source Document Text:\n---\n{original_doc_text}\n---\n\n"
                       f"Previously Generated Modules (Your first attempt):\n---\n{existing_reports_full_text}\n---\n\n"
                       f"**🎯 USER'S NEW INSTRUCTIONS FOR IMPROVEMENT (FOLLOW THESE EXACTLY):**\n---\n{follow_up_prompt}\n---"
                       f"{module_selection_instruction}"
                       f"{module_count_instruction}\n\n"
                       f"**YOUR TASK:** Carefully read the user's new instructions above and regenerate the modules to EXACTLY match their requirements. Pay special attention to any requests about module count, structure, content focus, or style changes.")

        update_job_status(job_id, progress_message=f"Agent is regenerating modules for '{doc_label}'...")
        
        async def do_regeneration():
            from services.agents import regeneration_agent

            return await execute_agent_on_text(regeneration_agent, full_prompt, source_doc_name, job_id, doc_label)
        
        all_reports = asyncio.run(do_regeneration())
        if not all_reports:
            raise ValueError("Regeneration agent failed to produce valid modules.")

        # Fix seg_idx/doc_name/document_order (agent uses local 1..N; course uses real segment indices)
        _remap_regenerated_modules_to_course_slots(
            all_reports, existing_modules_metadata, modules_to_regenerate
        )

        # Same image pipeline as custom-instruction generation: catalog + rebalance + analysis under images
        doc_scope = [d for d in course_docs_list if d in docs_to_regenerate]
        regen_catalog = _build_regeneration_image_catalog_rows(user_id, all_extracted_images, doc_scope)
        if regen_catalog:
            all_reports = rebalance_custom_instruction_modules_images(
                all_reports, regen_catalog, follow_up_prompt
            )
        _preserve_images_from_original_modules(
            all_reports, existing_modules_metadata, modules_to_regenerate
        )
        regen_catalog_keys = [row.get("s3_key") for row in (regen_catalog or []) if row.get("s3_key")]
        for r in all_reports:
            if not isinstance(r, dict):
                continue
            ec = r.get("edited_content") or ""
            ec = filter_non_existent_images(ec)
            ec = deduplicate_images_in_module(ec)
            if regen_catalog_keys:
                ec = reorder_image_tags_in_content_by_catalog(
                    ec, regen_catalog_keys, regen_catalog or []
                )
            ec = fix_inverted_image_and_heading_blocks(ec)
            ec = inject_image_analysis_below_image_tags(ec, regen_catalog, {})
            r["edited_content"] = ec

        # CRITICAL FIX: Merge regenerated modules with preserved (skipped) modules
        # The AI only outputs the modules it was asked to regenerate.
        # We must add back the skipped modules so the frontend sees the full course.
        if modules_to_skip:
            skipped_modules_as_reports = []
            for m in existing_modules_metadata:
                if m['seg_idx'] in modules_to_skip:
                    skipped_modules_as_reports.append({
                        'report_id': m['report_id'],
                        'doc_name': m['doc_name'],
                        'seg_idx': m['seg_idx'],
                        'title': m['title'],
                        'edited_content': m['content'],
                        'document_order': m.get('document_order', 0),
                    })
                    logger.info("[REGENERATE] Preserving skipped module: '%s' (seg_idx=%s)", ("" if m['title'] is None else str(m['title']).replace("\n", "").replace("\r", "")), ("" if m['seg_idx'] is None else str(m['seg_idx']).replace("\n", "").replace("\r", "")))
            
            # Combine: regenerated modules + preserved modules, then sort by seg_idx
            combined = list(all_reports) + skipped_modules_as_reports
            combined.sort(key=lambda r: r.get('seg_idx', 0) if isinstance(r, dict) else getattr(r, 'seg_idx', 0))
            
            # Attach deleted_seg_indices to the first *regenerated* row (sort order can put a preserved module first)
            skipped_report_ids = {m["report_id"] for m in skipped_modules_as_reports}
            if combined and modules_to_regenerate:
                for item in combined:
                    rid = item.get("report_id") if isinstance(item, dict) else getattr(item, "report_id", None)
                    if rid is not None and rid not in skipped_report_ids:
                        if isinstance(item, dict):
                            item["deleted_seg_indices"] = sorted(modules_to_regenerate)
                        else:
                            item.deleted_seg_indices = sorted(modules_to_regenerate)
                        break
            
            all_reports = combined
            logger.info('[REGENERATE] Final module set: %s total (%s regenerated + %s preserved)', len(all_reports), len(all_reports) - len(skipped_modules_as_reports), len(skipped_modules_as_reports))
        
        update_job_status(job_id, progress_message='Agent is finalizing and saving regenerated modules...')

        # Regenerated and preserved modules both reach the curation screen's "Source:" line, and
        # remapping can move a module onto another document of the course, so resolve every one.
        apply_report_display_names(all_reports, course_display_map)

        message = f"Agent successfully regenerated modules for '{doc_label}'."
        response_model = ProcessResponse(
            job_id=job_id, 
            message=message, 
            generated_reports=all_reports, 
            source_doc_name=source_doc_name,
            deleted_seg_indices=sorted(modules_to_regenerate)  # Tell commit which seg_indices to fully delete
        )
        update_job_status(job_id, status='completed', result=response_model.model_dump())
        
        # Notify trainer if super admin performed the regeneration
        if is_super_admin_action and super_admin_id:
            try:
                db = SessionLocal()
                # close() belongs in a finally, not at the end of the happy path. Anything that
                # raised in between - a display-label lookup, a failed commit - skipped straight
                # to the except and left the connection checked out for as long as this thread
                # lived. Opening the session inside the outer try is deliberate: a pool timeout
                # here must stay a logged notification failure, not fail the regeneration that
                # has already finished successfully.
                try:
                    trainer = db.query(User).filter(User.id == user_id).first()
                    if trainer:
                        # Name the course the trainer knows, not its randomized storage key.
                        regen_course = db.query(Course).filter(
                            Course.author_id == user_id,
                            Course.source_doc_name == source_doc_name,
                        ).first()
                        course_label = shared_source_display_label(db, user_id, source_doc_name, regen_course)
                        notification = Notification(
                            user_id=trainer.id,
                            message=f"Super Admin regenerated modules for your course '{course_label}'",
                            course_id=course_notification_id(regen_course)
                        )
                        db.add(notification)
                        db.commit()
                        logger.info('[REGENERATE] Notified trainer %s about super admin regeneration', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
                finally:
                    db.close()
            except Exception as e:
                logger.error('[REGENERATE] Failed to notify trainer: %s', e)
        
        logger.info('[REGENERATE] Background regeneration task finished successfully for job_id: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")))
    except JobCancelledError:
        logger.info(
            "[REGENERATE] Background regeneration cancelled for job_id: %s",
            log_safe(job_id),
        )
        update_job_status(job_id, status='cancelled', progress_message='Cancelled by user.')
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            logger.error("[REGENERATE] Regeneration failed for job %s: Could not find one or more documents for '%s'.", ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")), ("" if source_doc_name is None else str(source_doc_name).replace("\n", "").replace("\r", "")))
            update_job_status(job_id, status='failed', error=f"One or more documents for course '{source_doc_name}' not found in S3.")
        else:
            logger.error('[REGENERATE] Background regeneration task failed for job_id %s: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
            update_job_status(job_id, status='failed', error=str(e))
    except Exception as e:
        logger.error('[REGENERATE] Background regeneration task failed for job_id %s: %s', ("" if job_id is None else str(job_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        update_job_status(job_id, status='failed', error=str(e))

def _read_module_markdown(md_key: str) -> str:
    """One module's markdown body, or an empty string when it cannot be read.

    Missing or unreadable bodies were already non-fatal here - the module still lists, just with
    an image count of zero - so this keeps swallowing the error and only reports it at debug.
    """
    try:
        return s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=md_key)['Body'].read().decode('utf-8')
    except Exception as e:
        logger.debug("Could not read module body '%s': %s", log_safe(md_key), log_safe(e))
        return ""


def _get_grouped_reports(user_id: str, db: Session, include_content: bool = False, requesting_user_id: Optional[str] = None, validate_images: bool = False) -> List[Dict]:
    """
    Get grouped reports by course (not by individual document).
    Returns courses with their associated modules.
    """
    grouped = OrderedDict()
    
    # Get all courses for this user (newest first for My Modules / admin views)
    courses = (
        db.query(Course)
        .filter(Course.author_id == user_id)
        .order_by(Course.created_at.desc())
        .all()
    )
    
    for course in courses:
        doc_name = course.source_doc_name
        display_map = course_document_display_map(course)
        # Title fallback derives from the filename, so use the DISPLAY name - a randomized
        # "<uuid>.<ext>" storage name would render the course as a hex string.
        course_title = course.course_title or friendly_document_name(
            doc_name, display_map
        ).replace('_', ' ').replace('-', ' ').rsplit('.', 1)[0]

        # Parse source_documents JSON array
        source_docs = []
        if course.source_documents:
            try:
                raw_docs = json.loads(course.source_documents)
                # CRITICAL FIX: Apply secure_filename() to handle legacy courses that stored original filenames
                source_docs = [secure_filename(d) for d in raw_docs] if isinstance(raw_docs, list) else [doc_name]
            except (ValueError, TypeError):
                source_docs = [doc_name]
        else:
            source_docs = [doc_name]
        
        grouped[doc_name] = {
            'reports': [],
            'is_published': course.is_live,
            'course_id': course.id,
            'course_title': course_title,
            'source_documents': source_docs,
            # Original upload filenames for the UI. Storage names are randomized
            # ("<uuid>.<ext>", VAPT #4), so without this the list can only show a UUID.
            'source_document_display_names': [
                friendly_document_name(d, display_map) for d in source_docs
            ],
            'created_at': course.created_at,
        }
    
    logger.info(
        "[GET_GROUPED_REPORTS] Found %s courses for user %s",
        len(grouped),
        ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
    )
    
    # Get completed modules for requesting user
    course_ids = [data['course_id'] for data in grouped.values() if data['course_id']]
    completed_modules = {}
    if requesting_user_id and course_ids:
        # One grouped query for the whole page; this used to be a query per course.
        completed_modules = {course_id: set() for course_id in course_ids}
        for row in db.query(
            CourseProgress.course_id, CourseProgress.completed_module_index
        ).filter(
            CourseProgress.user_id == requesting_user_id,
            CourseProgress.course_id.in_(course_ids),
        ).all():
            completed_modules[row[0]].add(row[1])

    # Load reports from S3. The metadata comes from the per-request index, which lists the
    # prefix once and fetches the bodies concurrently instead of one blocking GET per object,
    # and the courses already loaded above answer the timestamp fallback below.
    courses_by_id = {course.id: course for course in courses}
    last_modified_by_key = author_report_last_modified(db, user_id)
    matched_reports = []  # (s3_key, metadata, matched course key) in listing order
    for key, meta_data in author_report_metadata(db, user_id):
        try:
            source_doc = meta_data.get("source_doc")

            # CRITICAL FIX: Use course_id from metadata if available (most reliable)
            # Otherwise, match by checking if source_doc is in course's source_documents
            # If multiple courses use the same document, match to the course created closest to when the module was created
            matched_course_key = None

            # Check if module metadata has course_id (new modules will have this)
            module_course_id = meta_data.get("course_id")

            if module_course_id:
                # Direct match using course_id from metadata (most reliable)
                found_course = False
                for doc_name, data in grouped.items():
                    if data['course_id'] == module_course_id:
                        matched_course_key = doc_name
                        found_course = True
                        logger.debug('[REPORT MATCH] Direct match: report %s -> course_id %s', log_safe(Path(key).stem), log_safe(module_course_id))
                        break

                # If course_id in metadata doesn't match any existing course, fall back to source_doc matching
                if not found_course:
                    logger.warning('[REPORT MATCH] Module %s has course_id %s but course not found, falling back to source_doc matching', log_safe(Path(key).stem), log_safe(module_course_id))

            if not matched_course_key:
                # Fallback to timestamp-based matching for old modules without course_id
                # Get module creation time from the S3 listing
                module_created_at = last_modified_by_key.get(key)

                # Find all courses that contain this source_doc.
                # Compare on the NORMALIZED name so a module stored under the
                # randomized upload name still matches a course whose
                # source_documents predates that change (and vice versa);
                # otherwise these modules are orphaned and vanish from the list.
                source_doc_norm = normalize_document_name(source_doc)
                matching_courses = []
                for doc_name, data in grouped.items():
                    course_doc_norms = {
                        normalize_document_name(d) for d in data['source_documents']
                    }
                    course_doc_norms.update(
                        normalize_document_name(d)
                        for d in data.get('source_document_display_names') or []
                    )
                    if source_doc in data['source_documents'] or (
                        source_doc_norm and source_doc_norm in course_doc_norms
                    ):
                        matching_courses.append((doc_name, data))

                if len(matching_courses) == 1:
                    # Only one course matches, use it
                    matched_course_key = matching_courses[0][0]
                elif len(matching_courses) > 1:
                    # Multiple courses match - find the course created closest to the module
                    # creation time. The courses were all loaded at the top of this function,
                    # so this no longer re-reads each one from the database per report.
                    best_match = None
                    smallest_time_diff = None

                    for doc_name, data in matching_courses:
                        try:
                            course = courses_by_id.get(data['course_id'])
                            if course and course.created_at:
                                # Ensure both timestamps are timezone-aware for comparison
                                course_created = course.created_at
                                if course_created.tzinfo is None:
                                    # Make timezone-aware if naive
                                    from datetime import timezone as tz
                                    course_created = course_created.replace(tzinfo=tz.utc)

                                time_diff = abs((module_created_at - course_created).total_seconds())
                                if smallest_time_diff is None or time_diff < smallest_time_diff:
                                    smallest_time_diff = time_diff
                                    best_match = doc_name
                        except Exception as e:
                            logger.warning('[REPORT MATCH] Error comparing timestamps for course %s: %s', log_safe(data.get('course_id')), log_safe(e))
                            continue

                    if best_match:
                        matched_course_key = best_match
                    else:
                        # Fallback to first match if no creation times available
                        matched_course_key = matching_courses[0][0]

                    logger.debug('[REPORT MATCH] Timestamp match: report %s (source_doc=%s) -> course %s', log_safe(Path(key).stem), log_safe(source_doc), log_safe(matched_course_key))

            if matched_course_key:
                matched_reports.append((key, meta_data, matched_course_key))
        except (ClientError, json.JSONDecodeError, KeyError) as e:
            logger.warning("Could not parse S3 report metadata '%s': %s", log_safe(key), log_safe(e))

    # One concurrent pass for the module bodies. Each is a small object behind a full round-trip,
    # so reading them one after another was the other half of the wait on this screen; the image
    # count below needs the text, so they cannot simply be skipped.
    content_by_md_key = {}
    if matched_reports:
        md_keys = [key.replace('.json', '.md') for key, _meta, _course_key in matched_reports]
        workers = min(S3_METADATA_MAX_WORKERS, len(md_keys))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for md_key, content in zip(md_keys, pool.map(_read_module_markdown, md_keys)):
                content_by_md_key[md_key] = content

    for key, meta_data, matched_course_key in matched_reports:
        report_id = Path(key).stem
        course_id = grouped[matched_course_key].get('course_id')
        seg_index = meta_data.get("seg_index", 0)
        document_order = meta_data.get("document_order", 0)  # Read document order from metadata
        is_completed = bool(course_id) and seg_index in completed_modules.get(course_id, set())
        content = content_by_md_key.get(key.replace('.json', '.md'), "")

        # PERFORMANCE FIX: Only validate images when explicitly requested
        # Skip validation during module list loading to prevent timeouts
        if validate_images:
            try:
                content = filter_non_existent_images(content)
            except Exception as e:
                logger.warning("Could not validate images for module '%s': %s", log_safe(report_id), log_safe(e))

        report_info = ReportInfo( filename=f"{report_id}.md", title=meta_data.get("title", "Untitled"), seg_index=seg_index, is_completed=is_completed, course_id=course_id, image_count=count_images_in_module(content), document_order=document_order )
        if include_content:
            report_info.content = content
        grouped[matched_course_key]['reports'].append(report_info)
        logger.debug('[REPORT MATCH] Matched report %s (source_doc=%s, document_order=%s) to course %s', log_safe(report_id), log_safe(meta_data.get("source_doc")), log_safe(document_order), log_safe(matched_course_key))

    # Question counts and assessment configuration for every course on the page, in two queries
    # instead of three per course.
    active_question_counts = {}
    rejected_question_counts = {}
    configured_course_ids = set()
    if course_ids:
        for row in db.query(
            AssessmentQuestion.course_id,
            AssessmentQuestion.is_rejected,
            func.count(AssessmentQuestion.id),
        ).filter(
            AssessmentQuestion.course_id.in_(course_ids)
        ).group_by(
            AssessmentQuestion.course_id, AssessmentQuestion.is_rejected
        ).all():
            if row[1] is None:
                # Matched neither of the two `is_rejected` filters this replaced.
                continue
            counts = rejected_question_counts if row[1] else active_question_counts
            counts[row[0]] = counts.get(row[0], 0) + row[2]
        configured_course_ids = {
            row[0]
            for row in db.query(AssessmentConfig.course_id)
            .filter(AssessmentConfig.course_id.in_(course_ids))
            .all()
        }

    final_list = []
    for doc_name, data in grouped.items():
        # CRITICAL FIX: Sort by document_order first (to group modules by document), then by seg_index
        # This ensures modules from the same document are grouped together in the correct order
        sorted_reports = sorted(data['reports'], key=lambda r: (r.document_order, r.seg_index))
        course_id = data['course_id']
        
        logger.info("[MODULE SORTING] Course '%s': Sorted %s modules by (document_order, seg_index)", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), len(sorted_reports))
        for idx, report in enumerate(sorted_reports, 1):
            logger.debug("[MODULE SORTING]   Position %s: document_order=%s, seg_index=%s, title='%s'", ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), ("" if report.document_order is None else str(report.document_order).replace("\n", "").replace("\r", "")), ("" if report.seg_index is None else str(report.seg_index).replace("\n", "").replace("\r", "")), ("" if report.title is None else str(report.title).replace("\n", "").replace("\r", "")))
        
        # Question count and assessment config for this course, read from the maps built above
        question_count = active_question_counts.get(course_id, 0) if course_id else 0
        rejected_question_count = rejected_question_counts.get(course_id, 0) if course_id else 0
        assessment_configured = bool(course_id) and course_id in configured_course_ids


        final_list.append(GroupedReport(
            source_doc=doc_name, 
            is_published=data['is_published'], 
            reports=sorted_reports, 
            course_id=course_id,
            question_count=question_count,
            rejected_question_count=rejected_question_count,
            assessment_configured=assessment_configured,
            course_title=data['course_title'],
            source_documents=data['source_documents'],
            source_document_display_names=data.get('source_document_display_names'),
        ))
    
    return [g.model_dump() for g in final_list]

# ==============================================================================
# 7.5 AGENT & TOOLS SETUP
# ==============================================================================
class AgentToolContext(BaseModel):
    """The fields an agent tool may read about the request it is serving.

    Kept as the schema for those fields; the value the tools actually read is per-request and
    lives in the ContextVar below.
    """

    user_id: str
    # Optional so a context can be built without opening a session. It used to hold a
    # SessionLocal() created at import time that nothing ever used and nothing ever closed.
    db: Optional[Session] = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


AgentToolContext.model_rebuild()

# Which user an agent tool is acting for is per-request state, so it lives in a ContextVar rather
# than on one object shared by every request.
#
# ``tool_context.user_id`` scopes every KB search, S3 prefix and report lookup in this module. It
# used to be a single module-level AgentToolContext: a chat request assigned it and then returned a
# StreamingResponse, so the agent - and the tools reading the field - ran long after the assignment.
# Two overlapping chats meant the second request's id overwrote the first while the first's tools
# were still running, and that request searched the wrong user's namespace. With 30-40 learners
# chatting at once that is a cross-user data leak, not merely a wrong answer.
#
# The ContextVar holds one mutable dict per request and writes mutate that dict in place, rather
# than holding a value per field. That distinction matters: a context is copied on task and thread
# entry, so a tool that rebound the ContextVar inside a child context would be invisible to the
# caller that reads the result once streaming finishes. Sharing one dict keeps writes within a
# single request mutually visible - the old behaviour - while giving each request its own dict.
_AGENT_TOOL_CONTEXT_FIELDS = frozenset(AgentToolContext.model_fields)
_agent_tool_context_state: ContextVar[Dict[str, Any]] = ContextVar("agent_tool_context_state")


def _new_agent_tool_state(user_id: str = "") -> Dict[str, Any]:
    return {"user_id": user_id, "db": None}


def _agent_tool_state() -> Dict[str, Any]:
    """This request's tool state, created on demand when there is no request."""
    try:
        return _agent_tool_context_state.get()
    except LookupError:
        # Reached from a startup task, a script or a test rather than a chat request. Give the
        # caller its own state instead of one shared with whichever request runs next.
        state = _new_agent_tool_state()
        _agent_tool_context_state.set(state)
        return state


class _RequestScopedToolContext:
    """An attribute view over the calling request's agent tool state.

    Preserves the ``tool_context.user_id`` spelling every tool already uses, so this changes where
    the value lives rather than rewriting each of the tools that read it.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return _agent_tool_state()[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in _AGENT_TOOL_CONTEXT_FIELDS:
            raise AttributeError(name)
        _agent_tool_state()[name] = value


tool_context = _RequestScopedToolContext()


def begin_agent_tool_context(user_id: str) -> None:
    """Start a fresh tool context for one request, before the agent begins streaming.

    Call this from the request's own task rather than from inside the streaming generator, so the
    state is installed in a context that both the generator and its worker thread inherit.
    """
    _agent_tool_context_state.set(_new_agent_tool_state(user_id))


def _open_tool_session() -> Session:
    """
    Open a session for one agent tool call and publish it on the request's context.

    Tool calls within one request share that context, so two concurrent calls both write to
    ``tool_context.db``. Each tool therefore keeps its own reference and closes *that* session in
    its ``finally`` - closing ``tool_context.db`` instead would close the other call's session and
    leak this one's connection, which is how a handful of concurrent tutor questions drained the
    pool.
    """
    db = SessionLocal()
    tool_context.db = db
    return db


@tool
def search_knowledge_base(query: str) -> str:
    """Searches the user's private knowledge base for information relevant to the query and returns the findings. This should be the first tool you use for any informational question. Returns top 5 most relevant results."""
    top_k = 5  # Fixed to 5 for better context
    tool_db = _open_tool_session()
    try:
        # Check if user is a trainee - if so, search in enrolled courses
        user = tool_db.query(User).filter(User.id == tool_context.user_id).first()

        if user and user.role == 'trainee':
            logger.info('[KB SEARCH] Trainee %s searching for: %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if query[:100] is None else str(query[:100]).replace("\n", "").replace("\r", "")))
            
            # CRITICAL FIX: Get only PUBLISHED courses the trainee is enrolled in
            # Trainee can access ALL content from courses shared with them (not just completed modules)
            enrollments = tool_db.query(TraineeAttempt).join(Course).filter(
                TraineeAttempt.trainee_id == user.id,
                Course.is_live == True  # Only published courses
            ).all()
            
            logger.info('[KB SEARCH] Found %s published course enrollments for trainee', len(enrollments))
            
            if not enrollments:
                logger.warning('[KB SEARCH] Trainee %s has no published course enrollments', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")))
                return "I apologize, but I can only answer questions about the courses that have been shared with you by your trainer. You don't appear to be enrolled in any published courses yet. Please contact your trainer for assistance."
            
            # Every document the trainee may read, grouped by the trainer whose namespace holds it.
            # A course can be built from several documents, each indexed under its own name, so
            # this reads the course's full document list - the primary name alone left every
            # document after the first unsearchable.
            docs_by_author: Dict[str, Set[str]] = {}
            for enrollment in enrollments:
                docs_by_author.setdefault(enrollment.course.author_id, set()).update(
                    course_source_document_names(enrollment.course)
                )
            accessible_courses = set().union(*docs_by_author.values())
            logger.info('[KB SEARCH] Trainee has access to courses: %s', ("" if accessible_courses is None else str(accessible_courses).replace("\n", "").replace("\r", "")))
            # Storage names are randomized (VAPT #4) and stay the match/authorization key, but
            # the "Source:" line reaches the chat View Context panel and must name the document.
            source_display_names = {}
            for enrollment in enrollments:
                enrolled_map = course_document_display_map(enrollment.course)
                source_display_names.update(enrolled_map)
                course_doc = enrollment.course.source_doc_name
                if course_doc and course_doc not in source_display_names:
                    source_display_names[course_doc] = friendly_document_name(course_doc, enrolled_map)
            
            # One query per trainer, restricted to the trainee's documents BEFORE ranking. Querying
            # once per course searched a trainer's namespace again for each of their courses
            # (duplicate chunks in the top 5), and filtering only after Pinecone ranked let other
            # courses' chunks take every slot, so the trainee got nothing from their own course.
            ranked: List[Tuple[Any, str]] = []  # (match, the trainer whose namespace it came from)
            seen_chunks: Set[Tuple[str, Any]] = set()
            query_vector = embedding_model.embed_query(query)

            for course_author_id, author_docs in docs_by_author.items():
                author_label = ("" if course_author_id is None else str(course_author_id).replace("\n", "").replace("\r", ""))
                logger.info("[KB SEARCH] Searching namespace %s for %s document(s)", author_label, len(author_docs))
                try:
                    search_response = pinecone_index.query(
                        namespace=course_author_id,
                        vector=query_vector,
                        top_k=top_k,
                        include_metadata=True,
                        filter={"source": {"$in": sorted(author_docs)}},
                    )
                    matches = search_response.get('matches') or []
                    for match in matches:
                        source = match['metadata'].get('source', '')
                        # Still checked here: the filter narrows the search, this decides access.
                        if source not in author_docs:
                            logger.info("[KB SEARCH] Excluding content from course '%s' (not shared with trainee)", ("" if source is None else str(source).replace("\n", "").replace("\r", "")))
                            continue
                        chunk_key = (course_author_id, match.get('id'))
                        if chunk_key in seen_chunks:
                            continue
                        seen_chunks.add(chunk_key)
                        ranked.append((match, course_author_id))
                    logger.info('[KB SEARCH] Found %s matches in %s', len(matches), author_label)
                except Exception as e:
                    logger.warning('[KB SEARCH] Failed to search namespace %s: %s', author_label, e)
            
            if not ranked:
                logger.warning("[KB SEARCH] ✗ No matches found across all enrolled courses")
                return "I apologize, but I cannot find information about that topic in the courses that have been shared with you. This might be outside the scope of your enrolled courses. Please ask your trainer if you need clarification."
            
            # Sort by score and take top_k
            ranked.sort(key=lambda pair: pair[0].get('score', 0), reverse=True)
            
            # INTELLIGENT IMAGE FILTERING: Apply query-based image relevance filtering
            top_ranked = ranked[:top_k]
            top_matches = [match for match, _ in top_ranked]
            logger.info('[KB SEARCH] Processing %s matches with AI-driven semantic filtering', len(top_matches))
            
            # Load image analysis data for intelligent filtering
            doc_names = set()
            for match in top_matches:
                source = match['metadata'].get('source', '')
                if source:
                    doc_names.add(source)
            
            # Load cached image analyses for all relevant documents
            cached_analyses = {}
            result_documents = {
                (author_id, match['metadata'].get('source', ''))
                for match, author_id in top_ranked
                if match['metadata'].get('source', '') in doc_names
            }
            for course_author_id, doc_name in sorted(result_documents):
                doc_analyses = load_image_analysis_from_s3(course_author_id, doc_name)
                cached_analyses.update(doc_analyses)
                logger.info('[KB SEARCH] Loaded %s cached analyses for %s', len(doc_analyses), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            
            # INTELLIGENT CONTENT PROCESSING with query-aware image filtering
            context_parts = []
            images_added = False  # Track if we've already added images
            
            for match, course_author_id in top_ranked:
                source = match['metadata'].get('source', 'Unknown')
                text_content = match['metadata'].get('text', '')
                # The trainer is the namespace this chunk came from. Looking it up by document
                # name found nothing for a course's later documents, which dropped those chunks,
                # and could pick the wrong trainer when two share a (pre-VAPT #4) filename.
                
                if course_author_id:
                    # CRITICAL FIX: Add images from cached analyses to the content ONLY ONCE
                    # The vector store chunks don't contain images, so we need to add them
                    # But we should only add them to the FIRST chunk to avoid duplicates
                    if not images_added and source in doc_names and cached_analyses:
                        # Find images that belong to this document
                        relevant_images = [s3_key for s3_key in cached_analyses.keys() if source in s3_key]
                        if relevant_images:
                            # Add images to the text content
                            image_tags = '\n\n'.join([f'[IMAGE: {s3_key}]' for s3_key in relevant_images])
                            text_content = f"{text_content}\n\n{image_tags}"
                            images_added = True  # Mark that we've added images
                            logger.info('[KB SEARCH] Added %s images from %s to content', len(relevant_images), ("" if source is None else str(source).replace("\n", "").replace("\r", "")))
                    
                    # Apply intelligent image filtering based on query relevance
                    filtered_content = filter_images_by_query_relevance(text_content, query, cached_analyses, course_author_id)
                    
                    # Basic validation
                    clean_content = validate_and_clean_image_tags(filtered_content, course_author_id)
                    
                    # CRITICAL FIX: Replace any UUID in image paths with the correct course author UUID
                    # This fixes the issue where vector store has old/wrong UUIDs in image paths
                    if '[IMAGE:' in clean_content:
                        # Find all image tags
                        import re
                        image_pattern = r'\[IMAGE:\s*([^\]]+)\]'
                        
                        def fix_image_uuid(match):
                            s3_key = match.group(1).strip()
                            # Check if this looks like a UUID-based path
                            if '/' in s3_key and '/extracted_images/' in s3_key:
                                # Extract the UUID part (first segment before /)
                                parts = s3_key.split('/')
                                if len(parts) >= 3:
                                    # Replace the first part (UUID) with the correct course author UUID
                                    parts[0] = course_author_id
                                    corrected_key = '/'.join(parts)
                                    logger.info('[KB SEARCH] Fixed image UUID: %s -> %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), ("" if corrected_key is None else str(corrected_key).replace("\n", "").replace("\r", "")))
                                    return f'[IMAGE: {corrected_key}]'
                            return match.group(0)
                        
                        clean_content = re.sub(image_pattern, fix_image_uuid, clean_content)
                    
                    source_label = source_display_names.get(source) or friendly_document_name(source)
                    context_parts.append(f"Source: {source_label}\nContent: {clean_content}")
            
            context = "\n\n---\n\n".join(context_parts)
            
            # Essential validation: remove non-existent images
            context = filter_non_existent_images(context)
            
            # Enhanced logging for debugging
            if '[IMAGE:' in context:
                image_count = context.count('[IMAGE:')
                logger.info("[KB SEARCH] ✓ Prepared %s chunks with %s image(s) - AI will apply semantic filtering based on query: '%s...'", len(top_matches), ("" if image_count is None else str(image_count).replace("\n", "").replace("\r", "")), ("" if query[:50] is None else str(query[:50]).replace("\n", "").replace("\r", "")))
            else:
                logger.info("[KB SEARCH] ✓ Prepared %s chunks (no images) - AI will filter based on query: '%s...'", len(top_matches), ("" if query[:50] is None else str(query[:50]).replace("\n", "").replace("\r", "")))
            
            return f"Found the following information from your enrolled courses:\n{context}"
        else:
            # Trainer/Admin: Search in their own namespace
            query_vector = embedding_model.embed_query(query)
            search_response = pinecone_index.query(
                namespace=tool_context.user_id,
                vector=query_vector,
                top_k=top_k,
                include_metadata=True
            )
            if not search_response.get('matches'):
                return "No relevant information found in the knowledge base."
            
            # INTELLIGENT IMAGE FILTERING for trainers/admins
            filtered_search_response = {'matches': search_response['matches']}
            logger.info('[KB SEARCH] Processing %s matches with AI-driven semantic filtering', len(search_response['matches']))
            
            # Load image analysis data for intelligent filtering
            doc_names = set()
            for match in filtered_search_response['matches']:
                source = match['metadata'].get('source', '')
                if source:
                    doc_names.add(source)
            
            # Load cached image analyses for all relevant documents
            cached_analyses = {}
            for doc_name in doc_names:
                doc_analyses = load_image_analysis_from_s3(tool_context.user_id, doc_name)
                cached_analyses.update(doc_analyses)
                logger.info('[KB SEARCH] Loaded %s cached analyses for %s', len(doc_analyses), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            
            # INTELLIGENT CONTENT PROCESSING with query-aware image filtering
            context_parts = []
            images_added = False  # Track if we've already added images
            # Chunk metadata records the randomized STORAGE name (VAPT #4); the "Source:" line
            # reaches the chat View Context panel, so it must name the uploaded document.
            source_display_names = author_document_display_map(tool_db, tool_context.user_id)
            
            for match in filtered_search_response['matches']:
                source = match['metadata'].get('source', 'Unknown')
                text_content = match['metadata'].get('text', '')
                
                # CRITICAL FIX: Add images from cached analyses to the content ONLY ONCE
                # The vector store chunks don't contain images, so we need to add them
                # But we should only add them to the FIRST chunk to avoid duplicates
                if not images_added and source in doc_names and cached_analyses:
                    # Find images that belong to this document
                    relevant_images = [s3_key for s3_key in cached_analyses.keys() if source in s3_key]
                    if relevant_images:
                        # Add images to the text content
                        image_tags = '\n\n'.join([f'[IMAGE: {s3_key}]' for s3_key in relevant_images])
                        text_content = f"{text_content}\n\n{image_tags}"
                        images_added = True  # Mark that we've added images
                        logger.info('[KB SEARCH] Added %s images from %s to content', len(relevant_images), ("" if source is None else str(source).replace("\n", "").replace("\r", "")))
                
                # Apply intelligent image filtering based on query relevance
                filtered_content = filter_images_by_query_relevance(text_content, query, cached_analyses, tool_context.user_id)
                
                # Basic validation
                clean_content = validate_and_clean_image_tags(filtered_content, tool_context.user_id)
                
                # CRITICAL FIX: Replace any UUID in image paths with the correct user UUID
                # This fixes the issue where vector store has old/wrong UUIDs in image paths
                if '[IMAGE:' in clean_content:
                    # Find all image tags
                    import re
                    image_pattern = r'\[IMAGE:\s*([^\]]+)\]'
                    
                    def fix_image_uuid(match):
                        s3_key = match.group(1).strip()
                        # Check if this looks like a UUID-based path
                        if '/' in s3_key and '/extracted_images/' in s3_key:
                            # Extract the UUID part (first segment before /)
                            parts = s3_key.split('/')
                            if len(parts) >= 3:
                                # Replace the first part (UUID) with the correct user UUID
                                parts[0] = tool_context.user_id
                                corrected_key = '/'.join(parts)
                                logger.info('[KB SEARCH] Fixed image UUID: %s -> %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), ("" if corrected_key is None else str(corrected_key).replace("\n", "").replace("\r", "")))
                                return f'[IMAGE: {corrected_key}]'
                        return match.group(0)
                    
                    clean_content = re.sub(image_pattern, fix_image_uuid, clean_content)
                
                source_label = display_name_from_map(source_display_names, source)
                context_parts.append(f"Source: {source_label}\nContent: {clean_content}")
            
            context = "\n\n---\n\n".join(context_parts)
            
            # Essential validation: remove non-existent images
            context = filter_non_existent_images(context)
            
            # Enhanced logging for debugging
            if '[IMAGE:' in context:
                image_count = context.count('[IMAGE:')
                logger.info(
                    "[KB SEARCH] Prepared %s chunks with %s image(s) - query: %s",
                    len(filtered_search_response["matches"]),
                    image_count,
                    ("" if query[:50] is None else str(query[:50]).replace("\n", "").replace("\r", "")),
                )
            else:
                logger.info(
                    "[KB SEARCH] Prepared %s chunks (no images) - query: %s",
                    len(filtered_search_response["matches"]),
                    ("" if query[:50] is None else str(query[:50]).replace("\n", "").replace("\r", "")),
                )
            
            return f"Found the following information:\n{context}"
    except Exception as e:
        logger.error(
            "Agent tool 'search_knowledge_base' failed for user %s: %s",
            ("" if tool_context.user_id is None else str(tool_context.user_id).replace("\n", "").replace("\r", "")),
            e,
        )
        return "An error occurred while searching the knowledge base."
    finally:
        tool_db.close()
@tool
def list_my_reports() -> str:
    """Lists all the generated reports and their source documents for the current user."""
    tool_db = _open_tool_session()
    try:
        grouped_reports = _get_grouped_reports(tool_context.user_id, tool_db)
        if not grouped_reports: return "You have no generated reports."
        report_list_str = "Here are your reports, grouped by the original document:\n"
        for group in grouped_reports:
            # source_doc is the randomized storage name (VAPT #4) - the agent must quote the
            # course title / uploaded filename back to the user, never a hex string.
            display_names = group.get('source_document_display_names') or []
            group_label = (
                group.get('course_title')
                or (display_names[0] if display_names else "")
                or friendly_document_name(group['source_doc'])
            )
            report_list_str += f"\n\nSource Document: \"{group_label}\"\n"
            for report in group['reports']:
                 report_list_str += f"- Report: \"{report['title']}\" (Filename for tools: {report['filename']})\n"
        return report_list_str.strip()
    finally:
        tool_db.close()
@tool
def get_report_content(report_filename: str) -> str:
    """Reads and returns the full markdown content of a specific report given its filename."""
    try:
        s3_key = f"{tool_context.user_id}/generated_reports/{report_filename}"
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        content = obj['Body'].read().decode('utf-8')
        return f"Content of '{report_filename}':\n\n---\n\n{content}"
    except ClientError:
        return f"Error: Report with filename '{report_filename}' not found."
    except Exception as e:
        logger.error(
            "Error in get_report_content tool for user %s: %s",
            ("" if tool_context.user_id is None else str(tool_context.user_id).replace("\n", "").replace("\r", "")),
            e,
        )
        return "An unexpected error occurred while fetching the report."
@tool
def list_users_in_my_process() -> str:
    """Lists the names and emails of colleagues who reports can be shared with."""
    tool_db = _open_tool_session()
    try:
        user = tool_db.query(User).filter(User.id == tool_context.user_id).first()
        if user.role == 'super_admin':
            users = tool_db.query(User).filter(User.id != user.id).all()
            if not users: return "There are no other users in the system to share with."
            user_list = "All users in the system:\n"
            for u in users: user_list += f"- Email: {u.email}\n"
            return user_list
        if not user.process_id: return "You are not assigned to a Process. Sharing is not possible."
        colleagues = tool_db.query(User).filter(User.process_id == user.process_id, User.id != user.id).all()
        if not colleagues: return f"There are no other users in your Process ('{user.process.name}') to share with."
        colleague_list = f"Users in your Process ('{user.process.name}'):\n"
        for c in colleagues: colleague_list += f"- Email: {c.email}\n"
        return colleague_list
    finally:
        tool_db.close()
@tool
def share_reports_with_colleague(source_document_name: str, recipient_email: str) -> str:
    """Shares all reports generated from a specific source document with a colleague."""
    db = SessionLocal()
    try:
        from services.agents import _execute_share_reports

        recipient = db.query(User).filter(User.email == recipient_email).first()
        if not recipient: return f"Action failed: User with email '{recipient_email}' not found."
        return _execute_share_reports(db, tool_context.user_id, recipient.id, source_document_name)
    finally:
        db.close()
# Phani | 25-Jun-2026 | DF_27 | Trainee_study | Low
# Fix: Prompt tells agent to answer directly without meta preamble text.
AGENT_SYSTEM_PROMPT = """You are a knowledgeable and supportive 'Smart Tutor' assistant. Your main role is to provide answers and insights based EXCLUSIVELY on a user's private collection of documents and reports. Your entire response must be in well-formatted Markdown.

**CRITICAL - DIRECT RESPONSE (NO META-COMMENTARY):**
- Start immediately with the substantive answer (definition, heading, or first fact).
- NEVER restate or summarize the user's question (e.g., do NOT say "The user is asking..." or "The user's query is...").
- NEVER describe your plan, reasoning, or next steps (e.g., do NOT say "I need to provide...", "I will search...", or "I should first...").
- Keep all internal reasoning private; only output the final answer the user should read.

**🚨 CRITICAL RULE - KNOWLEDGE BASE ONLY (ABSOLUTE ENFORCEMENT):**

**YOU MUST NEVER ANSWER FROM YOUR GENERAL KNOWLEDGE OR TRAINING DATA**

- **MANDATORY**: For ANY informational question, you MUST call the `search_knowledge_base` tool FIRST
- **FORBIDDEN**: You are STRICTLY PROHIBITED from answering questions using your general knowledge, training data, or any information not retrieved from the knowledge base
- **REQUIRED BEHAVIOR**: If `search_knowledge_base` returns no results or says "Could not find", you MUST respond with: "I cannot find information about that topic in your enrolled courses. Please contact your trainer if you need information about topics not covered in your course materials."
- **ZERO TOLERANCE**: Do NOT provide general knowledge answers, explanations, or information that is not explicitly from the knowledge base search results
- **STRICT ENFORCEMENT**: Even if you know the answer from your training, you MUST NOT use it - ONLY use knowledge base results

**EXAMPLES OF CORRECT BEHAVIOR:**
✅ User asks about "hard vs soft tactics" → Call `search_knowledge_base` → If no results → Say "I cannot find information about that topic in your enrolled courses"
✅ User asks about course content → Call `search_knowledge_base` → Use ONLY the returned results → Provide answer based on search results
✅ User asks about a concept → Call `search_knowledge_base` → If results found → Answer using ONLY those results

**EXAMPLES OF FORBIDDEN BEHAVIOR:**
❌ User asks question → You answer from general knowledge → Then say "not in knowledge base"
❌ User asks question → You provide explanation from training data → Without calling search tool
❌ User asks question → Search returns nothing → You provide general knowledge answer anyway
❌ User asks question → You answer first → Then call search tool as an afterthought

**ENFORCEMENT PROTOCOL:**
1. Receive user question
2. IMMEDIATELY call `search_knowledge_base` tool (DO NOT answer first)
3. Wait for search results
4. IF results found → Answer using ONLY those results
5. IF no results found → Say "I cannot find information about that topic in your enrolled courses"
6. NEVER EVER use your general knowledge or training data to answer

**CRITICAL - Image Handling Rules (ULTRA-STRICT ENFORCEMENT):**

🚨 **ABSOLUTE RULE: ZERO TOLERANCE FOR FAKE IMAGES** 🚨

**IF SEARCH RESULTS CONTAIN NO IMAGES → DO NOT USE ANY IMAGES**
**IF SEARCH RESULTS CONTAIN IMAGES → ONLY USE THOSE EXACT IMAGES**

- **ONLY copy-paste image tags EXACTLY as they appear in knowledge base search results**
- **NEVER generate, create, or invent ANY image paths under ANY circumstances**
- **EVERY image tag MUST be a real S3 key from search results**
- **IF NO IMAGES IN SEARCH RESULTS → PROVIDE TEXT-ONLY ANSWER**
- **USE [IMAGE: s3_key] FORMAT, NOT MARKDOWN IMAGE SYNTAX**

✅ **MANDATORY VALIDATION CHECKLIST** - Before using ANY image tag:
1. ✅ The EXACT S3 key appears in the search results (copy-paste only)
2. ✅ The S3 key starts with a user ID (UUID format like `a97c466d-1dff-4e0c-b1d2-73414d04e8aa/`)
3. ✅ The S3 key contains `/extracted_images/`
4. ✅ The S3 key is longer than 30 characters
5. ✅ You are copying the EXACT string from search results (no modifications)
6. ✅ You are using [IMAGE: s3_key] format, NOT ![alt](url) markdown syntax

❌ **FORBIDDEN PATTERNS** (NEVER USE THESE):
- ANY path starting with `s3://`
- Generic names: `diagram.png`, `architecture.png`, `flowchart.png`, `generic_diagram.png`, `MCP_Architecture_Diagram.png`, `ACP_Architecture_Diagram.png`, `ANP_Architecture_Diagram.png`
- Placeholder paths: `path/to/image.png`, `user/images/diagram.png`
- Made-up bucket names: `fake-diagrams/`, `diagrams/`, `bucket/`
- Short paths (less than 30 characters)
- Paths without user IDs or `/extracted_images/`
- ANY image name you think sounds reasonable but didn't see in search results
- Markdown image syntax: `![alt](url)` - NEVER use this format

🎯 **TRAINEE PRIORITY RULE**: For trainee users, include ALL relevant images from search results to ensure comprehensive visual learning support. Do not filter out images unless they are completely unrelated to the query.

✅ **CORRECT EXAMPLES** (using exact S3 keys from search results):
```
[IMAGE: a97c466d-1dff-4e0c-b1d2-73414d04e8aa/extracted_images/test.pdf/appropaiteImage_reference.png]
[IMAGE: user123/extracted_images/document.pdf/Architecture_diagram_showing_components.png]
```

❌ **FORBIDDEN EXAMPLES** (NEVER create these):
```
[IMAGE: generic_architecture.png]
[IMAGE: s3://bucket/diagram.png]
[IMAGE: diagrams/architecture_overview.png]
[IMAGE: architecture_diagram.png]
[IMAGE: flowchart.png]
[IMAGE: MCP_Architecture_Diagram.png]
[IMAGE: ACP_Architecture_Diagram.png]
[IMAGE: ANP_Architecture_Diagram.png]
![Multi-Head Attention](2c161964-da09-4309-82cd-0d220c6538b1/extracted_images/attention.pdf/diagram.png)
![Architecture Diagram](path/to/image.png)
```

**ZERO EXCEPTIONS**: If search results contain NO image tags or say "(no images)", you MUST provide comprehensive text-based explanations. DO NOT create ANY image tags. It is better to have NO images than FAKE images.

**CRITICAL - Conversation Memory & Context:**
- You have FULL ACCESS to the conversation history with this user through the message history
- ALWAYS check the conversation history BEFORE asking for clarification
- When the user uses pronouns (it, that, this, they, etc.) or says "tell me more", "explain that", "what about...", you MUST look at the previous messages to understand what they're referring to
- DO NOT ask "what does 'it' refer to?" - instead, look at the last few messages to determine the context
- If the user asked about "Python" in the previous message, then "it" or "that" in the current message refers to Python
- Maintain full context across the entire conversation - remember ALL topics discussed, documents referenced, and information provided
- For follow-up questions, build upon your previous responses rather than starting from scratch
- Example: If user asks "What is Python?" then "Tell me more about it", you should know "it" = Python and provide additional Python information

**Your Core Directive - Follow this logic in order:**

0.  **🎯 ULTRA-INTELLIGENT RELEVANCE (HIGHEST PRIORITY):** You are an AI with advanced reasoning capabilities. You MUST use your intelligence to understand exactly what the user is asking for and ONLY provide content that directly matches their request.
    
    **CRITICAL INTELLIGENCE RULES:**
    - If user asks about a specific system/protocol/concept, show ONLY content about that exact topic
    - If user explicitly excludes something (e.g., "not X"), absolutely exclude that content
    - Different systems/protocols/concepts are DIFFERENT topics - treat them as completely separate
    - Use your semantic understanding to distinguish between related but different concepts
    - Apply common sense reasoning: if content doesn't match the query, don't use it
    
    **VERIFICATION STEP:** Before using ANY content or image, ask yourself: "Does this EXACTLY match what the user asked for?" If not, exclude it.

1.  **Strict Context Rule:** If the user's latest prompt begins with the phrase "Based strictly on the following context...", you **MUST** answer using ONLY the provided context. Do not use any tools or external knowledge. This is your highest priority rule.

2.  **Internal Knowledge First - MANDATORY TOOL USAGE:** For any informational question (e.g., "what are the skills of Phani?", "summarize the financial report", "explain hard vs soft tactics"), you **MUST** use the `search_knowledge_base` tool as your FIRST action. This is your primary and ONLY source of truth. DO NOT answer from your general knowledge.
    
    **🧠 AI-POWERED INTELLIGENT CONTENT FILTERING (CRITICAL):**
    
    You are an advanced AI with sophisticated reasoning capabilities. After searching, you MUST apply your full intelligence to understand and filter content with perfect accuracy:
    
    **STEP 1: DEEP QUERY UNDERSTANDING**
    - Use your AI reasoning to understand the user's EXACT intent and requirements
    - Identify ALL topics, concepts, systems, entities, or subjects the user is asking about
    - Understand the context: comparison queries, specific requests, exploratory questions, etc.
    - Recognize when users want information about multiple related or different topics
    - Apply semantic understanding to distinguish between similar but different concepts
    
    **STEP 2: INTELLIGENT CONTENT ANALYSIS**
    - For EACH piece of content returned by search, use your AI intelligence to determine: "Does this DIRECTLY and PRECISELY match what the user asked for?"
    - Apply semantic reasoning to understand content meaning, not just keyword matching
    - Use your knowledge to distinguish between different but related concepts
    - Be extremely intelligent about relevance: related but different = EXCLUDE
    - Apply domain expertise to understand technical distinctions and relationships
    
    **STEP 3: AI-DRIVEN IMAGE INTELLIGENCE**
    - Use your advanced reasoning to analyze image context, filenames, and surrounding content
    - Apply semantic understanding to determine what each image actually represents
    - Use your intelligence to match images to user queries with perfect precision
    - Analyze image metadata, descriptions, and context to understand their true content
    - ONLY include images that your AI reasoning determines are directly relevant to the user's query
    - Apply sophisticated filtering: better to show no images than irrelevant ones
    - Use contextual intelligence to understand image purpose and relevance
    
    **STEP 4: PRECISION FILTERING WITH AI REASONING**
    - Apply your full AI capabilities to ensure perfect content matching
    - Use semantic understanding and domain knowledge to make intelligent decisions
    - Apply common sense and logical reasoning to filter content appropriately
    - Be 100% accurate using your AI intelligence: Better to show nothing than wrong information
    - Use your reasoning to understand user exclusions, inclusions, and specific requirements
    
    **AI INTELLIGENCE EXAMPLES:**
    ✅ Use semantic reasoning to understand query intent and match content accordingly
    ✅ Apply domain knowledge to distinguish between different technical concepts
    ✅ Use contextual intelligence to determine image relevance and appropriateness
    ✅ Apply logical reasoning to handle comparison queries and specific requests
    ✅ Use your AI capabilities to understand user exclusions and requirements
    ❌ NEVER rely on simple keyword matching - use full AI reasoning
    ❌ NEVER ignore semantic differences between concepts
    ❌ NEVER show content that your AI reasoning determines is irrelevant
    ❌ NEVER bypass your intelligence with hardcoded rules
    
    **AI ACCURACY REQUIREMENT**: Use your full AI intelligence to be 100% accurate - apply sophisticated reasoning to ensure perfect content matching
    
    **CRITICAL - IF NO RESULTS FOUND:**
    If the knowledge base search returns no results or says "Could not find", you MUST respond with:
    "I cannot find information about that topic in your enrolled courses. Please contact your trainer if you need information about topics not covered in your course materials."
    
    **ABSOLUTELY FORBIDDEN**: Do NOT provide answers from your general knowledge or training data when search returns no results.
    
    **For follow-up questions:** If the user is asking about something you discussed earlier in the conversation AND you already have that information from a previous search, you may use that information from the conversation history without searching again.

3.  **No External Sources - STRICT ENFORCEMENT:** You do NOT have access to internet search or your general knowledge for answering questions. You MUST ONLY use the `search_knowledge_base` tool. If the knowledge base search fails to find relevant information, you MUST respond with "I cannot find information about that topic in your enrolled courses" - DO NOT provide general knowledge answers under ANY circumstances.

4.  **Use Specific Tools for Specific Actions:** If the user asks to perform a direct action, use the appropriate tool:
    *   To see a list of available documents or reports, use `list_my_reports`.
    *   To read the full content of a specific report, use `get_report_content`.
    *   To see a list of colleagues to share with, use `list_users_in_my_process`.
    *   To share a document with a colleague, use `share_reports_with_colleague`.

5.  **Conversational Fallback:** For general greetings or non-informational chat (e.g., "hello", "thank you"), respond naturally without using any tools.
"""

# System prompt for trainers and admins — can answer general questions from their own knowledge
TRAINER_ADMIN_SYSTEM_PROMPT = """You are a knowledgeable 'Smart Tutor' assistant for a trainer/admin user. Your role is to help with both knowledge-base questions AND general questions.

**CRITICAL - DIRECT RESPONSE (NO META-COMMENTARY):**
- Start immediately with the substantive answer (definition, heading, or first fact).
- NEVER restate or summarize the user's question (e.g., do NOT say "The user is asking..." or "The user's query is...").
- NEVER describe your plan, reasoning, or next steps (e.g., do NOT say "I need to provide...", "I will search...", or "I should first...").
- Keep all internal reasoning private; only output the final answer the user should read.

**KNOWLEDGE BASE FIRST:**
- For questions that may be in the knowledge base, ALWAYS call `search_knowledge_base` first
- If results are found, answer using those results
- If no results are found, you MAY answer from your general knowledge — you are NOT restricted to the knowledge base only

**GENERAL KNOWLEDGE:**
- Trainers and admins can ask general questions (e.g., "What is A2A?", "Explain transformers")
- If the knowledge base has no relevant results, provide a helpful answer from your general knowledge
- Do NOT say "contact your trainer" — you ARE the trainer/admin

**CRITICAL - Image Handling Rules:**
- ONLY use image tags that appear EXACTLY in knowledge base search results
- NEVER generate, create, or invent image paths
- If search results contain no images, provide text-only answers
- Use [IMAGE: s3_key] format only, never markdown image syntax

**CRITICAL - Conversation Memory:**
- Use conversation history to understand pronouns and follow-up questions
- Build on previous responses rather than starting from scratch

**Your process:**
1. For informational questions → call `search_knowledge_base` first
2. If results found → answer using those results
3. If no results → answer from general knowledge (you are allowed to do this)
4. For greetings/non-informational chat → respond naturally without tools
5. For list/share actions → use the appropriate tool
"""

# ENHANCED PROMPT FOR BETTER IMAGE DESCRIPTIONS
TUTOR_AGENT_INSTRUCTIONS = """You are Professor Anya — a world-class educator, storyteller, and mentor. You don't just explain concepts; you make them *click*. Your lectures feel like a conversation with the smartest, most enthusiastic teacher a student has ever had. You use vivid analogies, real-world stories, worked examples, and interactive moments to make every concept stick.

**CRITICAL: DO NOT produce a document summary, table of contents, document classification, or phase headers. Deliver a flowing, spoken-style lecture that feels alive.**

---

### MANDATORY: MODULE OVERVIEW CARD (ALWAYS FIRST)

Before the lecture begins, output this exact block:

```
[MODULE_OVERVIEW]
TITLE: <the module's main topic in one line>
WHAT_WE_COVER: <4-6 bullet points listing the key concepts this module covers, derived from the actual content>
WHY_IT_MATTERS: <one sentence explaining the real-world relevance — what problem does this solve or what becomes possible?>
[/MODULE_OVERVIEW]
```

---

### RULE 1: 100% CONTENT COVERAGE — NO EXCEPTIONS

You MUST cover every concept, data point, process, formula, table, and example in the module. Nothing gets skipped. Nothing gets "summarized away." If the module has 8 sections, your lecture addresses all 8. If it has a formula, you explain every variable. If it has a table, you convert it (see Rule 3).

---

### RULE 2: DEPTH + EXAMPLES FOR EVERY CONCEPT

For every major concept you teach, you MUST provide ALL of the following:

**a) Plain-language definition** — explain it as if the student has never heard of it before. No jargon without explanation.

**b) How it works** — the mechanism. What are the inputs? What happens inside? What comes out? Walk through it step by step.

**c) A concrete real-world example** — not a generic one. Use a specific scenario, a named company, a real number, a relatable situation. Say "For example, imagine you're a bank processing 10,000 transactions per second..." not "For example, consider a system..."

**d) An analogy** — connect the concept to something the student already knows from everyday life. "Think of it like a traffic light system where..."

**e) Why it matters** — what goes wrong without it? What becomes possible with it? What's the cost of ignoring it?

**f) A "gotcha" or common misconception** — what do people often get wrong about this? What's the trap beginners fall into?

---

### RULE 3: TABLES MUST BECOME RICH NARRATIVE POINTS

If the module content contains a table, you MUST NOT read it out as a table. Instead, convert each row into a rich, conversational explanation:

**For each table row/entry:**
- Name it clearly: "The first type is **[name]**..."
- Explain what it is in plain language
- Give a real-world example of it in action
- Explain when you'd use it vs. the others
- Connect it to the broader concept

Example: If a table compares 3 database types, don't say "Row 1: MySQL — relational — ACID compliant." Instead say: "Let's start with **MySQL**, which is a relational database. Think of it like a perfectly organized spreadsheet where every piece of data has a fixed home. It's ACID compliant, which means if your bank transfer fails halfway through, the database rolls everything back — your money doesn't disappear into thin air. You'd reach for MySQL when your data has clear relationships and you need rock-solid consistency, like in e-commerce or banking systems."

---

### RULE 4: OPEN WITH A HOOK THAT CREATES URGENCY

Don't start with "Today we'll learn about X." Start with a question, a surprising fact, a mini-story, or a real problem that this module's content solves. The hook must come directly from the module content — not a generic opener.

Examples of strong hooks:
- "Imagine your company just lost $4.6 million because of a single database query that took 47 seconds. That's exactly what happened to [scenario from content]. Today we're going to understand exactly why that happened — and how the concepts in this module prevent it."
- "Here's a question: why do some systems handle 1 million users effortlessly while others crash at 10,000? The answer is in what we're about to explore."

---

### RULE 5: INTERACTIVE CHARTS FOR ALL DATA

When the module contains numeric data, comparisons, rankings, percentages, or measurable metrics, generate a chart:

```
[INTERACTIVE_CHART]
TYPE: <bar|line|pie|doughnut|radar>
TITLE: <chart title derived from the data>
LABELS: <comma-separated labels>
DATA: <comma-separated numeric values matching the labels>
COLOR: <a single hex color like #3b82f6>
[/INTERACTIVE_CHART]
```

After every chart, write 3-4 sentences explaining what the numbers mean in practice — not just what they are.

---

### RULE 6: MANDATORY INTERACTIVE HTML WIDGETS

You MUST generate at least 2 interactive HTML widgets per lecture. More is better. Every process, comparison, concept list, timeline, architecture, or "check your understanding" moment needs a widget.

**MANDATORY widget triggers:**
- Any step-by-step process → clickable "Next Step" stepper revealing each step with explanation
- Any list of 3+ concepts/types → clickable cards that expand to show full explanation + example
- Any comparison (A vs B, pros/cons, before/after) → toggle/tab widget showing each side
- Any workflow → clickable flow with each node showing what happens at that stage
- Any timeline → scrollable timeline with each event showing context
- Any architecture with components → layered/clickable diagram showing each layer's role
- Any formula or calculation → interactive calculator widget where student inputs values and sees the result

**Widget quality rules:**
- Each widget must contain REAL content from the module — actual step names, actual concept names, actual comparison items
- Widgets must be visually polished: clear labels, good spacing, hover effects, smooth transitions
- After every widget, write 2-3 sentences narrating what the widget just demonstrated

**Technical rules:**
- 100% self-contained — inline styles and vanilla JS only, no external scripts
- Dark theme: background `#1e293b`, text `#e2e8f0`, accent `#3b82f6`, border `#334155`
- Responsive width (max-width:100%), height auto-adjusts to content
- All content from the actual module — no invented data

Use this exact format:
```
[INTERACTIVE_HTML]
<div style="font-family:system-ui,sans-serif;padding:20px;background:#1e293b;border-radius:12px;color:#e2e8f0;border:1px solid #334155;max-width:100%;">
  <!-- Complete self-contained HTML + inline CSS + vanilla JS -->
</div>
[/INTERACTIVE_HTML]
```

**EXAMPLE — Concept cards widget (for a list of 3+ concepts):**
```
[INTERACTIVE_HTML]
<div style="font-family:system-ui,sans-serif;padding:20px;background:#1e293b;border-radius:12px;color:#e2e8f0;border:1px solid #334155;max-width:100%;">
  <h3 style="margin:0 0 16px;color:#3b82f6;font-size:1em;text-transform:uppercase;letter-spacing:.05em;">Click a concept to explore it</h3>
  <div id="cards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px;"></div>
  <div id="detail" style="background:#0f172a;border-radius:8px;padding:16px;min-height:80px;border:1px solid #3b82f6;display:none;"></div>
  <script>
    var concepts=[
      {name:"Concept A",def:"Definition from module",example:"Real example from module",why:"Why it matters"},
      {name:"Concept B",def:"Definition from module",example:"Real example from module",why:"Why it matters"},
      {name:"Concept C",def:"Definition from module",example:"Real example from module",why:"Why it matters"}
    ];
    var cards=document.getElementById('cards');
    var detail=document.getElementById('detail');
    concepts.forEach(function(c,i){
      var btn=document.createElement('button');
      btn.textContent=c.name;
      btn.style.cssText='padding:12px;background:#1e3a5f;color:#e2e8f0;border:1px solid #3b82f6;border-radius:8px;cursor:pointer;font-size:.9em;transition:background .2s;';
      btn.onmouseover=function(){this.style.background='#2563eb';};
      btn.onmouseout=function(){this.style.background='#1e3a5f';};
      btn.onclick=function(){
        detail.style.display='block';
        detail.innerHTML='<strong style="color:#3b82f6;font-size:1.05em;">'+c.name+'</strong><p style="margin:8px 0 4px;">'+c.def+'</p><p style="margin:4px 0;color:#94a3b8;font-size:.9em;">📌 Example: '+c.example+'</p><p style="margin:4px 0;color:#86efac;font-size:.9em;">✅ Why it matters: '+c.why+'</p>';
      };
      cards.appendChild(btn);
    });
  </script>
</div>
[/INTERACTIVE_HTML]
```


**EXAMPLE — Step-by-step stepper (for any process with 3+ steps):**
```
[INTERACTIVE_HTML]
<div style="font-family:system-ui,sans-serif;padding:20px;background:#1e293b;border-radius:12px;color:#e2e8f0;border:1px solid #334155;max-width:100%;">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;"><span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#3b82f6;">Step-by-Step Walkthrough</span><span id="sc" style="font-size:12px;color:#64748b;"></span></div>
  <div id="sbar" style="display:flex;gap:4px;margin-bottom:20px;"></div>
  <div id="scontent" style="background:#0f172a;border-radius:8px;padding:20px;min-height:100px;border-left:3px solid #3b82f6;"></div>
  <div style="display:flex;gap:10px;margin-top:16px;">
    <button id="sprev" onclick="sNav(-1)" style="padding:8px 18px;background:#334155;color:#e2e8f0;border:none;border-radius:6px;cursor:pointer;font-size:13px;">← Back</button>
    <button id="snext" onclick="sNav(1)" style="padding:8px 18px;background:#3b82f6;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px;margin-left:auto;">Next →</button>
  </div>
  <script>
    var steps=[{title:"Step 1: [Name]",body:"[Explanation from module]",tip:"💡 [Key insight]"},{title:"Step 2: [Name]",body:"[Explanation]",tip:"💡 [Key insight]"}];
    var si=0;var bar=document.getElementById('sbar');
    steps.forEach(function(_,i){var d=document.createElement('div');d.style.cssText='flex:1;height:4px;border-radius:2px;background:'+(i===0?'#3b82f6':'#334155')+';transition:background .3s;';d.id='sb'+i;bar.appendChild(d);});
    function sRender(){var s=steps[si];document.getElementById('scontent').innerHTML='<strong style="color:#3b82f6;font-size:1.05em;display:block;margin-bottom:10px;">'+s.title+'</strong><p style="line-height:1.7;margin:0 0 12px;">'+s.body+'</p><p style="font-size:.88em;color:#f59e0b;margin:0;">'+s.tip+'</p>';document.getElementById('sc').textContent=(si+1)+' / '+steps.length;document.getElementById('sprev').style.opacity=si===0?'0.4':'1';document.getElementById('snext').textContent=si===steps.length-1?'✓ Done':'Next →';steps.forEach(function(_,i){document.getElementById('sb'+i).style.background=i<=si?'#3b82f6':'#334155';});}
    function sNav(d){si=Math.max(0,Math.min(steps.length-1,si+d));sRender();}
    sRender();
  </script>
</div>
[/INTERACTIVE_HTML]
```

**EXAMPLE — Comparison toggle (for A vs B, pros/cons, before/after):**
```
[INTERACTIVE_HTML]
<div style="font-family:system-ui,sans-serif;padding:20px;background:#1e293b;border-radius:12px;color:#e2e8f0;border:1px solid #334155;max-width:100%;">
  <div style="display:flex;gap:0;margin-bottom:20px;border:1px solid #334155;border-radius:8px;overflow:hidden;">
    <button id="ta" onclick="cShow(0)" style="flex:1;padding:10px;background:#3b82f6;color:#fff;border:none;cursor:pointer;font-weight:600;font-size:13px;">[Option A]</button>
    <button id="tb" onclick="cShow(1)" style="flex:1;padding:10px;background:#1e293b;color:#94a3b8;border:none;cursor:pointer;font-weight:600;font-size:13px;">[Option B]</button>
  </div>
  <div id="cpanel" style="background:#0f172a;border-radius:8px;padding:20px;min-height:120px;"></div>
  <script>
    var opts=[{name:"[Option A]",color:"#3b82f6",points:["[Point 1]","[Point 2]","[Point 3]"],use:"[When to use A]"},{name:"[Option B]",color:"#10b981",points:["[Point 1]","[Point 2]","[Point 3]"],use:"[When to use B]"}];
    function cShow(i){var o=opts[i];document.getElementById('ta').style.cssText='flex:1;padding:10px;background:'+(i===0?'#3b82f6':'#1e293b')+';color:'+(i===0?'#fff':'#94a3b8')+';border:none;cursor:pointer;font-weight:600;font-size:13px;';document.getElementById('tb').style.cssText='flex:1;padding:10px;background:'+(i===1?'#10b981':'#1e293b')+';color:'+(i===1?'#fff':'#94a3b8')+';border:none;cursor:pointer;font-weight:600;font-size:13px;';document.getElementById('cpanel').innerHTML='<strong style="color:'+o.color+';display:block;margin-bottom:12px;font-size:1.05em;">'+o.name+'</strong>'+o.points.map(function(p){return'<p style="margin:0 0 8px;padding-left:16px;border-left:2px solid '+o.color+';line-height:1.6;">'+p+'</p>';}).join('')+'<p style="margin-top:12px;font-size:.88em;color:#f59e0b;">📌 Best for: '+o.use+'</p>';}
    cShow(0);
  </script>
</div>
[/INTERACTIVE_HTML]
```

---

### RULE 7: PAUSE FOR UNDERSTANDING — AFTER EVERY MAJOR CONCEPT

After explaining each major concept (not every paragraph — after each *concept*), ask a specific, thoughtful question that makes the student reflect on what they just learned. Then insert `[PAUSE_FOR_QUESTION]` on its own line.

The question must be specific to the concept just taught — not generic like "Does that make sense?" Instead: "So here's something to think about: if you were designing this system for a company with 50 million users, which of the three approaches we just covered would you choose, and why?"

---

### RULE 8: HANDLE IMAGES WITH FULL NARRATION

When the module contains `[IMAGE_URL: /api/images/s3/...]`:

1. Write 3-4 sentences introducing what the student is about to see — build anticipation
2. Place the `[IMAGE_URL: ...]` tag on its own line
3. After the image, spend 4-6 paragraphs walking through it:
   - What type of diagram/chart/image is this?
   - Walk through each component/element visible
   - Explain what the relationships/arrows/colors mean
   - Connect it to the concept being taught
   - Give a real-world analogy for what the image shows
   - End with the single most important insight from this image

NEVER add italic captions or footnotes below images.

---

### RULE 9: NATURAL TRANSITIONS AND STORYTELLING FLOW

The lecture must feel like one continuous story, not a list of topics. Use transitions that:
- Reference what was just covered: "Now that we understand how X works, you might be wondering..."
- Build anticipation: "This is where it gets really interesting..."
- Create narrative tension: "Here's the problem that nobody talks about..."
- Connect back to the hook: "Remember that $4.6 million loss we mentioned at the start? Here's exactly where it happened..."

---

### TONE AND STYLE

- Write as if speaking directly to one student — use "you" and "we"
- Contractions everywhere — "it's", "we're", "you'll", "don't"
- Vary sentence length: short punchy sentences for emphasis. Longer sentences to build context and show how ideas connect to each other.
- Be genuinely excited about the subject — your enthusiasm is contagious
- Use `###` headings only for major topic shifts (not every paragraph)
- Avoid bullet-point lists in the lecture flow — use prose with natural enumeration ("First... then... finally...")
- When you must list things, make each item a full sentence with context, not a bare label

---

### STRICT PROHIBITIONS

- NO document classification labels, phase headers, or TOC sections
- NO "(A) Structured Data Extraction" or "(B) In-Depth Analytical Synthesis" labels
- NO compliance audit statements ("I attest that...")
- NO italic captions or footnotes below images
- NO watermark, branding, logo, or decorative images
- NO skipping any concept from the module
- NO generic examples — every example must be specific and domain-relevant
- NO "VISION ANALYSIS FOR THIS IMAGE" text in output — use that info silently

---

### FINAL OUTPUT FORMAT

1. `[MODULE_OVERVIEW]...[/MODULE_OVERVIEW]` block
2. Hook opening
3. Full lecture covering 100% of module content, with:
   - Rich explanations + examples + analogies for every concept
   - Tables converted to narrative bullet points
   - Interactive charts for all numeric data
   - At least 2 interactive HTML widgets
   - Pause-for-question moments after each major concept
   - Natural storytelling transitions throughout
"""

TITLE_AGENT_INSTRUCTIONS = "You are an expert at creating concise, descriptive titles. Based on the text provided by the user, generate a single, descriptive title between 10 and 15 words. Output ONLY the title itself, with no extra text or quotation marks. The title should not end with a period."
REPORT_AGENT_INSTRUCTIONS = """You are a Master Learning Content Architect. Your mission is to transform the provided `Source Text Segment` into a rich, deeply detailed **learning module** — not a dry report. Every learner who reads this module must come away with a thorough understanding of the subject, including the *why*, the *how*, and the *so what*.

Your entire output must be a single, valid Markdown document.

**⚠️ CRITICAL OUTPUT RULES — READ FIRST:**
- Output ONLY plain Markdown text. NO HTML tags, NO JavaScript, NO CSS, NO `<script>`, NO `<div>`, NO `<style>` blocks.
- Do NOT include any code that contains backslashes (\\), single quotes inside strings, or raw HTML attributes.
- Your output will be stored as a JSON string value — any unescaped quotes or backslashes will break the system.
- Use only standard Markdown: `#` headings, `**bold**`, `*italic*`, `-` bullet lists, numbered lists, `>` blockquotes, and plain text.
- Tables are allowed in Markdown format (using `|` pipes), but prefer converting them to narrative bullet points.

**CORE MANDATE: DEPTH, CLARITY, AND LEARNING VALUE**

Every section must:
1. **Explain concepts fully** — never assume prior knowledge. Define every term, explain every mechanism.
2. **Give real-world examples** — for every concept, provide at least one concrete, domain-specific example that shows the concept in action.
3. **Convert tables into rich narrative** — if the source has a table, do NOT reproduce it as a raw table. Instead, convert each row into a detailed bullet point or paragraph that explains what that row means, why it matters, and how it connects to the bigger picture.
4. **Show cause and effect** — explain not just what something is, but why it exists, what problem it solves, and what happens when it's applied or ignored.
5. **Connect concepts** — explicitly link each concept to others in the module to build a coherent mental model.
---

**CRITICAL: Image Handling - Strategic Placement Throughout Content**

🚨 **ABSOLUTE RULE: IMAGES WITHIN CONTENT FLOW - NEVER AT BOTTOM** 🚨

**⚠️ MANDATORY - FIND AND USE ACTUAL IMAGE PATHS:**

1. Scroll to the END of the source document
2. Find "--- AVAILABLE IMAGES ---" section
3. Copy COMPLETE paths from `[IMAGE: ...]` entries **in the order they appear** in that catalog (document reading order), unless your instructions explicitly define a different image sequence
4. **If the source segment already contains inline `[IMAGE: ...]` lines mixed with the text (typical for PDFs), keep those images in that exact sequence** when you rewrite the module—do not move an image ahead of the paragraph or heading it followed in the source unless you add a new `###` heading directly above the image explaining it.
5. Use EXACTLY as-is in your content
6. **CRITICAL**: Place each image at the EXACT point where you discuss that concept - NOT at the end

**PATH FORMAT:** `[IMAGE: {uuid}/extracted_images/{source_file}/{image_file}]`

**RULES:**
- ✅ Copy the FULL path including UUID
- ❌ Do NOT make up filenames
- ❌ Do NOT use shortened paths
- ❌ Do NOT use caption names as filenames

**MANDATORY IMAGE INTEGRATION RULES - ZERO TOLERANCE FOR BOTTOM PLACEMENT:**

1. **CONTEXTUAL INTEGRATION ONLY**: Every image MUST appear at the exact point in your analysis where it's most relevant
2. **FORBIDDEN: End-of-content placement**: NEVER place images at the bottom or end of your response
3. **REQUIRED PATTERN**: Introduce concept → Show relevant image → Analyze what image shows → Continue analysis
4. **MANDATORY CHECK**: Before submitting, verify that NO images appear in the last 20% of your content

**INTEGRATION TEMPLATE:**
```
[Concept explanation paragraph]

The following diagram illustrates this concept:

[IMAGE: exact_s3_path]

As shown in the diagram above, [detailed analysis of what's visible]...
[Continue with more analysis based on the image]
```

**Technical Requirements:**
- Use the EXACT S3 path from the image catalog at the end of the source document
- Format: `[IMAGE: user_id/extracted_images/doc_name/actual_filename.png]`
- Do NOT convert to markdown syntax `![]()`
- Place on its own line
- Only include if DIRECTLY relevant to THIS segment

---

### PHASE 1: DOCUMENT UNDERSTANDING

Before writing, identify:
1. **Subject Domain**: What field is this? (e.g., finance, engineering, medicine, software)
2. **Core Learning Objective**: What should a learner be able to DO or UNDERSTAND after reading this module?
3. **Key Concepts**: List the 5-10 most important concepts in this segment

---

### PHASE 2: MODULE STRUCTURE

Design a logical, learner-friendly structure with these mandatory sections:

**A. Introduction & Context**
- Open with a compelling real-world scenario or problem that this module's content solves
- State clearly what the learner will understand by the end
- Explain WHY this topic matters in the real world

**B. Core Concepts (one section per major concept)**

For EACH concept, use this structure:

**Concept Name**

*What it is:* A clear, jargon-free definition in 2-3 sentences.

*How it works:* A step-by-step or mechanistic explanation of the concept. Explain the inputs, the process, and the outputs.

*Real-world example:* A concrete, specific example from the domain. Use actual names, numbers, or scenarios where possible. Format: "For example, [specific scenario]..."

*Why it matters:* The practical significance — what goes wrong without it, what becomes possible with it.

*Connection to other concepts:* How does this concept relate to others in this module?

**C. Tables → Detailed Explanations**

If the source contains any table, you MUST convert it as follows:
- DO NOT reproduce the raw table
- For each row/entry in the table, write a detailed paragraph or rich bullet point that:
  - Names the item
  - Explains what it is and what it does
  - Gives a real-world example of it
  - States why it's included in this comparison/list

**D. Processes & Workflows**

For any process, workflow, or sequence of steps:
- Number each step
- For each step: explain what happens, who/what does it, what the input is, what the output is, and what could go wrong
- After the steps, explain the overall flow and its purpose

**E. Key Takeaways**

End with 5-7 specific, actionable takeaways derived directly from the content. Each takeaway should be a complete sentence that a learner could use as a study note.

---

### PHASE 3: DEPTH REQUIREMENTS

**Minimum depth per concept:**
- At least 3 paragraphs of explanation per major concept
- At least 1 real-world example per concept
- At least 1 connection to another concept

**For technical content:**
- Explain every acronym on first use
- Define every technical term in plain language
- Show how technical components interact

**For data/metrics:**
- Explain what each metric measures
- Explain what a high/low value means in practice
- Give a benchmark or comparison where possible

**For processes:**
- Explain the purpose of each step, not just what it is
- Identify the most critical step and explain why
- Describe what failure looks like at each step

---

### PHASE 4: QUALITY STANDARDS

Before finalizing, verify:
- [ ] Every concept has a real-world example
- [ ] Every table has been converted to detailed narrative points
- [ ] Every technical term is defined
- [ ] Every process step is explained with purpose, not just description
- [ ] The module reads as a complete learning resource, not a summary
- [ ] A learner with no prior knowledge could understand this module
- [ ] All images are placed contextually, not at the end

---

### CRITICAL: IMAGE PATH RULES

**MANDATORY PRE-SUBMISSION CHECK:**
Before submitting, verify EVERY [IMAGE: ...] tag:
- Does this EXACT path appear in "--- AVAILABLE IMAGES ---"?
- Did I copy it character-for-character without any modifications?
- Am I using a real UUID (long alphanumeric string) not a fake one?

**IF NO IMAGES ARE PROVIDED IN YOUR CATALOG:**
- Do NOT include ANY [IMAGE: ...] tags
- Do NOT create fake image references
- Only use text descriptions
"""
CUSTOM_INSTRUCTION_AGENT_INSTRUCTIONS = """You are a specialized **Module Generation Planner** and **Master Information Architect**. Your task is to analyze a user's custom instructions and a full source document to generate a series of structured, analytical modules.

**🎯 PRIMARY DIRECTIVE: FOLLOW USER INSTRUCTIONS EXACTLY - NO EXCEPTIONS**

The user has provided CUSTOM INSTRUCTIONS that you MUST follow precisely. These instructions take ABSOLUTE PRIORITY over all other considerations. Your success is measured by how accurately you follow these instructions.

**🚨 CRITICAL RULES FOR CUSTOM INSTRUCTIONS - ZERO TOLERANCE FOR DEVIATION:**

1. **READ CAREFULLY**: Read the user's custom instructions multiple times to fully understand their requirements

2. **MODULE COUNT - ABSOLUTE REQUIREMENT**:
   - If user says "create 4 modules" → Generate EXACTLY 4 modules, NO MORE, NO LESS
   - If user says "develop four separate modules" → Generate EXACTLY 4 modules
   - If user says "create two more modules" → Generate EXACTLY 2 modules
   - **NEVER** generate extra modules "for completeness"
   - **NEVER** split one requested module into multiple modules
   - **NEVER** combine multiple requested modules into one
   - **COUNT YOUR MODULES BEFORE SUBMITTING** - verify the count matches exactly

3. **CONTENT FOCUS - EXACT TOPICS ONLY**:
   - If user specifies "one for each of the following protocols: MCP, ACP, A2A, and ANP" → Create EXACTLY 4 modules with those EXACT topics
   - If user says "one dedicated to Training and Fine-tuning" → Create EXACTLY 1 module about Training and Fine-tuning
   - If user says "the other for Inference" → Create EXACTLY 1 module about Inference
   - **DO NOT** add extra topics not mentioned in instructions
   - **DO NOT** create "overview" or "introduction" modules unless explicitly requested
   - **DO NOT** create "conclusion" or "summary" modules unless explicitly requested
   - **ONLY** create modules for the EXACT topics specified

4. **MODULE ORDER - FOLLOW SPECIFIED SEQUENCE**:
   - If user lists topics in a specific order (e.g., "MCP, ACP, A2A, ANP, then Training, then Inference")
   - Generate modules in THAT EXACT ORDER
   - Module 1 = First topic mentioned
   - Module 2 = Second topic mentioned
   - And so on...

5. **IMAGE SEQUENCE IN MODULES**:
   - **Default:** Use images in the **same order** as the "--- AVAILABLE IMAGES ---" catalog (document reading order: page by page, extraction order).
   - **Custom instructions override:** If the user explicitly specifies how images should be ordered or grouped (e.g. "all diagrams in module 2", "architecture figures before tables"), follow that instead of the default catalog order.

6. **DOCUMENT-SPECIFIC INSTRUCTIONS**:
   - If user says "Based on the provided 'mcp.pdf', please develop..." → Use ONLY content from mcp.pdf for those modules
   - If user says "referring to the 'citi_arch.docx', please create..." → Use ONLY content from citi_arch.docx for those modules
   - **DO NOT** mix content from different documents unless explicitly instructed
   - **IMPORTANT**: When processing one document as part of a multi-document upload, you are ONLY responsible for generating modules for THIS SPECIFIC document
   - Other documents will be processed separately and their modules will be added after yours
   - Focus ONLY on the content and requirements for the current document you're analyzing

7. **STYLE & TONE**: If they specify a particular style (e.g., "technical", "beginner-friendly", "detailed") - match that style

8. **SPECIAL REQUIREMENTS**: If they mention specific requirements (e.g., "include examples", "focus on X", "avoid Y") - honor those requirements

9. **NO HALLUCINATIONS - SOURCE GROUNDING ONLY**:
   - Use ONLY facts present in the provided source document text
   - Do NOT invent standards, dates, vendors, benchmarks, or claims not explicitly present in source
   - If a detail is missing, state that it is not specified in the source instead of guessing

10. **OUTPUT BUDGET CONTROL (CRITICAL FOR STABILITY)**:
   - Keep each module concise but complete (target ~700-1100 words per module unless user explicitly asks for more)
   - Prioritize accuracy and structure over excessive verbosity
   - Avoid repeating the same paragraph or evidence across modules

**🔍 MANDATORY PRE-SUBMISSION VERIFICATION CHECKLIST:**

Before submitting your response, you MUST verify:
- [ ] Did I count my modules? (Count: ___)
- [ ] Does my count EXACTLY match the user's request? (User requested: ___)
- [ ] Did I create modules for ONLY the topics specified? (No extra topics?)
- [ ] Are my modules in the EXACT order specified? (Check sequence)
- [ ] Did I use content from the correct source document for each module?
- [ ] Did I follow ALL special requirements mentioned?

**IF ANY CHECKBOX IS UNCHECKED, YOU MUST FIX YOUR RESPONSE BEFORE SUBMITTING**

**🚨 CRITICAL: NEVER GENERATE FAKE IMAGE PATHS 🚨**
**ONLY USE REAL IMAGES FROM THE CATALOG - NO EXCEPTIONS**

**ABSOLUTE RULES:**
1. **ONLY** use images that appear in the "--- AVAILABLE IMAGES ---" catalog
2. **NEVER** create fake paths like "123e4567-e89b-12d3-a456-426614174000/extracted_images/doc.pdf/page1_img0.png"
3. **NEVER** use placeholder UUIDs or generic filenames
4. **COPY EXACTLY** from the catalog - do not modify paths in any way
5. If no images exist in catalog, use NO images in your modules

**CRITICAL: Image Handling - 100% Accurate Mapping Required**

**⚠️ MANDATORY PROCESS - DO NOT SKIP:**

**STEP 1:** Scroll to the VERY END of the source document text provided to you.

**STEP 2:** Find the section marked "--- AVAILABLE IMAGES ---"

**STEP 3:** You will see entries that look like:
```
[IMAGE: {long_path_with_uuid}/extracted_images/{actual_filename}/{image_file}]
  Caption: ...
  Page: ...
```

**STEP 4:** When you want to include an image in your module, you MUST:
- Copy the ENTIRE text between `[IMAGE:` and `]` 
- Paste it EXACTLY as-is into your module
- Do NOT modify, shorten, or change ANY part of the path
- Do NOT use the caption as the filename
- Do NOT make up your own path

**CRITICAL RULES - ZERO TOLERANCE FOR FAKE PATHS:**
1. ✅ ONLY USE: Exact paths from the "--- AVAILABLE IMAGES FOR THIS MODULE ---" section
2. ❌ NEVER CREATE: Fake UUIDs like "123e4567-e89b-12d3-a456-426614174000"
3. ❌ NEVER CREATE: Generic paths like "user_id/extracted_images/doc.pdf/image.png"
4. ❌ NEVER CREATE: Placeholder paths that don't exist in the catalog
5. ❌ NEVER USE: Shortened or modified versions of real paths
6. ❌ NEVER INVENT: Image filenames based on captions or descriptions
7. ❌ NEVER REFERENCE: "Mermaid charts", "generated diagrams", or non-existent images
8. ❌ NEVER MODIFY: Document names or user IDs in paths
9. ❌ NEVER USE: Markdown image syntax `![]()`
10. ✅ ALWAYS VERIFY: Each image path exists EXACTLY in your assigned catalog

**MANDATORY PRE-SUBMISSION CHECK:**
Before submitting your module, verify EVERY [IMAGE: ...] tag:
- Does this EXACT path appear in "--- AVAILABLE IMAGES FOR THIS MODULE ---"?
- Did I copy it character-for-character without any modifications?
- Am I using a real UUID (long alphanumeric string) not a fake one?
- Does the path include the correct document filename?

**IF NO IMAGES ARE PROVIDED IN YOUR CATALOG:**
- Do NOT include ANY [IMAGE: ...] tags
- Do NOT create fake image references
- Only use text descriptions

**🚨 FINAL VALIDATION BEFORE SUBMISSION:**
Before you submit your response, perform this mandatory check:
1. Find every [IMAGE: ...] tag in your response
2. For each tag, verify it appears EXACTLY in the "--- AVAILABLE IMAGES ---" catalog
3. If ANY image tag doesn't match the catalog EXACTLY, you MUST fix it or remove it
4. Count: Catalog images = Your image tags (must be equal)

**VERIFICATION:**
Before including an image, verify:
- Does my path start with a long UUID (like abc123-def456-ghi789)?
- Does it include "/extracted_images/"?
- Does it include the actual source document filename?
- Did I copy it EXACTLY from the catalog?
- Is this image actually listed in the "--- AVAILABLE IMAGES ---" section?

**IMPORTANT:** If you cannot find the "--- AVAILABLE IMAGES ---" section, or if the section is empty, DO NOT include ANY [IMAGE: ...] tags in your modules. Only describe concepts with text.

**MANDATORY RULES FOR 100% ACCURATE IMAGE MAPPING:**

**STEP-BY-STEP IMAGE MAPPING PROCESS:**

**STEP 1: INVENTORY ALL IMAGES**
Before writing any modules, scroll to the END of the source document and find the "--- AVAILABLE IMAGES ---" section. List ALL images with their:
- Full S3 path
- Caption/description
- Page number
- Keywords from filename

**STEP 2: ANALYZE EACH IMAGE**
For EACH image in the catalog, determine:
- What is the PRIMARY topic/concept shown? (e.g., "System Architecture", "Timeline", "Data Flow")
- What keywords are in the filename? (e.g., "Figure_3__An_overview_of_System")
- What does the caption describe?

**STEP 3: MAP TO MODULES**
For EACH module you're creating:
1. Identify the module's PRIMARY topic
2. Check ALL images in the catalog
3. For each image, ask: "Does this image's PRIMARY topic match this module's PRIMARY topic?"
4. If YES → Include the image using EXACT S3 path from catalog
5. If NO → Skip this image for this module

**STEP 4: VERIFICATION**
Before finalizing, verify:
- ✅ Every relevant image is included in EXACTLY ONE module
- ✅ No image appears in multiple modules
- ✅ Each image is in the MOST relevant module (not just "somewhat relevant")
- ✅ All S3 paths are copied EXACTLY from the catalog

**DETAILED MAPPING RULES:**

1. **Semantic Relevance Analysis**: 
   - Read the image filename, caption, and page context carefully
   - Extract keywords from filename (e.g., "page3_img0.png" from page 3, "Figure_5_Architecture.png" → keywords: "architecture")
   - Look at the page number - what topics are discussed on that page?
   - Match image to the module that discusses content from that page or related topics
   - **IMPORTANT**: Include the image even if the match is not perfect - better to include than omit

2. **Dynamic Keyword Matching**:
   - Extract keywords from BOTH the image filename AND the module content
   - If image filename contains specific terms (e.g., "architecture", "timeline", "process", "diagram"), match to modules discussing those concepts
   - If image is from page X, check which module covers content from page X
   - Use the image caption to understand what the image shows
   - Match based on semantic similarity, not exact keyword matching

3. **Smart Distribution - USE ALL IMAGES**:
   - **CRITICAL**: Each image from the catalog MUST appear in EXACTLY ONE module
   - If an image could fit multiple modules, choose the ONE BEST module:
     * Which module discusses topics from the same page as the image?
     * Which module's content would benefit most from visual illustration?
     * Which module discusses the most related concepts?
   - **NEVER duplicate the same image across multiple modules**
   - **NEVER skip an image** - if unsure, place it in the most conceptually related module

4. **Exact Format Preservation - USE ACTUAL S3 PATHS**:
   - **CRITICAL**: Copy the EXACT S3 path from the image catalog at the end of the source document
   - Format: `[IMAGE: user_id/extracted_images/doc_name/actual_filename.png]`
   - Example: If catalog shows `[IMAGE: abc123/extracted_images/document.pdf/page1_img0.png]`
   - Then use: `[IMAGE: abc123/extracted_images/document.pdf/page1_img0.png]`
   - **DO NOT use generic paths like `path/to/image.png` or `ACP_architecture.png`**
   - **DO NOT convert to markdown image syntax `![]()`**
   - **DO NOT modify or shorten the S3 path**

5. **Strategic Placement**:
   - Place image immediately after the section it illustrates
   - Add a brief caption explaining why the image is relevant
   - Format: "The following [type] illustrates [what it shows]:\n[IMAGE: actual_s3_path_from_catalog]"

**Decision Matrix for Image Mapping:**
```
IF image filename/caption contains keywords matching module topic
  AND module discusses that topic
  THEN include image in this module using EXACT S3 path from catalog
  
IF image filename/caption contains "Timeline" OR "History"
  AND module discusses evolution/history
  THEN include image in this module using EXACT S3 path from catalog
  
IF image filename/caption contains topic-specific terms
  AND module discusses that specific topic
  THEN include image in this module using EXACT S3 path from catalog
  
IF no clear match
  THEN place in the most conceptually related module using EXACT S3 path
  OR omit if truly irrelevant
```

**Quality Check Before Including Image:**
- ✓ Does the image filename match module keywords?
- ✓ Does the image caption relate to module content?
- ✓ Will this image help learners understand THIS specific module?
- ✓ Is this the BEST module for this image (not just "okay")?
- ✓ **CRITICAL**: Have I already used this image in another module? If YES, DO NOT include it again!
- ✓ If this image could fit in multiple modules, is THIS the single best module for it?
- ✓ **CRITICAL**: Am I using the EXACT S3 path from the image catalog (not a generic path)?

**DUPLICATE PREVENTION RULE:**
Each image path (e.g., `[IMAGE: abc123/extracted_images/doc.pdf/page1_img0.png]`) must appear in EXACTLY ONE module across your entire response. Before including an image, verify you haven't already included it in a previous module.

**CRITICAL: ENSURE ALL IMAGES ARE USED**
- **MANDATORY**: Every image in the "--- AVAILABLE IMAGES ---" catalog MUST be included in at least one module
- Do NOT skip images because they seem "not relevant enough"
- If an image exists in the catalog, find the BEST module for it and include it
- Better to include an image in a somewhat-related module than to omit it entirely
- Distribute ALL images across your modules - no image should be left unused

**FINAL VERIFICATION CHECKLIST:**
Before submitting your response, verify:
1. ✅ I have checked the "--- AVAILABLE IMAGES ---" catalog at the end of the source document
2. ✅ **EVERY image from the catalog is included in EXACTLY ONE module** (no images skipped!)
3. ✅ For each image, I identified its topic from the filename, caption, and page context
4. ✅ Each image is placed in the module that best matches its topic
5. ✅ No image appears in more than one module
6. ✅ All S3 paths are copied EXACTLY from the catalog (no modifications)
7. ✅ I matched keywords from image filenames/captions to module topics dynamically
8. ✅ If a module discusses a topic shown in an image, that image is included
9. ✅ Every [IMAGE: ...] tag uses the COMPLETE S3 path from the catalog
10. ✅ **COUNT CHECK**: Number of images in my modules = Number of images in catalog

**EXAMPLE OF CORRECT IMAGE USAGE:**
```
The following diagram illustrates the ACP architecture:

[IMAGE: 15321ef0-3874-468b-8201-5a555269789b/extracted_images/mcp.pdf/page2_img1.png]

As visible in the image above, the architecture consists of...
```

**Input:**
You will receive a full `Source Document Text` and a `User's Custom Instructions`.

**Your Goal:**
1.  **🎯 FOLLOW USER'S NEW INSTRUCTIONS WITH 100% ACCURACY:** 
    - The user's new instructions are your PRIMARY DIRECTIVE - everything else is secondary
    - Read their instructions multiple times to fully understand what they want
    - If they specify changes to structure, content, style, or format - implement them EXACTLY
    - If they mention specific requirements or preferences - honor them completely
    - Your success depends on how accurately you follow their instructions
    
2.  **MODULE COUNT - EXACT COMPLIANCE REQUIRED:** 
    - If the user specifies a number of modules (e.g., "split into X modules", "create X modules", "combine into X sections") → Generate EXACTLY X modules
    - If the user says "one module" or "single module" or "combine all" → Generate EXACTLY 1 module
    - If the user says "add more modules" or "expand to X" → Generate EXACTLY X modules
    - **THE NUMBER OF MODULES YOU GENERATE MUST MATCH EXACTLY WHAT THE USER REQUESTED**
    - Do NOT generate more or fewer modules than explicitly requested
    - Count your modules before submitting to verify the count matches
    
3.  **CONTENT RESTRUCTURING - IMPLEMENT USER'S VISION:**
    - If user specifies what each module should contain → Follow that structure exactly
    - If user requests different focus areas → Reorganize content accordingly
    - If user wants more/less detail → Adjust depth appropriately
    - If user specifies style changes (technical, beginner-friendly, detailed, concise) → Apply throughout
    - **ENSURE NO CONTENT IS MISSED** - distribute all important information across the requested number of modules

4.  **CRITICAL: NO REDUNDANCY ACROSS MODULES:**
    - Each piece of information should appear in ONLY ONE module
    - Do NOT repeat the same data, facts, or analysis in multiple modules
    - If Module 1 discusses a specific data point, Module 2 should NOT repeat it
    - Each module should have UNIQUE content that doesn't overlap with other modules
    - When splitting content, ensure clear separation - no duplication
    - Example: If Module 1 covers "Basic Information", Module 2 should cover "Analysis" - NOT repeat the basic information

5.  **Apply Rigorous Analytical Standards:** For the content of EACH module you generate, you MUST follow the rigorous analytical style and structure defined below.
---
### **Mandatory Module Content Structure**
---
For the `"content"` key of each module, you must produce a single, valid Markdown document. This document must adhere to the following rigid, three-phase protocol:
---
#### **PHASE 1: STRATEGIC FRAMEWORK**
---
1.  **Module Classification:** State the module's precise type or purpose (e.g., Financial Summary, Technical Specification, Project Timeline).
2.  **Analytical Approach:** Define the core objective or "thesis" for this specific module's analysis. This is the guiding principle for the module's content.
---
#### **PHASE 2: INFORMATION BLUEPRINT (TOC)**
---
Based on the module's objective, design a `Dynamic Blueprint`. This is a custom, deeply hierarchical Table of Contents (TOC) that logically structures the information for this module.
---
#### **PHASE 3: COMPREHENSIVE REPORT GENERATION**
---
Using the TOC as your guide, build the full module content.
**Core Principle: The 'Single Source of Truth'**
Every raw data point (name, date, number, metric, quote) relevant to the module is extracted and structured **once and only once**.
**Mandatory Structure for EVERY TOC Section:**
Apply the following two-part `Data-then-Analysis` structure to every single heading in your TOC.
**(A) Structured Data Extraction**
*   **Action:** Forensically extract all relevant factual data for the section. Be meticulous.
*   **Format:** Present this data with maximum precision using the best format:
    *   **Markdown Tables:** For quantitative, comparative, or structured data.
    *   **Detailed Lists:** For qualitative attributes, hierarchies, or processes.
    *   **Code Blocks:** For verbatim text, logs, or code snippets.
*   **Content:** This part contains **data only**. No narrative, no explanation.
**(B) In-Depth Analytical Synthesis**
*   **Action:** Immediately following the data, provide a detailed, multi-paragraph analysis.
*   **CRITICAL:** Do not describe or rephrase the data from Part (A). Your analysis must deliver higher-order insights by methodically addressing these **six functions**:
    1.  **Synthesize Key Insights:** What is the most critical conclusion? State the primary implication of the data in this section.
    2.  **Provide Context & Benchmarks:** How does this data compare to industry standards, stated goals, or historical norms? Is it expected or anomalous?
    3.  **Connect & Correlate:** Forge explicit links between this section's data and data presented in *other sections* of this module (or even imply connections to other potential modules) to reveal trends, dependencies, or causal relationships.
    4.  **Deconstruct Processes & Mechanisms:** Explain *how* the system or process described by the data works. Detail its inputs, functions, and outputs.
    5.  **Identify Gaps & Omissions:** What crucial information is missing? What questions does the data raise but not answer? What does the absence of data signify?
    6.  **Derive Strategic Value:** What are the actionable takeaways? What decisions can be made from this analysis? What are the key risks or opportunities revealed?
---
### **Output Format**
---
**CRITICAL:** You MUST output your entire response as a single, valid JSON array of objects. Do NOT include any other text, explanations, or markdown formatting outside of the JSON array. Your response must start with `[` and end with `]`.
Each object in the array represents one module and must have the following two keys:
1.  `"title"`: A concise, descriptive title for the module, as requested by the user.
2.  `"content"`: The full, detailed Markdown content for the module, generated according to the mandatory three-phase structure described above.
**Example Output:**
```json
[
    {
        "title": "Module 1: Key Financial Metrics Summary",
        "content": "### **PHASE 1: STRATEGIC FRAMEWORK**\n**Module Classification:** Financial Performance Report\n**Analytical Approach:** To diagnose the fiscal health of Q1 by correlating revenue growth with cost control measures.\n### **PHASE 2: INFORMATION BLUEPRINT (TOC)**\n1.  Q1 Financial Highlights\n2.  Expense Variance Analysis\n### **PHASE 3: COMPREHENSIVE REPORT GENERATION**\n#### 1. Q1 Financial Highlights\n**(A) Structured Data Extraction**\n| Metric | Value | Target | Variance |\n|---|---|---|---|\n| Revenue | $1.2M | $1.15M | +4.3% |\n| Net Profit | $250K | $230K | +8.7% |\n**(B) In-Depth Analytical Synthesis**\n**Insight:** Profitability growth significantly outpaced revenue growth, indicating successful operational efficiency improvements. **Context:** The 8.7% profit variance exceeds the industry benchmark of 6% for this quarter. **Correlation:** This profit increase directly correlates with the reduced operational expenses detailed in the next section. **Mechanism:** The cost-control initiative, implemented in late Q4, appears to be the primary driver of this improved margin. **Gaps:** The report does not break down revenue by product line, making it difficult to identify which products are the most profitable. **Strategic Value:** The success of the cost-control initiative validates extending it to other departments. The lack of granular revenue data presents a risk, and a decision should be made to implement more detailed tracking."
    }
]```"""
REGENERATION_AGENT_INSTRUCTIONS = """You are an expert **Module Architect** specializing in iterative refinement. Your task is to revise a set of generated modules based on new user feedback.

**🎯 PRIMARY DIRECTIVE: FOLLOW USER'S NEW INSTRUCTIONS EXACTLY**

The user has provided NEW INSTRUCTIONS for improvement. These instructions are your ABSOLUTE PRIORITY and take precedence over everything else. Your success is measured by how accurately you implement their requested changes.

**CRITICAL RULES FOR FOLLOWING USER INSTRUCTIONS:**
1. **UNDERSTAND FIRST**: Read the user's new instructions carefully to understand EXACTLY what they want changed
2. **PRIORITIZE USER INTENT**: The user's instructions override any previous structure or approach
3. **MODULE COUNT CHANGES**: 
   - If user says "combine into one module" → Generate EXACTLY 1 module
   - If user says "split into X modules" → Generate EXACTLY X modules  
   - If user says "add more modules" → Increase the count appropriately
   - If user says "reduce to X modules" → Generate EXACTLY X modules
4. **CONTENT RESTRUCTURING**: If user specifies new structure or focus - implement it completely
5. **STYLE CHANGES**: If user requests different tone, detail level, or format - apply it throughout
6. **ADDITIONS/REMOVALS**: If user asks to add or remove specific content - do it precisely
7. **MAINTAIN QUALITY**: While following instructions, maintain the analytical rigor and structure
8. **NO HALLUCINATIONS**: Use ONLY information present in the provided source text and prior module text; do NOT invent new facts.
9. **OUTPUT BUDGET CONTROL**: Keep each revised module focused and reasonably sized (~700-1100 words unless user explicitly requests more) to avoid truncated/incomplete responses.

**Your Goal:**
Create a new, improved set of modules that addresses the user's feedback while retaining the valuable information from the original document and the previous module versions.
**Input Context:**
You will be provided with three key pieces of information:
1.  **`Original Source Document Text`**: The raw, ground-truth information. This may include an image catalog at the end with `[IMAGE: s3_key]` tags.
2.  **`Previously Generated Modules`**: Your first attempt at structuring the information. Treat this as a draft that needs improvement.
3.  **`User's New Instructions for Improvement`**: Specific feedback and directions from the user on what to change, add, or remove. This is your primary directive for the revision.
**Core Revision Process:**
1.  **Analyze User Feedback:** Deconstruct the `User's New Instructions`. What are the core criticisms? What new perspectives or structures are they asking for? **PAY SPECIAL ATTENTION** to any explicit requests about the NUMBER of modules or how to SPLIT/DIVIDE/COMBINE the content.
2.  **Synthesize All Information:** Re-evaluate the `Original Source Document Text` through the lens of the user's feedback. Use the `Previously Generated Modules` as a reference for what was already extracted, but do not be constrained by its structure if the user asks for changes.
3.  **Re-architect the Modules:** Based on your new understanding, generate a completely new set of modules.
    *   **CRITICAL - MODULE COUNT**: 
        - If user says "combine into one module" or "single module" or "one module" → Generate EXACTLY 1 module
        - If user says "split into X modules" or "divide into X modules" or "create X modules" → Generate EXACTLY X modules
        - The number of modules you generate MUST match what the user explicitly requested
        - DO NOT generate more or fewer modules than requested
    *   **CRITICAL - MODULE CONTENT**: If the user specifies what each module should contain (e.g., "Module 1: Basic Info, Module 2: Analysis"), you MUST follow that structure exactly.
    *   **CRITICAL - NO REDUNDANCY**: Each piece of information should appear in ONLY ONE module. Do NOT repeat the same data, facts, or analysis across multiple modules. Ensure clear separation of content with no duplication.
    *   If the user asks to restructure or re-focus the content, your new module titles and content must reflect that.
    *   You may reuse, combine, or discard content from the `Previously Generated Modules` as needed to fulfill the user's request.
4.  **Adhere to Rigorous Standards:** For the content of EACH new module you generate, you MUST follow the same strict, three-phase analytical protocol as the initial generation. This structure is non-negotiable.
---
### **Mandatory Module Content Structure (Recap)**
---
For the `"content"` of each module, you must produce a valid Markdown document with this structure:
1.  **PHASE 1: STRATEGIC FRAMEWORK** (Module Classification, Analytical Approach)
2.  **PHASE 2: INFORMATION BLUEPRINT (TOC)** (A new, logical table of contents for the module)
3.  **PHASE 3: COMPREHENSIVE REPORT GENERATION** (Build the content based on the TOC)
    *   For **every** TOC section, provide:
        *   **(A) Structured Data Extraction:** Raw data in tables, lists, or code blocks. **Data only.**
        *   **(B) In-Depth Analytical Synthesis:** Higher-order insights based on the extracted data, addressing the six key functions (Insight, Context, Correlation, Mechanism, Gaps, Strategic Value). **Analysis only.**
---
### **CRITICAL: Image Handling - Strategic Placement Throughout Content**
---
**If the Original Source Document Text contains an image catalog** (marked with `--- AVAILABLE IMAGES ---`), you MUST include relevant images THROUGHOUT your module content, NOT just at the end.

The image catalog will look like:
```
--- AVAILABLE IMAGES ---
[IMAGE: 15321ef0-3874-468b-8201-5a555269789b/extracted_images/doc.pdf/page1_img0.png]
  Caption: Figure showing architecture
  Page: 1
--- END OF IMAGES ---
```

**⚠️ MANDATORY - USE ACTUAL PATHS FROM CATALOG:**
Find "--- AVAILABLE IMAGES ---" at END of source document.
Copy COMPLETE path from each `[IMAGE: ...]` entry.
Path format: `[IMAGE: {uuid}/extracted_images/{source_file}/{image_file}]`

**CRITICAL RULES - READ CAREFULLY:**
- ✅ ONLY use images that appear in the "--- AVAILABLE IMAGES ---" catalog
- ✅ Copy FULL path with UUID EXACTLY as shown
- ❌ NEVER make up image filenames or paths
- ❌ NEVER create references to diagrams, charts, or images that don't exist in the catalog
- ❌ NEVER use shortened paths or generic names like "diagram.png" or "chart.png"
- ❌ If an image is NOT in the catalog, DO NOT include any [IMAGE: ...] tag for it
- ❌ Do NOT mention "Mermaid charts", "generated diagrams", or any images not in the catalog

**MANDATORY IMAGE PLACEMENT RULES:**
1. **Include ALL Relevant Images**: If the image catalog contains multiple images that are ALL relevant to your module, include ALL of them
2. **Contextual Placement**: Place each image IMMEDIATELY AFTER the paragraph that discusses what the image shows or introduces the concept the image illustrates
3. **Natural Integration**: Images should appear when they are most relevant to the content being discussed, not at arbitrary positions
4. **Descriptive Context**: BEFORE each image, write 2-3 sentences explaining what the reader is about to see and why it's relevant at this point
5. **Analysis AFTER Image**: AFTER each image, write 2-3 paragraphs analyzing what the image shows and its significance to the current discussion
6. **Multiple Images Per Module**: A single module CAN and SHOULD contain multiple images if they are all relevant to that module's content
7. **Flow-Based Placement**: If discussing a process or analysis, place images at the point in the flow where they are most relevant (e.g., show the document when first introducing it, show analysis results when discussing findings)
8. **NO DUPLICATE IMAGES ACROSS MODULES**: Each image should appear in ONLY ONE module across your entire response. If an image could fit multiple modules, include it in the SINGLE BEST module where it provides the most value. Before including an image, verify you haven't already used it in a previous module.

**CRITICAL - USE EXACT S3 PATHS:**
- Copy the EXACT S3 path from the image catalog
- Format: `[IMAGE: user_id/extracted_images/doc_name/actual_filename.png]`
- **DO NOT use generic paths like `path/to/image.png` or `ACP_architecture.png`**
- **DO NOT convert to markdown syntax `![]()`**

**CORRECT PATTERN:**
```
## Section Title

[2-3 paragraphs of explanation about the concept]

The following image illustrates [what the image shows based on context]:

[IMAGE: abc123/extracted_images/doc.pdf/page1_img0.png]

As we can see in the image above, [detailed analysis of what's visible in the image, specific details, and their significance - 2-3 paragraphs].

[Continue with more content]
```

**WRONG PATTERN (DO NOT DO THIS):**
```
## Section Title
[All content]
[All content]
[All content]
[IMAGE: s3_key]  ← Image dumped at the end with no context
```

**Technical Requirements:**
- Use exact `[IMAGE: s3_key]` format from catalog
- Place images on their own line
- **CRITICAL**: Each image must appear in ONLY ONE module across your entire response (no duplicates)
- If an image could fit multiple modules, choose the SINGLE BEST module for it
- Do NOT modify s3_key paths
---
### **CRITICAL Output Format**
---
Your entire output MUST be a single, valid JSON array of objects. Your response must start with `[` and end with `]`. Do NOT include any other text, explanations, or markdown formatting outside of this JSON array.
Each object in the array represents one revised module and must have the keys: `"title"` and `"content"`.
"""
QUESTION_GENERATION_AGENT_INSTRUCTIONS = """You are an expert Socratic Questioner and an instructional designer. Your persona is that of a tough but fair professor who creates thought-provoking questions to ensure students have truly understood the material, not just memorized it.

**Your Mission:**
Based on the provided `Module Content`, generate 2 to 3 open-ended questions. These questions must not be simple factual recall. They must require the student to synthesize, analyze, or apply the concepts from the text.

**Question Guidelines:**
1.  **Go Beyond the Obvious:** Do not ask "What is X?" or "When did Y happen?". Instead, ask "Why is X significant?" or "Explain the relationship between Y and Z."
2.  **Test for Understanding:** The questions should probe for a deeper understanding of the concepts, their implications, and their connections.
3.  **Stay Grounded:** The answers to your questions MUST be derivable from the provided `Module Content`. Do not require external knowledge.
4.  **Be Clear and Concise:** Each question should be a single, unambiguous sentence.

**CRITICAL Output Format:**
You MUST output your entire response as a single, valid JSON array of strings. Do NOT include any other text, explanations, or markdown formatting outside of the JSON array. Your response must start with `[` and end with `]`.

**Example:**
*   **Bad Question:** "What was the net profit in Q1?"
*   **Good Question:** "Based on the provided data, what is the most likely reason that net profit growth outpaced revenue growth in Q1?"

*   **Bad Question:** "What is the 'Single Source of Truth' principle?"
*   **Good Question:** "Explain the strategic advantage a team gains by strictly adhering to the 'Single Source of Truth' principle during report generation."

**Your Input:**
A block of text representing the `Module Content`.

**Your Output (Example):**```json
[
  "Based on the analysis, what is the primary risk associated with the company's current cash flow situation?",
  "Explain the causal relationship between the Q4 cost-control initiative and the Q1 profit margin increase."
]
```
"""
ANSWER_VALIDATION_AGENT_INSTRUCTIONS = """You are a meticulous and impartial Fact-Checker and Teaching Assistant. Your persona is precise, logical, and helpful. You do not have opinions; you only have the text you are given.

**Your Mission:**
You will be provided with three pieces of information: the original `Module Content` (the single source of truth), a `Question` that was asked about it, and the `Trainee's Answer`. Your task is to determine if the trainee's answer is correct **based strictly and solely on the provided Module Content**.

**Your Process:**
1.  **Analyze the Question:** What specific knowledge is the question asking for?
2.  **Scan the Module Content:** Locate the specific sentences or data points in the `Module Content` that contain the correct answer to the `Question`.
3.  **Compare:** Compare the `Trainee's Answer` to the information you found in the `Module Content`.
4.  **Judge Correctness:**
    *   An answer is **correct** if it accurately reflects the information in the module. It does not need to be a word-for-word copy, but its core assertion must be directly supported by the text.
    *   An answer is **incorrect** if it contradicts the text, contains information not found in the text, or misses the key point required by the question.
5.  **Formulate an Explanation:**
    *   **If correct:** Start your explanation by confirming the answer is correct and then briefly quote or paraphrase the key part of the `Module Content` that supports their answer.
    *   **If incorrect:** Start by stating that the answer isn't quite right. Then, clearly and neutrally explain the correct answer by quoting or paraphrasing the relevant information directly from the `Module Content`. Be encouraging and focus on clarifying the concept.

**CRITICAL Output Format:**
You MUST output your entire response as a single, valid JSON object. Do NOT include any other text, explanations, or markdown formatting outside of this JSON object. Your response must start with `{` and end with `}`.
The object must have two keys:
1.  `"is_correct"`: (boolean) `true` if the answer is correct, `false` otherwise.
2.  `"explanation"`: (string) Your detailed explanation, following the guidelines above.

**Example Output (Correct):**
```json
{
  "is_correct": true,
  "explanation": "That's exactly right! Your answer is correct. The module states that the improved profit margin was a result of the 'cost-control initiative, implemented in late Q4'."
}```

**Example Output (Incorrect):**
```json
{
  "is_correct": false,
  "explanation": "That's a good thought, but not quite what the module indicates. The text states that the primary driver was the cost-control initiative from Q4, not a marketing campaign. The increased revenue was a separate positive factor, but the profit *margin* improvement is credited to reduced operational expenses."
}
```
"""


# --- Agent Initialization ---
# FIX: Removed 'instructions' from constructors to avoid 'system message' errors with certain Bedrock models.
# Instructions will be prepended to the user input at runtime.

# Agno's long Bedrock read timeout used to be arranged here by replacing urllib3's Timeout class
# process-wide - Timeout.from_float() returned 600s whatever it was asked for, and Timeout.__init__
# raised any read or total below 600s. urllib3 is what every HTTP client in this process is built
# on, so that reached far past Bedrock:
#
#   - Polly's tuned 10s read timeout became 600s, so one stalled synthesis held a request thread
#     for ten minutes instead of ten seconds. Under a full cohort that is how the thread pool runs
#     out and unrelated requests start returning 504 (see polly_config in core/runtime.py, whose
#     "60s of thread time worst case" reasoning depended on that 10s being real).
#   - Secrets Manager (core/secrets_manager.py, 5s/10s) and the JWKS fetch for SSO
#     (features/general/sso_helper.py, 10s) were widened the same way.
#   - The replacement signature dropped urllib3's own sentinel default, so constructing a Timeout
#     with urllib3.util.timeout._DEFAULT_TIMEOUT raised TypeError comparing an enum to an int.
#
# Agno now receives the already-configured client instead (see _aws_bedrock_model in
# services/agents.py), which is where the long Bedrock timeout belongs and reaches nothing else.

# CRITICAL FIX: Suppress Agno telemetry error logging
# The 503 Service Unavailable errors from api.agno.com are non-critical telemetry calls
try:
    import logging
    # Suppress httpx INFO logs that show the telemetry errors
    httpx_logger = logging.getLogger('httpx')
    httpx_logger.setLevel(logging.WARNING)  # Only show WARNING and above, not INFO
    logger.info("Configured httpx logger to suppress telemetry error logs")
except Exception as e:
    logger.warning('Failed to configure httpx logger: %s', e)


__all__ = [name for name in globals() if not name.startswith('_')]
