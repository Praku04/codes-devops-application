"""Amazon Polly TTS — audio stored in S3 (no local files)."""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any

from utils.voice_training.config import get_voice_training_settings
from utils.voice_training.storage import VoiceTrainingStore
from utils.voice_training.tts_helpers import format_digits_for_speech, wrap_digits_for_polly_ssml


def _customer_ssml(text: str, *, engine: str) -> str:
    raw = (text or "").strip()
    raw = (
        raw.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    safe = wrap_digits_for_polly_ssml(raw)
    safe = re.sub(r"([.!?])\s+", r'\1<break time="300ms"/> ', safe)
    neural = (engine or "").strip().lower() == "neural"
    # Neural engine supports prosody rate only — not pitch or volume.
    if neural:
        return f"<speak><prosody rate=\"95%\">{safe}</prosody></speak>"
    return f'<speak><prosody rate="95%" pitch="+1%">{safe}</prosody></speak>'


def _is_invalid_ssml_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name == "InvalidSsmlException":
        return True
    msg = str(exc).lower()
    return "invalidssml" in name.lower() or "unsupported neural" in msg


class VoicePollyService:
    def __init__(self, polly_client: Any, store: VoiceTrainingStore):
        self.settings = get_voice_training_settings()
        self._client = polly_client if self.settings.use_polly and polly_client else None
        self.store = store

    def _synthesize_sync(
        self,
        text: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        *,
        customer: bool = False,
    ) -> dict[str, Any]:
        voice_id = (
            self.settings.polly_customer_voice_id if customer else self.settings.polly_voice_id
        )
        kwargs: dict[str, Any] = {
            "OutputFormat": "mp3",
            "VoiceId": voice_id,
            "Engine": self.settings.polly_engine,
        }
        if customer:
            kwargs["Text"] = _customer_ssml(text, engine=self.settings.polly_engine)
            kwargs["TextType"] = "ssml"
        else:
            kwargs["Text"] = format_digits_for_speech(text)
            kwargs["TextType"] = "text"

        try:
            response = self._client.synthesize_speech(**kwargs)
        except Exception as exc:
            if customer and _is_invalid_ssml_error(exc):
                kwargs["Text"] = format_digits_for_speech(text)
                kwargs["TextType"] = "text"
                response = self._client.synthesize_speech(**kwargs)
            else:
                raise
        audio_bytes = response["AudioStream"].read()
        s3_key = self.store.upload_tts_audio(user_id, session_id, turn_id, audio_bytes)
        return {
            "audio_url": f"/voice-training/audio/{session_id}/{turn_id}",
            "audio_base64": base64.b64encode(audio_bytes).decode("utf-8"),
            "format": "mp3",
            "s3_key": s3_key,
        }

    async def synthesize(
        self,
        text: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        *,
        customer: bool = False,
    ) -> dict[str, Any]:
        if self._client:
            return await asyncio.to_thread(
                self._synthesize_sync,
                text,
                user_id,
                session_id,
                turn_id,
                customer=customer,
            )

        return {
            "audio_url": None,
            "audio_base64": None,
            "format": "mp3",
            "use_browser_tts": True,
            "text": format_digits_for_speech(text),
        }

    async def synthesize_customer(
        self,
        text: str,
        user_id: str,
        session_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        return await self.synthesize(
            text,
            user_id,
            session_id,
            turn_id,
            customer=True,
        )
