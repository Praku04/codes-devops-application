"""S3 key helpers for voice training assets (existing Smart Tutor bucket)."""

from __future__ import annotations

import re
from werkzeug.utils import secure_filename

_SAFE_ID = re.compile(r"[^a-zA-Z0-9._-]+")


def sanitize_id(value: str) -> str:
    cleaned = _SAFE_ID.sub("", (value or "").strip())
    if not cleaned:
        raise ValueError("Invalid identifier")
    return cleaned


def scenario_doc_key(user_id: str, scenario_id: str, filename: str) -> str:
    safe_name = secure_filename(filename) or "document"
    return f"{user_id}/voice_training/scenarios/{sanitize_id(scenario_id)}/docs/{safe_name}"


def assessment_pdf_key(user_id: str, assessment_id: str) -> str:
    return f"{user_id}/voice_training/assessments/{sanitize_id(assessment_id)}_report.pdf"


def session_tts_key(user_id: str, session_id: str, turn_id: str) -> str:
    return (
        f"{user_id}/voice_training/sessions/{sanitize_id(session_id)}"
        f"/tts/{sanitize_id(turn_id)}.mp3"
    )


def transcribe_input_key(user_id: str, job_id: str, filename: str) -> str:
    safe_name = secure_filename(filename) or "audio.wav"
    return f"{user_id}/voice_training/transcribe/{sanitize_id(job_id)}/{safe_name}"
