"""In-app notifications for voice training publish and assessment events."""

from __future__ import annotations

from sqlalchemy.orm import Session

from models.domain import Notification, User


def notify_voice_scenario_published(
    db: Session,
    *,
    trainee_ids: list[str],
    scenario_title: str,
    publisher_email: str,
) -> int:
    if not trainee_ids:
        return 0
    title = (scenario_title or "Voice training scenario").strip()
    publisher = (publisher_email or "your trainer").strip()
    sent = 0
    for trainee_id in set(trainee_ids):
        db.add(
            Notification(
                user_id=trainee_id,
                message=(
                    f"New voice training scenario assigned: '{title}' by {publisher}. "
                    "Open Voice Training Agent to practice and take the assessment."
                ),
                related_link="/voice-training",
            )
        )
        sent += 1
    return sent


def notify_voice_assessment_completed(
    db: Session,
    *,
    trainee: User,
    scenario_title: str,
    author_id: str | None,
    final_percentage: float,
    grade: str,
    pass_percentage: int,
    passed: bool,
) -> None:
    title = (scenario_title or "Voice training scenario").strip()
    if passed:
        db.add(
            Notification(
                user_id=trainee.id,
                message=(
                    f"Congratulations! You passed '{title}' with {final_percentage:.1f}% "
                    f"(required {pass_percentage}%). Assessment is now locked — no further attempts needed."
                ),
                related_link="/voice-training",
            )
        )
    if author_id and author_id != trainee.id:
        author = db.query(User).filter(User.id == author_id).first()
        if author:
            status = "passed" if passed else "completed"
            db.add(
                Notification(
                    user_id=author.id,
                    message=(
                        f"Trainee {trainee.email} {status} voice assessment for '{title}' — "
                        f"Grade {grade} ({final_percentage:.1f}%)."
                    ),
                    related_link="/voice-training",
                )
            )
