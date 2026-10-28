"""Voice training settings — uses Smart Tutor env; no local file storage."""

from __future__ import annotations

import os


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


class VoiceTrainingSettings:
    use_polly: bool = os.environ.get("VOICE_TRAINING_USE_POLLY", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    polly_voice_id: str = os.environ.get("VOICE_TRAINING_POLLY_VOICE", "Joanna")
    polly_customer_voice_id: str = os.environ.get("VOICE_TRAINING_POLLY_CUSTOMER_VOICE", "Ruth")
    polly_engine: str = os.environ.get("VOICE_TRAINING_POLLY_ENGINE", "neural")
    auto_generate_scenarios: bool = os.environ.get(
        "VOICE_TRAINING_AUTO_GENERATE", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # Agent live speech (practice/assessment): en-IN for Indian English, en-US for US English.
    transcribe_language: str = os.environ.get("VOICE_TRAINING_TRANSCRIBE_LANGUAGE", "en-IN").strip() or "en-IN"
    use_aws_agent_stt: bool = os.environ.get("VOICE_TRAINING_USE_AWS_AGENT_STT", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # Max time (ms) the browser waits for AWS Transcribe before browser STT fallback.
    agent_stt_max_wait_ms: int = _int_env("VOICE_TRAINING_AGENT_STT_MAX_WAIT_MS", 12000)

    weight_intent: float = _float_env("VOICE_TRAINING_WEIGHT_INTENT", 0.15)
    weight_resolution: float = _float_env("VOICE_TRAINING_WEIGHT_RESOLUTION", 0.20)
    weight_sop: float = _float_env("VOICE_TRAINING_WEIGHT_SOP", 0.20)
    weight_tone: float = _float_env("VOICE_TRAINING_WEIGHT_TONE", 0.15)
    weight_voice: float = _float_env("VOICE_TRAINING_WEIGHT_VOICE", 0.10)
    weight_dialogue: float = _float_env("VOICE_TRAINING_WEIGHT_DIALOGUE", 0.10)
    weight_performance: float = _float_env("VOICE_TRAINING_WEIGHT_PERFORMANCE", 0.10)


_settings: VoiceTrainingSettings | None = None


def get_voice_training_settings() -> VoiceTrainingSettings:
    global _settings
    if _settings is None:
        _settings = VoiceTrainingSettings()
    return _settings
