"""Publish voice scenarios to batches and gate assessment after practice."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from utils.timezone_helpers import now_ist
from models.domain import BatchMember, TraineeBatch, User, VoiceScenario


def get_assessment_config(content: dict[str, Any] | None) -> dict[str, Any]:
    raw = (content or {}).get("assessment_config") or {}
    publish = (content or {}).get("publish") or {}
    min_practice = raw.get("min_practice_sessions", publish.get("min_practice_sessions", 1))
    max_attempts = raw.get("max_assessment_attempts", publish.get("max_assessment_attempts", 5))
    configured = bool(raw.get("configured")) or (
        bool(publish.get("is_published"))
        and (
            raw.get("min_practice_sessions") is not None
            or publish.get("min_practice_sessions") is not None
            or publish.get("max_assessment_attempts") is not None
        )
    )
    return {
        "configured": configured,
        "min_practice_sessions": max(0, int(min_practice or 0)),
        "max_assessment_attempts": max(1, int(max_attempts or 5)),
        "pass_percentage": max(1, min(100, int(raw.get("pass_percentage", 70) or 70))),
        "configured_at": raw.get("configured_at"),
        "configured_by": raw.get("configured_by"),
    }


def is_assessment_configured(content: dict[str, Any] | None) -> bool:
    return bool(get_assessment_config(content).get("configured"))


def apply_assessment_config(
    content: dict[str, Any],
    *,
    min_practice_sessions: int,
    max_assessment_attempts: int,
    pass_percentage: int,
    configured_by: str,
) -> dict[str, Any]:
    updated = dict(content or {})
    updated["assessment_config"] = {
        "configured": True,
        "min_practice_sessions": max(0, min_practice_sessions),
        "max_assessment_attempts": max(1, min(max_assessment_attempts, 20)),
        "pass_percentage": max(1, min(pass_percentage, 100)),
        "configured_at": now_ist().isoformat(),
        "configured_by": configured_by,
    }
    return updated


def get_publish_config(content: dict[str, Any] | None) -> dict[str, Any]:
    raw = (content or {}).get("publish") or {}
    assessment = get_assessment_config(content)
    return {
        "is_published": bool(raw.get("is_published")),
        "publication_type": raw.get("publication_type") or "batch",
        "batch_ids": list(raw.get("batch_ids") or []),
        "trainee_ids": list(raw.get("trainee_ids") or []),
        "excluded_trainee_ids": list(raw.get("excluded_trainee_ids") or []),
        "min_practice_sessions": assessment["min_practice_sessions"],
        "max_assessment_attempts": assessment["max_assessment_attempts"],
        "pass_percentage": assessment["pass_percentage"],
        "published_at": raw.get("published_at"),
        "published_by": raw.get("published_by"),
    }


def resolve_scenario_publisher(
    db: Session,
    scenario_row: VoiceScenario,
    content: dict[str, Any] | None,
) -> dict[str, Any]:
    """Who published/shared this scenario to trainees (for trainee UI)."""
    publish = get_publish_config(content)
    if not publish.get("is_published"):
        return {}
    author = db.query(User).filter(User.id == scenario_row.author_id).first()
    publisher = author
    if publish.get("published_by"):
        pub_user = db.query(User).filter(User.id == publish["published_by"]).first()
        if pub_user:
            publisher = pub_user
    person = publisher or author
    if not person:
        return {}
    role = (person.role or "trainer").replace("_", " ")
    return {
        "email": person.email,
        "role": person.role,
        "role_label": role.title(),
        "author_email": author.email if author else None,
        "published_at": publish.get("published_at"),
    }


def apply_publish_config(
    content: dict[str, Any],
    *,
    publication_type: str,
    batch_ids: list[str],
    published_by: str,
    trainee_ids: list[str] | None = None,
    excluded_trainee_ids: list[str] | None = None,
) -> dict[str, Any]:
    updated = dict(content or {})
    existing = (content or {}).get("publish") or {}
    updated["publish"] = {
        "is_published": True,
        "publication_type": publication_type,
        "batch_ids": batch_ids,
        "trainee_ids": list(
            trainee_ids if trainee_ids is not None else existing.get("trainee_ids") or []
        ),
        "excluded_trainee_ids": list(
            excluded_trainee_ids
            if excluded_trainee_ids is not None
            else existing.get("excluded_trainee_ids") or []
        ),
        "published_at": now_ist().isoformat(),
        "published_by": published_by,
    }
    return updated


def apply_publish_audience(
    content: dict[str, Any],
    *,
    batch_ids: list[str] | None = None,
    trainee_ids: list[str] | None = None,
    excluded_trainee_ids: list[str] | None = None,
) -> dict[str, Any]:
    updated = dict(content or {})
    existing = dict((content or {}).get("publish") or {})
    if not existing.get("is_published"):
        raise ValueError("Scenario is not published")
    if batch_ids is not None:
        existing["batch_ids"] = list(batch_ids)
    if trainee_ids is not None:
        existing["trainee_ids"] = list(trainee_ids)
    if excluded_trainee_ids is not None:
        existing["excluded_trainee_ids"] = list(excluded_trainee_ids)
    if not list(existing.get("batch_ids") or []) and not list(existing.get("trainee_ids") or []):
        existing["is_published"] = False
        existing["unpublished_at"] = now_ist().isoformat()
    updated["publish"] = existing
    return updated


def apply_unpublish(content: dict[str, Any]) -> dict[str, Any]:
    """Mark scenario unpublished and clear batch assignment."""
    updated = dict(content or {})
    existing = dict((content or {}).get("publish") or {})
    existing.update(
        {
            "is_published": False,
            "batch_ids": [],
            "trainee_ids": [],
            "unpublished_at": now_ist().isoformat(),
        }
    )
    updated["publish"] = existing
    return updated


def _trainee_batch_ids(db: Session, trainee_id: str) -> set[str]:
    rows = (
        db.query(BatchMember.batch_id)
        .filter(BatchMember.trainee_id == trainee_id)
        .all()
    )
    return {row[0] for row in rows}


def scenario_visible_to_trainee(
    db: Session,
    scenario_row: VoiceScenario,
    trainee: User,
) -> bool:
    if trainee.role != "trainee":
        return False
    content = scenario_row.content or {}
    if content.get("archived"):
        return False
    publish = get_publish_config(content)
    if not publish["is_published"]:
        return False
    if scenario_row.process_id and trainee.process_id != scenario_row.process_id:
        return False
    excluded = set(publish.get("excluded_trainee_ids") or [])
    if trainee.id in excluded:
        return False
    direct = set(publish.get("trainee_ids") or [])
    if trainee.id in direct:
        return True
    pub_type = publish["publication_type"]
    if pub_type == "all_process":
        return True
    trainee_batches = _trainee_batch_ids(db, trainee.id)
    target_batches = set(publish.get("batch_ids") or [])
    return bool(trainee_batches & target_batches)


def list_published_scenarios_for_trainee(db: Session, trainee: User) -> list[VoiceScenario]:
    query = db.query(VoiceScenario).order_by(VoiceScenario.created_at.desc())
    if trainee.process_id:
        query = query.filter(
            (VoiceScenario.process_id == trainee.process_id)
            | (VoiceScenario.process_id.is_(None))
        )
    rows = query.all()
    return [row for row in rows if scenario_visible_to_trainee(db, row, trainee)]


def assessment_access(
    *,
    practice_count: int,
    assessment_count: int,
    publish: dict[str, Any],
    has_passed: bool = False,
    best_percentage: float = 0,
) -> dict[str, Any]:
    try:
        # Keep 0 valid (disable practice for trainees); invalid values fall back to 1
        required = max(0, int(publish.get("min_practice_sessions", 1)))
    except (TypeError, ValueError):
        required = 1
    max_attempts = publish.get("max_assessment_attempts", 5)
    pass_percentage = publish.get("pass_percentage", 70)
    practices_remaining = max(0, required - practice_count)
    unlocked = practice_count >= required
    attempts_left = max(0, max_attempts - assessment_count) if unlocked else 0
    can_assess = unlocked and attempts_left > 0 and not has_passed
    return {
        "practice_count": practice_count,
        "assessment_count": assessment_count,
        "min_practice_sessions": required,
        "max_assessment_attempts": max_attempts,
        "pass_percentage": pass_percentage,
        "practices_remaining": practices_remaining,
        "assessment_attempts_left": attempts_left,
        "assessment_unlocked": unlocked,
        "can_start_assessment": can_assess,
        # DF_52: for trainees only — Practice off when required practices is 0
        "practice_allowed": required > 0,
        "has_passed": has_passed,
        "assessment_passed": has_passed,
        "best_percentage": round(best_percentage, 1),
        "assessment_frozen": has_passed,
    }


def assessment_block_reason(access: dict[str, Any]) -> str | None:
    if access.get("can_start_assessment"):
        return None
    if access.get("has_passed") or access.get("assessment_passed"):
        best = access.get("best_percentage", 0)
        required = access.get("pass_percentage", 70)
        return (
            f"You passed this assessment ({best:.0f}% — required {required}%). "
            "No further attempts are allowed."
        )
    if not access.get("assessment_unlocked"):
        remaining = access.get("practices_remaining", 0)
        required = access.get("min_practice_sessions", 1)
        return (
            f"Complete {remaining} more practice session(s) before assessment "
            f"({access.get('practice_count', 0)}/{required} done)."
        )
    if access.get("assessment_attempts_left", 0) <= 0:
        return "You have used all assessment attempts for this scenario."
    return "Assessment is not available yet."


def _trainer_owned_batch_ids(db: Session, trainer_id: str) -> set[str]:
    rows = (
        db.query(TraineeBatch.id)
        .filter(TraineeBatch.trainer_id == trainer_id, TraineeBatch.is_active == True)
        .all()
    )
    return {row[0] for row in rows}


def resolve_publish_audience(
    db: Session,
    content: dict[str, Any] | None,
    *,
    trainer_id: str | None = None,
    process_id: str | None = None,
) -> dict[str, Any]:
    publish = get_publish_config(content)
    batch_ids = list(publish.get("batch_ids") or [])
    direct_ids = set(publish.get("trainee_ids") or [])
    excluded = set(publish.get("excluded_trainee_ids") or [])
    owned_batches = _trainer_owned_batch_ids(db, trainer_id) if trainer_id else None

    batches: list[dict[str, Any]] = []
    active_map: dict[str, dict[str, Any]] = {}

    # The selected batches and all of their members, in two queries. Publishing to several
    # batches previously cost a batch lookup plus a member query each, and the loop below still
    # walks `batch_ids` in order, so the batch and member ordering is unchanged.
    batch_by_id: dict[str, TraineeBatch] = {}
    members_by_batch: dict[str, list[tuple[Any, User]]] = defaultdict(list)
    if batch_ids:
        batch_query = db.query(TraineeBatch).filter(
            TraineeBatch.id.in_(batch_ids),
            TraineeBatch.is_active == True,
        )
        if owned_batches is not None:
            batch_query = batch_query.filter(TraineeBatch.trainer_id == trainer_id)
        batch_by_id = {row.id: row for row in batch_query.all()}
        if batch_by_id:
            for member, member_user in (
                db.query(BatchMember, User)
                .join(User, BatchMember.trainee_id == User.id)
                .filter(BatchMember.batch_id.in_(list(batch_by_id.keys())), User.role == "trainee")
                .order_by(User.email)
                .all()
            ):
                members_by_batch[member.batch_id].append((member, member_user))

    for batch_id in batch_ids:
        batch = batch_by_id.get(batch_id)
        if not batch:
            continue
        members = members_by_batch.get(batch.id, [])
        member_list = []
        for _member, user in members:
            member_list.append({"id": user.id, "email": user.email})
            if user.id in excluded:
                continue
            active_map[user.id] = {
                "id": user.id,
                "email": user.email,
                "source": "batch",
                "batch_id": batch.id,
                "batch_name": batch.batch_name,
            }
        batches.append(
            {
                "id": batch.id,
                "name": batch.batch_name,
                "member_count": len(member_list),
                "members": member_list,
            }
        )

    # Directly selected trainees, resolved in one query rather than one per id.
    direct_users_by_id: dict[str, User] = {}
    if direct_ids:
        direct_users_by_id = {
            row.id: row
            for row in db.query(User)
            .filter(User.id.in_(list(direct_ids)), User.role == "trainee")
            .all()
        }

    for trainee_id in direct_ids:
        if trainee_id in excluded or trainee_id in active_map:
            continue
        user = direct_users_by_id.get(trainee_id)
        if not user:
            continue
        active_map[trainee_id] = {
            "id": user.id,
            "email": user.email,
            "source": "direct",
            "batch_id": None,
            "batch_name": None,
        }

    available: list[dict[str, Any]] = []
    seen_available: set[str] = set()

    def _add_available(user: User, batch_id: str | None, batch_name: str | None, *, readd: bool = False) -> None:
        if user.id in seen_available:
            return
        if user.id in active_map and not readd:
            return
        seen_available.add(user.id)
        label = f"{user.email}"
        if batch_name:
            label += f" ({batch_name})"
        if readd:
            label += " — removed, click Add to restore"
        available.append(
            {
                "id": user.id,
                "email": user.email,
                "batch_id": batch_id,
                "batch_name": batch_name,
                "readd": readd,
                "label": label,
            }
        )

    if trainer_id:
        owned = list(_trainer_owned_batch_ids(db, trainer_id))
        if owned:
            rows = (
                db.query(User, BatchMember, TraineeBatch)
                .join(BatchMember, BatchMember.trainee_id == User.id)
                .join(TraineeBatch, TraineeBatch.id == BatchMember.batch_id)
                .filter(
                    BatchMember.batch_id.in_(owned),
                    TraineeBatch.is_active == True,
                    User.role == "trainee",
                )
                .order_by(User.email)
                .all()
            )
            for user, _member, batch in rows:
                _add_available(user, batch.id, batch.batch_name)

    if process_id:
        for user in (
            db.query(User)
            .filter(User.role == "trainee", User.process_id == process_id)
            .order_by(User.email)
            .all()
        ):
            _add_available(user, None, "Same process")

    # Removed trainees offered for re-adding, resolved in one query rather than one per id.
    excluded_users_by_id: dict[str, User] = {}
    if excluded:
        excluded_users_by_id = {
            row.id: row
            for row in db.query(User)
            .filter(User.id.in_(list(excluded)), User.role == "trainee")
            .all()
        }

    for trainee_id in excluded:
        user = excluded_users_by_id.get(trainee_id)
        if user:
            _add_available(user, None, None, readd=True)

    active_trainees = sorted(active_map.values(), key=lambda x: x["email"])
    return {
        "is_published": publish["is_published"],
        "publication_type": publish.get("publication_type"),
        "batches": batches,
        "active_trainees": active_trainees,
        "trainee_count": len(active_trainees),
        "excluded_trainee_ids": list(excluded),
        "direct_trainee_ids": list(direct_ids),
        "available_trainees": available,
        "batch_names": [b["name"] for b in batches],
    }


def publish_audience_summary(db: Session, content: dict[str, Any] | None, trainer_id: str | None, process_id: str | None = None) -> dict[str, Any]:
    audience = resolve_publish_audience(db, content, trainer_id=trainer_id, process_id=process_id)
    return {
        "batch_names": audience["batch_names"],
        "batch_count": len(audience["batches"]),
        "trainee_count": audience["trainee_count"],
        "publication_type": audience.get("publication_type"),
    }
