"""Shared token analytics sync and aggregation for trainer and super-admin dashboards."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.runtime import BEDROCK_GENERATION_MODEL, BEDROCK_IMAGE_MODEL
from models.domain import ChatMessage, ChatSession, Course, TokenUsage, User
from services.knowledge_base import normalize_document_name
from services.module_index import author_report_metadata
from services.token_tracking import (
    OP_CHAT,
    OP_MODULE_TEXT,
    OP_MODULE_VISION,
    OP_TUTOR,
    VISION_TOKENS_PER_IMAGE,
    count_vision_images_for_document,
    resolve_course_generation_tokens,
)
from services.tutor_cache import calculate_token_cost
from utils.security_helpers import log_safe

logger = logging.getLogger(__name__)

MODULE_GEN_TOKENS_PER_MODULE = 2000
CHAT_TOKENS_PER_MESSAGE = 2000


def _estimate_cost_cents(tokens_used: int, input_ratio: float = 0.75, *, vision: bool = False) -> int:
    estimated_input = int(tokens_used * input_ratio)
    estimated_output = tokens_used - estimated_input
    pricing_model = BEDROCK_IMAGE_MODEL if vision else BEDROCK_GENERATION_MODEL
    return calculate_token_cost(
        estimated_input,
        estimated_output,
        pricing_model,
    )


def _count_course_modules_s3(db: Session, course: Course) -> int:
    """
    Count module JSON files in S3 that belong to this course.

    Reads the per-request metadata index instead of scanning S3 itself: this is called for
    every course of every trainer being synced, so the old per-course scan made the token
    analytics tabs cost trainers x courses x objects S3 round-trips and time out. The looser
    substring matching below is kept exactly as it was - it is deliberately more permissive
    than get_total_modules_count's, and narrowing it here would change reported token costs.
    """
    module_count = 0
    course_docs_normalized: List[str] = []
    try:
        if course.source_documents:
            source_docs_list = json.loads(course.source_documents)
            if isinstance(source_docs_list, list):
                for doc in source_docs_list:
                    course_docs_normalized.append(normalize_document_name(doc))
    except Exception as exc:
        logger.warning(
            "Could not parse source_documents for course %s: %s",
            log_safe(course.id),
            log_safe(exc),
        )

    if not course_docs_normalized:
        course_docs_normalized = [normalize_document_name(course.source_doc_name)]

    for _s3_key, meta_data in author_report_metadata(db, course.author_id):
        source_doc_normalized = normalize_document_name(meta_data.get("source_doc", ""))
        is_match = any(
            source_doc_normalized == course_doc_norm
            or course_doc_norm in source_doc_normalized
            or source_doc_normalized in course_doc_norm
            for course_doc_norm in course_docs_normalized
        )
        if is_match:
            module_count += 1
    return module_count


def _repair_zero_cost_records(db: Session, user_id: int) -> int:
    """Recalculate cost for a user's TokenUsage rows that have zero cost."""
    zero_cost_records = (
        db.query(TokenUsage)
        .filter(
            TokenUsage.user_id == user_id,
            or_(TokenUsage.cost_cents == 0, TokenUsage.cost_cents.is_(None)),
        )
        .all()
    )
    # The courses those records point at, in one query rather than one per record. The rows are
    # updated below, so they must be ORM objects and not a values() projection.
    courses_by_id: Dict[Any, Course] = {}
    repair_course_ids = {r.course_id for r in zero_cost_records if r.course_id}
    if repair_course_ids:
        courses_by_id = {
            row.id: row
            for row in db.query(Course).filter(Course.id.in_(repair_course_ids)).all()
        }

    repaired = 0
    for record in zero_cost_records:
        if record.tokens_used <= 0:
            continue
        is_vision = record.operation_type == OP_MODULE_VISION
        calculated_cost = _estimate_cost_cents(
            record.tokens_used,
            input_ratio=0.7 if is_vision else 0.75,
            vision=is_vision,
        )
        if calculated_cost <= 0:
            continue
        record.cost_cents = calculated_cost
        if record.course_id:
            course = courses_by_id.get(record.course_id)
            if course and (not course.generation_cost or course.generation_cost == 0):
                course.generation_cost = calculated_cost
        repaired += 1
    return repaired


def _backfill_module_generation_for_course(
    db: Session,
    course: Course,
    existing_operations: Optional[Set[Tuple[Any, str]]] = None,
) -> bool:
    """Create module_generation / module_vision TokenUsage when missing.

    `existing_operations` is the optional prefetched set of `(course_id, operation_type)` pairs
    that already have rows; when it is given the two existence counts below are skipped, which
    is what keeps a caller looping over every course of an author to a single query.
    """
    if existing_operations is None:
        text_exists = (
            db.query(TokenUsage)
            .filter(
                TokenUsage.course_id == course.id,
                TokenUsage.operation_type == OP_MODULE_TEXT,
            )
            .count()
            > 0
        )
        vision_exists = (
            db.query(TokenUsage)
            .filter(
                TokenUsage.course_id == course.id,
                TokenUsage.operation_type == OP_MODULE_VISION,
            )
            .count()
            > 0
        )
    else:
        text_exists = (course.id, OP_MODULE_TEXT) in existing_operations
        vision_exists = (course.id, OP_MODULE_VISION) in existing_operations
    if text_exists and vision_exists:
        return False

    module_count = _count_course_modules_s3(db, course)
    totals = resolve_course_generation_tokens(
        db,
        course.author_id,
        course.source_doc_name,
        module_count,
    )

    if not text_exists:
        if totals["module_text"] <= 0 and course.tokens_used and course.tokens_used > 0:
            totals["module_text"] = course.tokens_used
        if totals["module_text"] > 0:
            cost_cents = (
                course.generation_cost
                if course.generation_cost and course.generation_cost > 0
                else _estimate_cost_cents(totals["module_text"])
            )
            db.add(
                TokenUsage(
                    user_id=course.author_id,
                    course_id=course.id,
                    operation_type=OP_MODULE_TEXT,
                    tokens_used=totals["module_text"],
                    cost_cents=cost_cents,
                )
            )

    if not vision_exists and totals["module_vision"] <= 0:
        image_count = count_vision_images_for_document(
            course.author_id, course.source_doc_name
        )
        if image_count > 0:
            totals["module_vision"] = image_count * VISION_TOKENS_PER_IMAGE

    if not vision_exists and totals["module_vision"] > 0:
        db.add(
            TokenUsage(
                user_id=course.author_id,
                course_id=course.id,
                operation_type=OP_MODULE_VISION,
                tokens_used=totals["module_vision"],
                cost_cents=_estimate_cost_cents(totals["module_vision"], input_ratio=0.7, vision=True),
            )
        )

    created = (not text_exists and totals["module_text"] > 0) or (
        not vision_exists and totals["module_vision"] > 0
    )
    if created:
        combined = totals["module_text"] + totals["module_vision"]
        if not course.tokens_used or course.tokens_used < combined:
            course.tokens_used = combined
    return created


def _repair_chat_token_overcount(db: Session, user: User) -> int:
    """Trim duplicate chat backfill rows left by older sync logic."""
    ai_message_count = (
        db.query(ChatMessage)
        .join(ChatSession)
        .filter(ChatSession.user_id == user.id, ChatMessage.role == "ai")
        .count()
    )
    expected_total_tokens = ai_message_count * CHAT_TOKENS_PER_MESSAGE
    chat_records = (
        db.query(TokenUsage)
        .filter(
            TokenUsage.user_id == user.id,
            TokenUsage.operation_type == OP_CHAT,
        )
        .order_by(TokenUsage.timestamp.asc())
        .all()
    )
    total_tokens = sum(record.tokens_used for record in chat_records)
    if total_tokens <= expected_total_tokens:
        return 0

    excess = total_tokens - expected_total_tokens
    repaired = 0
    for record in reversed(chat_records):
        if excess <= 0:
            break
        if record.tokens_used <= excess:
            excess -= record.tokens_used
            db.delete(record)
            repaired += 1
            continue
        record.tokens_used -= excess
        record.cost_cents = _estimate_cost_cents(record.tokens_used, input_ratio=0.6)
        excess = 0
        repaired += 1
    return repaired


def _backfill_chat_tokens_for_user(db: Session, user: User) -> bool:
    """Create chat_agent TokenUsage when AI messages exist without matching token totals."""
    ai_message_count = (
        db.query(ChatMessage)
        .join(ChatSession)
        .filter(ChatSession.user_id == user.id, ChatMessage.role == "ai")
        .count()
    )
    existing_chat_token_sum = (
        db.query(func.coalesce(func.sum(TokenUsage.tokens_used), 0))
        .filter(
            TokenUsage.user_id == user.id,
            TokenUsage.operation_type == OP_CHAT,
        )
        .scalar()
    ) or 0

    expected_total_tokens = ai_message_count * CHAT_TOKENS_PER_MESSAGE
    if expected_total_tokens <= existing_chat_token_sum:
        return False

    tokens_to_backfill = expected_total_tokens - existing_chat_token_sum
    cost_cents = _estimate_cost_cents(tokens_to_backfill, input_ratio=0.6)

    db.add(
        TokenUsage(
            user_id=user.id,
            course_id=None,
            operation_type=OP_CHAT,
            tokens_used=tokens_to_backfill,
            cost_cents=cost_cents,
        )
    )
    return True


def sync_user_token_usage(db: Session, user: User) -> int:
    """
    Repair and backfill token data for one user.
    Returns the number of records created or repaired.
    """
    changes = 0
    changes += _repair_zero_cost_records(db, user.id)
    changes += _repair_chat_token_overcount(db, user)

    courses = db.query(Course).filter(Course.author_id == user.id).all()
    # Which module-generation rows already exist, for every course at once. The backfill below
    # needs only that yes/no per course and operation, and asking per course made this sync -
    # which runs before the trainer and super-admin stats tabs render - grow with the course
    # count. `courses` holds each course once, so no entry is read after the loop changes it.
    existing_operations = set()
    course_ids = [course.id for course in courses]
    if course_ids:
        existing_operations = {
            (row[0], row[1])
            for row in db.query(TokenUsage.course_id, TokenUsage.operation_type)
            .filter(
                TokenUsage.course_id.in_(course_ids),
                TokenUsage.operation_type.in_([OP_MODULE_TEXT, OP_MODULE_VISION]),
            )
            .distinct()
            .all()
        }

    for course in courses:
        if _backfill_module_generation_for_course(db, course, existing_operations):
            changes += 1

    if _backfill_chat_tokens_for_user(db, user):
        changes += 1

    if changes > 0:
        db.commit()
        db.expire_all()

    return changes


def _build_course_breakdown(
    db: Session,
    user: User,
    courses: List[Course],
) -> List[Dict[str, Any]]:
    """Per-course token usage scoped to the course author."""
    course_breakdown: List[Dict[str, Any]] = []

    # One query for every course's rows instead of one per course: the author's token analytics
    # tab loops over all of their courses, so the per-course query made the page cost grow with
    # the course count and was one of the reads that ran past the browser's request timeout.
    usage_by_course: Dict[Any, List[TokenUsage]] = defaultdict(list)
    course_ids = [course.id for course in courses]
    if course_ids:
        for row in (
            db.query(TokenUsage)
            .filter(
                TokenUsage.course_id.in_(course_ids),
                TokenUsage.user_id == user.id,
            )
            .all()
        ):
            usage_by_course[row.course_id].append(row)

    for course in courses:
        usage = usage_by_course.get(course.id, [])

        module_text_tokens = sum(
            u.tokens_used for u in usage if u.operation_type == OP_MODULE_TEXT
        )
        module_vision_tokens = sum(
            u.tokens_used for u in usage if u.operation_type == OP_MODULE_VISION
        )
        module_gen_tokens = module_text_tokens + module_vision_tokens
        tutor_tokens = sum(
            u.tokens_used for u in usage if u.operation_type == OP_TUTOR
        )
        chat_tokens = sum(
            u.tokens_used for u in usage if u.operation_type == OP_CHAT
        )
        course_total_tokens = module_gen_tokens + tutor_tokens + chat_tokens
        course_total_cost = sum(u.cost_cents for u in usage)

        course_breakdown.append(
            {
                "course_id": course.id,
                "course_name": course.source_doc_name,
                "course_title": course.course_title,
                "tokens_used": course_total_tokens,
                "cost_cents": course_total_cost,
                "module_gen_tokens": module_gen_tokens,
                "module_text_tokens": module_text_tokens,
                "module_vision_tokens": module_vision_tokens,
                "tutor_queries_tokens": tutor_tokens,
                "chat_agent_tokens": chat_tokens,
                "module_gen_count": len(
                    [
                        u
                        for u in usage
                        if u.operation_type in (OP_MODULE_TEXT, OP_MODULE_VISION)
                    ]
                ),
                "module_vision_count": len(
                    [u for u in usage if u.operation_type == OP_MODULE_VISION]
                ),
                "tutor_queries_count": len(
                    [u for u in usage if u.operation_type == OP_TUTOR]
                ),
                "chat_agent_count": len(
                    [u for u in usage if u.operation_type == OP_CHAT]
                ),
                "created_at": course.created_at.isoformat()
                if course.created_at
                else None,
            }
        )

    course_breakdown.sort(key=lambda item: item["tokens_used"], reverse=True)
    return course_breakdown


def build_user_token_analytics(db: Session, user: User) -> Dict[str, Any]:
    """Compute token analytics for one user from persisted TokenUsage rows."""
    user_usage = db.query(TokenUsage).filter(TokenUsage.user_id == user.id).all()
    total_tokens = sum(u.tokens_used for u in user_usage)
    total_cost_cents = sum((u.cost_cents or 0) for u in user_usage)
    if total_tokens > 0 and total_cost_cents <= 0:
        total_cost_cents = sum(
            _estimate_cost_cents(
                u.tokens_used,
                input_ratio=(
                    0.7
                    if u.operation_type == OP_MODULE_VISION
                    else 0.6
                    if u.operation_type == OP_CHAT
                    else 0.75
                ),
                vision=u.operation_type == OP_MODULE_VISION,
            )
            for u in user_usage
            if u.tokens_used > 0
        )

    operations = {
        "module_generation": sum(
            u.tokens_used
            for u in user_usage
            if u.operation_type in (OP_MODULE_TEXT, OP_MODULE_VISION)
        ),
        "module_text": sum(
            u.tokens_used for u in user_usage if u.operation_type == OP_MODULE_TEXT
        ),
        "module_vision": sum(
            u.tokens_used for u in user_usage if u.operation_type == OP_MODULE_VISION
        ),
        "tutor_query": sum(
            u.tokens_used for u in user_usage if u.operation_type == OP_TUTOR
        ),
        "chat_agent": sum(
            u.tokens_used for u in user_usage if u.operation_type == OP_CHAT
        ),
    }
    operation_counts = {
        "module_generation": len(
            [
                u
                for u in user_usage
                if u.operation_type in (OP_MODULE_TEXT, OP_MODULE_VISION)
            ]
        ),
        "module_text": len(
            [u for u in user_usage if u.operation_type == OP_MODULE_TEXT]
        ),
        "module_vision": len(
            [u for u in user_usage if u.operation_type == OP_MODULE_VISION]
        ),
        "tutor_query": len(
            [u for u in user_usage if u.operation_type == OP_TUTOR]
        ),
        "chat_agent": len(
            [u for u in user_usage if u.operation_type == OP_CHAT]
        ),
    }

    courses = db.query(Course).filter(Course.author_id == user.id).all()
    course_breakdown = _build_course_breakdown(db, user, courses)

    # Name every referenced course once. A record can point at a course the user no longer
    # authors, so this is a fresh lookup rather than a reuse of `courses` above - but one query
    # for all of them instead of one per usage record, which is what made a long history slow.
    referenced_course_ids = {record.course_id for record in user_usage if record.course_id}
    course_name_by_id: Dict[Any, Any] = {}
    if referenced_course_ids:
        course_name_by_id = {
            row.id: row.source_doc_name
            for row in db.query(Course).filter(Course.id.in_(referenced_course_ids)).all()
        }

    usage_details = []
    for record in user_usage:
        course_name = "N/A"
        if record.course_id and record.course_id in course_name_by_id:
            course_name = course_name_by_id[record.course_id]

        usage_details.append(
            {
                "id": record.id,
                "operation_type": record.operation_type,
                "tokens_used": record.tokens_used,
                "cost_cents": record.cost_cents,
                "course_name": course_name,
                "course_id": record.course_id,
                "timestamp": record.timestamp.isoformat()
                if record.timestamp
                else None,
            }
        )

    return {
        "total_tokens": total_tokens,
        "total_cost_cents": total_cost_cents,
        "total_cost_dollars": round(total_cost_cents / 100, 2),
        "courses_generated": len(courses),
        "operations": operations,
        "operation_counts": operation_counts,
        "courses": course_breakdown,
        "usage_details": usage_details,
        "total_records": len(user_usage),
    }


def get_user_token_analytics(db: Session, user: User) -> Dict[str, Any]:
    """Sync backfill/repair then return analytics for one user."""
    sync_user_token_usage(db, user)
    return build_user_token_analytics(db, user)


def build_admin_user_stat(db: Session, user: User) -> Dict[str, Any]:
    """Sync and build a single user's stats for the super-admin dashboard."""
    analytics = get_user_token_analytics(db, user)
    process_name = user.process.name if user.process else "N/A"
    course_tokens = sum(c.get("tokens_used", 0) for c in analytics.get("courses", []))
    return {
        "user_id": user.id,
        "email": user.email,
        "role": user.role,
        "process": process_name,
        "total_tokens": analytics["total_tokens"],
        "total_cost_cents": analytics["total_cost_cents"],
        "total_cost_dollars": analytics["total_cost_dollars"],
        "course_tokens": course_tokens,
        "other_tokens": max(0, analytics["total_tokens"] - course_tokens),
        "operations": analytics["operations"],
        "operation_counts": analytics["operation_counts"],
        "courses": analytics.get("courses", []),
    }
