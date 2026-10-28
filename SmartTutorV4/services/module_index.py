"""
Per-request index of the report metadata stored in S3.

Why this exists
---------------
A course's module count can only come from S3. The metadata key is
``{author_id}/generated_reports/{module_id}.json``, so the course a module belongs to is
known only from the object body, and there is no modules table to read instead. The same
prefix also holds video-analysis reports, so the number of objects grows with activity that
has nothing to do with courses.

What made that fatal was repetition, not the scan itself. Every caller listed the prefix and
issued a fresh ``get_object`` per object, once per course - and module counts are needed per
course on the trainee dashboard, the trainer dashboard and most super-admin tabs. A page
showing N courses therefore made N x M sequential S3 round-trips; token analytics, which
loops over every course of every trainer, made trainers x N x M. Past roughly a few hundred
round-trips that exceeds the 30s the browser waits before aborting the request
(``API_FETCH_TIMEOUT_MS`` in ``static/script.js``), which surfaced as the "Request timed out.
The server may be busy" toast for trainees, trainers and admins alike.

Two changes remove the cost without changing a single count:

* **Read each prefix at most once per request.** The parsed metadata is cached on the
  SQLAlchemy ``Session``, which ``get_db()`` creates per request and closes at the end of it,
  so the second and later callers do a dict lookup. Deliberately *not* a TTL cache: nothing
  survives the request, so a module generated or deleted since the previous request is
  counted immediately, exactly as before.
* **Fetch one prefix's objects concurrently.** Each ``get_object`` is a small payload behind
  a full round-trip, so latency dominates and the work parallelises almost perfectly.

This module is only an index: it returns raw ``(s3_key, metadata)`` pairs and holds no course
matching logic, because callers disagree about the rules. ``get_total_modules_count`` matches
a legacy module by exact normalized source-doc name while ``_count_course_modules_s3`` also
accepts a substring match, and keeping those rules at the call sites is what makes this
change count-for-count identical to the code it replaced.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.runtime import S3_BUCKET_NAME, S3_METADATA_MAX_WORKERS, s3_client
from utils.security_helpers import log_safe

logger = logging.getLogger(__name__)

# Key under which the per-request index lives on Session.info. SQLAlchemy leaves this dict
# entirely to the application, and a Session is never shared between requests.
_CACHE_KEY = "_s3_report_metadata_by_author"

# Listing timestamps, kept beside the metadata index: only the legacy report-to-course matching
# in `_get_grouped_reports` needs them, and they come free with the listing above.
_LAST_MODIFIED_CACHE_KEY = "_s3_report_last_modified_by_author"

# `db.info` dicts owned by other modules that hold values computed *from* this index - module
# counts per course. They are only valid while the index is, so they have to be dropped with it.
# Owners register at import time instead of every invalidation site popping each key by hand:
# that hand-written list is what silently went stale when the newest count cache was added.
_DERIVED_CACHE_KEYS: set = set()

ReportEntry = Tuple[str, Dict[str, Any]]


def _fetch_report_metadata(s3_key: str) -> Optional[Dict[str, Any]]:
    """One report's metadata, or ``None`` if it cannot be read or is not a JSON object."""
    try:
        body = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)["Body"].read()
        metadata = json.loads(body.decode("utf-8"))
        return metadata if isinstance(metadata, dict) else None
    except Exception as exc:
        # Matches the behaviour of every call site this replaced: an unreadable or malformed
        # object is skipped, never fatal to the page that asked for a count.
        logger.debug("[MODULE INDEX] Could not read %s: %s", log_safe(s3_key), log_safe(exc))
        return None


def author_report_metadata(db: Session, author_id: Any) -> List[ReportEntry]:
    """
    Every readable ``{author_id}/generated_reports/*.json`` as ``(s3_key, metadata)`` pairs.

    Built once per request and cached on ``db``; see the module docstring. The order of the
    returned list follows S3's lexicographic listing order, which is what the previous
    sequential scans produced, so callers that sort or de-duplicate behave unchanged.
    """
    cache = db.info.setdefault(_CACHE_KEY, {})
    last_modified_cache = db.info.setdefault(_LAST_MODIFIED_CACHE_KEY, {})
    if author_id in cache:
        return cache[author_id]

    s3_keys: List[str] = []
    last_modified: Dict[str, Any] = {}
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=S3_BUCKET_NAME,
            Prefix=f"{author_id}/generated_reports/",
        ):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".json"):
                    s3_keys.append(obj["Key"])
                    last_modified[obj["Key"]] = obj.get("LastModified")
    except Exception as exc:
        # A listing failure previously meant "no modules found", which sends the caller to its
        # CourseProgress fallback. Cache the empty result so one broken prefix cannot make the
        # request retry the failing call once per course.
        logger.error(
            "[MODULE INDEX] S3 listing failed for author %s: %s",
            log_safe(author_id),
            log_safe(exc),
        )
        cache[author_id] = []
        last_modified_cache[author_id] = {}
        return cache[author_id]

    entries: List[ReportEntry] = []
    if s3_keys:
        # boto3 clients are thread-safe and the connection pool is sized for this in
        # core/runtime.py (S3_MAX_POOL_CONNECTIONS). _fetch_report_metadata swallows every
        # exception, so map() cannot raise and one bad object cannot fail the whole index.
        workers = min(S3_METADATA_MAX_WORKERS, len(s3_keys))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for s3_key, metadata in zip(s3_keys, pool.map(_fetch_report_metadata, s3_keys)):
                if metadata is not None:
                    entries.append((s3_key, metadata))

    logger.info(
        "[MODULE INDEX] Indexed %s of %s metadata object(s) for author %s",
        len(entries),
        len(s3_keys),
        log_safe(author_id),
    )
    cache[author_id] = entries
    last_modified_cache[author_id] = last_modified
    return entries


def author_report_last_modified(db: Session, author_id: Any) -> Dict[str, Any]:
    """``{s3_key: LastModified}`` for the same objects ``author_report_metadata`` returns.

    Reads the cache the listing above fills, building it first if this is the first caller in
    the request, so asking for the timestamps never costs a second listing.
    """
    author_report_metadata(db, author_id)
    return db.info.get(_LAST_MODIFIED_CACHE_KEY, {}).get(author_id, {})


def register_derived_cache_key(key: str) -> None:
    """
    Declare a ``db.info`` dict whose values are derived from this index.

    ``invalidate_author_report_metadata`` then clears it too, so a module that memoises counts
    cannot keep serving a pre-change number after the index behind it was dropped.
    """
    _DERIVED_CACHE_KEYS.add(key)


def invalidate_author_report_metadata(db: Session, author_id: Any = None) -> None:
    """
    Drop the cached index so the next read re-scans S3.

    Needed only when the *same* request both changes the metadata objects and then reads a
    count back - generating, regenerating or deleting modules. Across requests the cache is
    already gone with the Session.
    """
    for key in (_CACHE_KEY, _LAST_MODIFIED_CACHE_KEY):
        cache = db.info.get(key)
        if not cache:
            continue
        if author_id is None:
            cache.clear()
        else:
            cache.pop(author_id, None)

    # Derived caches key by course, not by author, so there is nothing to pop selectively: an
    # author-scoped invalidation still has to clear them whole. That only costs a recompute,
    # whereas keeping an entry would return the count from before the change.
    for key in _DERIVED_CACHE_KEYS:
        derived = db.info.get(key)
        if derived:
            derived.clear()


__all__ = [
    "author_report_last_modified",
    "author_report_metadata",
    "invalidate_author_report_metadata",
    "register_derived_cache_key",
]
