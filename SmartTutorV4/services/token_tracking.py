"""Token accumulation during module generation and publish-time persistence."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from core.runtime import BEDROCK_GENERATION_MODEL, BEDROCK_IMAGE_MODEL
from utils.aws_helper import extract_bedrock_usage_tokens
from models.domain import Course, JobStatus, TokenUsage
from werkzeug.utils import secure_filename

from services.knowledge_base import normalize_document_name
from services.tutor_cache import calculate_token_cost, track_token_usage

logger = logging.getLogger(__name__)

OP_MODULE_TEXT = "module_generation"
OP_MODULE_VISION = "module_vision"
OP_TUTOR = "tutor_query"
OP_CHAT = "chat_agent"

# Conservative estimates when Bedrock usage metadata is unavailable.
VISION_TOKENS_PER_IMAGE = 3500
MODULE_TEXT_TOKENS_PER_MODULE = 2000
WORDS_TO_TOKENS_RATIO = 1.3

_token_job_id: ContextVar[Optional[str]] = ContextVar("token_job_id", default=None)
_token_doc_name: ContextVar[Optional[str]] = ContextVar("token_doc_name", default=None)


@contextmanager
def module_generation_token_context(job_id: str, doc_name: str):
    """Bind job/doc so vision and text calls accumulate into job token_usage."""
    job_token = _token_job_id.set(job_id)
    doc_token = _token_doc_name.set(doc_name)
    try:
        yield
    finally:
        _token_job_id.reset(job_token)
        _token_doc_name.reset(doc_token)


def extract_claude_usage(response_body: dict) -> Tuple[int, int]:
    """Read input/output token counts from a Bedrock response body (Claude or Nova)."""
    return extract_bedrock_usage_tokens(response_body)


def estimate_tokens_from_words(text: str) -> int:
    if not text or not str(text).strip():
        return 0
    return max(1, int(len(str(text).split()) * WORDS_TO_TOKENS_RATIO))


def _normalize_doc_entry(entry: Any) -> Dict[str, int]:
    if isinstance(entry, dict):
        return {
            "module_text": int(entry.get("module_text") or 0),
            "module_vision": int(entry.get("module_vision") or 0),
        }
    if isinstance(entry, (int, float)):
        return {"module_text": int(entry), "module_vision": 0}
    return {"module_text": 0, "module_vision": 0}


def accumulate_job_doc_tokens(
    job_id: str,
    doc_name: str,
    category: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Accumulate tokens into the job's token_usage JSON (module_text | module_vision)."""
    from services.jobs import get_job_status_data, update_job_status

    if not job_id or not doc_name:
        return
    total = max(0, int(input_tokens or 0) + int(output_tokens or 0))
    if total <= 0:
        return

    status = get_job_status_data(job_id) or {}
    token_usage = dict(status.get("token_usage") or {})
    entry = _normalize_doc_entry(token_usage.get(doc_name))
    if category == OP_MODULE_VISION:
        entry["module_vision"] += total
    else:
        entry["module_text"] += total
    token_usage[doc_name] = entry
    update_job_status(job_id, token_usage=token_usage)


def record_vision_tokens_from_response(
    response_body: dict,
    *,
    job_id: Optional[str] = None,
    doc_name: Optional[str] = None,
    fallback_total: int = VISION_TOKENS_PER_IMAGE,
) -> int:
    """Record vision tokens from a Bedrock response; returns total tokens recorded."""
    input_tokens, output_tokens = extract_claude_usage(response_body)
    if input_tokens <= 0 and output_tokens <= 0:
        input_tokens = int(fallback_total * 0.7)
        output_tokens = fallback_total - input_tokens

    job_id = job_id or _token_job_id.get()
    doc_name = doc_name or _token_doc_name.get()
    if job_id and doc_name:
        accumulate_job_doc_tokens(
            job_id, doc_name, OP_MODULE_VISION, input_tokens, output_tokens
        )
    return input_tokens + output_tokens


def record_module_text_tokens(
    text_tokens: int,
    *,
    job_id: Optional[str] = None,
    doc_name: Optional[str] = None,
) -> None:
    if text_tokens <= 0:
        return
    job_id = job_id or _token_job_id.get()
    doc_name = doc_name or _token_doc_name.get()
    if not job_id or not doc_name:
        return
    input_tokens = int(text_tokens * 0.75)
    output_tokens = text_tokens - input_tokens
    accumulate_job_doc_tokens(
        job_id, doc_name, OP_MODULE_TEXT, input_tokens, output_tokens
    )


def get_document_generation_tokens(
    db: Session,
    user_id: str,
    source_doc_name: str,
) -> Dict[str, int]:
    """
    Resolve module text + vision tokens for a document from completed jobs.
    Falls back to zeros when no job data exists.
    """
    doc_norm = normalize_document_name(source_doc_name)
    text_total = 0
    vision_total = 0

    jobs = (
        db.query(JobStatus)
        .filter(JobStatus.user_id == user_id, JobStatus.status == "completed")
        .order_by(JobStatus.completed_at.desc().nullslast(), JobStatus.updated_at.desc())
        .limit(50)
        .all()
    )

    for job in jobs:
        if not job.token_usage:
            continue
        try:
            usage = job.token_usage
            if isinstance(usage, str):
                usage = json.loads(usage)
        except (TypeError, json.JSONDecodeError):
            continue

        for doc_key, entry in (usage or {}).items():
            if normalize_document_name(doc_key) != doc_norm:
                continue
            normalized = _normalize_doc_entry(entry)
            text_total = max(text_total, normalized["module_text"])
            vision_total = max(vision_total, normalized["module_vision"])

    return {"module_text": text_total, "module_vision": vision_total}


def count_vision_images_for_document(user_id: str, source_doc_name: str) -> int:
    """Count cached vision analyses in S3 for a source document."""
    from core.runtime import S3_BUCKET_NAME, s3_client

    prefix = f"{user_id}/image_analysis/{secure_filename(source_doc_name)}/"
    count = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
        count += len(page.get("Contents") or [])
    return count


def resolve_course_generation_tokens(
    db: Session,
    user_id: str,
    source_doc_name: str,
    module_count: int,
) -> Dict[str, int]:
    """
    Best-effort module generation token totals (text LLM + vision).
    Priority: completed job data → module count estimate + S3 vision image count.
    """
    from_job = get_document_generation_tokens(db, user_id, source_doc_name)
    text_tokens = from_job["module_text"]
    vision_tokens = from_job["module_vision"]

    if text_tokens <= 0 and module_count > 0:
        text_tokens = module_count * MODULE_TEXT_TOKENS_PER_MODULE

    if vision_tokens <= 0:
        image_count = count_vision_images_for_document(user_id, source_doc_name)
        if image_count > 0:
            vision_tokens = image_count * VISION_TOKENS_PER_IMAGE

    return {
        "module_text": text_tokens,
        "module_vision": vision_tokens,
        "total": text_tokens + vision_tokens,
    }


def _split_io_tokens(total_tokens: int, input_ratio: float = 0.75) -> Tuple[int, int]:
    if total_tokens <= 0:
        return 0, 0
    input_tokens = int(total_tokens * input_ratio)
    return input_tokens, total_tokens - input_tokens


def persist_course_generation_tokens(
    db: Session,
    user_id: str,
    course_id: str,
    source_doc_name: str,
    module_count: int,
    *,
    include_existing: bool = False,
) -> Dict[str, int]:
    """
    Write module text + vision TokenUsage rows for a course.
    Skips categories that already have records unless include_existing is True.
    """
    totals = resolve_course_generation_tokens(db, user_id, source_doc_name, module_count)
    course = db.query(Course).filter(Course.id == course_id).first()

    def _has_operation(op_type: str) -> bool:
        return (
            db.query(TokenUsage)
            .filter(
                TokenUsage.course_id == course_id,
                TokenUsage.user_id == user_id,
                TokenUsage.operation_type == op_type,
            )
            .count()
            > 0
        )

    if totals["module_text"] > 0 and (include_existing or not _has_operation(OP_MODULE_TEXT)):
        inp, out = _split_io_tokens(totals["module_text"])
        track_token_usage(db, user_id, OP_MODULE_TEXT, inp, out, course_id)

    if totals["module_vision"] > 0 and (include_existing or not _has_operation(OP_MODULE_VISION)):
        inp, out = _split_io_tokens(totals["module_vision"], input_ratio=0.7)
        track_token_usage(
            db,
            user_id,
            OP_MODULE_VISION,
            inp,
            out,
            course_id,
            BEDROCK_IMAGE_MODEL,
        )

    combined = totals["total"]
    if course and combined > 0:
        course.tokens_used = max(course.tokens_used or 0, combined)
        inp, out = _split_io_tokens(combined)
        course.generation_cost = calculate_token_cost(inp, out, BEDROCK_GENERATION_MODEL)

    return totals


def track_tutor_pre_generation_tokens(
    db: Session,
    user_id: str,
    course_id: str,
    tutor_tokens: int,
) -> None:
    """Persist tutor pre-generation tokens at publish time."""
    if tutor_tokens <= 0:
        return
    inp, out = _split_io_tokens(tutor_tokens, input_ratio=0.6)
    track_token_usage(db, user_id, OP_TUTOR, inp, out, course_id, BEDROCK_GENERATION_MODEL)
