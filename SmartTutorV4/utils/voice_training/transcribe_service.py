"""AWS Transcribe — convert uploaded call recordings to text for scenario context.

Uses the same ``S3_BUCKET_NAME`` as the rest of Smart Tutor (wired via
``VoiceTrainingDependencies`` in ``main.py``). Flow:

1. Upload audio to ``{user_id}/voice_training/transcribe/{job_id}/{filename}``
2. Start a Transcribe job pointing at ``s3://{bucket}/{key}``
3. Poll until complete, fetch transcript JSON
4. Remove only the Transcribe *job record* (not the S3 audio file)

Uploaded audio remains in S3 under the path above (same bucket as the app).

IAM on the app role (same region as ``AWS_REGION``):

- ``transcribe:StartTranscriptionJob``, ``transcribe:GetTranscriptionJob``,
  ``transcribe:DeleteTranscriptionJob`` (job metadata only — not S3 objects)
- ``transcribe:StartStreamTranscription`` (live agent reply streaming STT)
- ``s3:PutObject``, ``s3:GetObject`` on
  ``arn:aws:s3:::{bucket}/*/voice_training/transcribe/*``

Supported uploads: WAV, MP3, M4A, FLAC, OGG, AMR, WEBM (max 50 MB).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

from utils.security_helpers import log_safe
from utils.voice_training.s3_keys import transcribe_input_key
from utils.voice_training.transcript_parser import (
    format_turns_as_labeled_text,
    merge_short_turns,
    turns_from_diarized_items,
)

AUDIO_EXTENSIONS = frozenset({"wav", "mp3", "mp4", "m4a", "flac", "ogg", "amr", "webm"})
MAX_AUDIO_BYTES = 50 * 1024 * 1024
POLL_INTERVAL_SEC = 3
MAX_WAIT_SEC = 300
REPLY_POLL_INTERVAL_SEC = 1
REPLY_MAX_WAIT_SEC = 45
REPLY_STREAM_CHUNK_BYTES = 4096
MAX_REPLY_AUDIO_BYTES = 8 * 1024 * 1024
MIN_REPLY_AUDIO_BYTES = 512

logger = logging.getLogger(__name__)

# Transcribe MediaFormat values (m4a is submitted as mp4).
_MEDIA_FORMAT = {
    "m4a": "mp4",
}

# S3 Content-Type hints (Transcribe reads MediaFormat, not Content-Type).
_CONTENT_TYPE = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "amr": "audio/amr",
    "webm": "audio/webm",
}


def is_audio_filename(filename: str) -> bool:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    return ext in AUDIO_EXTENSIONS


def _fetch_transcribe_result_json(uri: str) -> dict:
    """Fetch Transcribe result JSON — only AWS HTTPS endpoints are allowed (SSRF guard)."""
    parsed = urllib.parse.urlparse(uri or "")
    if parsed.scheme != "https":
        raise RuntimeError("Invalid transcript URI scheme.")
    host = (parsed.hostname or "").lower()
    if not host.endswith(".amazonaws.com"):
        raise RuntimeError("Invalid transcript URI host.")
    with urllib.request.urlopen(uri, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


class VoiceTranscribeService:
    def __init__(self, s3_client: Any, transcribe_client: Any, bucket: str, *, region: str | None = None):
        self.s3 = s3_client
        self.transcribe = transcribe_client
        self.bucket = bucket
        self.region = (region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1").strip()

    @property
    def is_configured(self) -> bool:
        return self.s3 is not None and self.transcribe is not None and bool(self.bucket)

    def transcribe_bytes(self, user_id: str, content: bytes, filename: str) -> str:
        if not self.is_configured:
            raise RuntimeError(
                "AWS Transcribe is not configured (missing S3 bucket or transcribe client)."
            )
        if len(content) > MAX_AUDIO_BYTES:
            raise ValueError(f"Audio file too large (max {MAX_AUDIO_BYTES // (1024 * 1024)} MB).")
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
        if ext not in AUDIO_EXTENSIONS:
            raise ValueError(f"Unsupported audio type: .{ext}")

        job_id = uuid.uuid4().hex[:12]
        s3_key = transcribe_input_key(user_id, job_id, filename)
        content_type = _CONTENT_TYPE.get(ext, "application/octet-stream")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
        )

        job_name = f"vt-{job_id}"[:200]
        media_uri = f"s3://{self.bucket}/{s3_key}"
        media_format = _MEDIA_FORMAT.get(ext, ext)

        use_speaker_labels = True
        try:
            self._start_transcription_job(job_name, media_uri, media_format, use_speaker_labels)
        except Exception as exc:
            logger.debug(
                "Speaker-label transcription unavailable for %s, retrying plain: %s",
                log_safe(filename),
                log_safe(exc),
            )
            use_speaker_labels = False
            self._start_transcription_job(job_name, media_uri, media_format, use_speaker_labels)

        deadline = time.time() + MAX_WAIT_SEC
        try:
            while time.time() < deadline:
                resp = self.transcribe.get_transcription_job(TranscriptionJobName=job_name)
                job = resp["TranscriptionJob"]
                status = job["TranscriptionJobStatus"]
                if status == "COMPLETED":
                    uri = job["Transcript"]["TranscriptFileUri"]
                    data = _fetch_transcribe_result_json(uri)
                    results = data.get("results") or {}
                    plain = (results.get("transcripts") or [{}])[0].get("transcript", "")
                    plain = (plain or "").strip()
                    text = plain
                    if use_speaker_labels:
                        items = results.get("items") or []
                        diarized_turns = merge_short_turns(turns_from_diarized_items(items))
                        if diarized_turns:
                            text = format_turns_as_labeled_text(diarized_turns)
                    if not text:
                        logger.warning(
                            "Transcription returned empty text for job %s (%s)",
                            log_safe(job_name),
                            log_safe(filename),
                        )
                        raise RuntimeError("Transcription returned empty text.")
                    return text
                if status == "FAILED":
                    reason = job.get("FailureReason", "Unknown error")
                    logger.warning(
                        "Transcription job failed for %s (%s): %s",
                        log_safe(job_name),
                        log_safe(filename),
                        log_safe(reason),
                    )
                    raise RuntimeError("Transcription failed.")
                time.sleep(POLL_INTERVAL_SEC)
            logger.warning(
                "Transcription timed out for job %s (%s) after %ss",
                log_safe(job_name),
                log_safe(filename),
                MAX_WAIT_SEC,
            )
            raise TimeoutError("Transcription timed out — try a shorter recording.")
        finally:
            try:
                self.transcribe.delete_transcription_job(TranscriptionJobName=job_name)
            except Exception as exc:
                logger.debug(
                    "Transcription job cleanup failed for %s: %s",
                    log_safe(job_name),
                    log_safe(exc),
                )

    def transcribe_reply_bytes(
        self,
        user_id: str,
        content: bytes,
        filename: str,
        *,
        language_code: str = "en-IN",
    ) -> str:
        """Transcribe a short agent reply — prefers streaming (low latency)."""
        if not self.is_configured:
            raise RuntimeError(
                "AWS Transcribe is not configured (missing S3 bucket or transcribe client)."
            )
        if len(content) > MAX_REPLY_AUDIO_BYTES:
            raise ValueError(f"Recording too large (max {MAX_REPLY_AUDIO_BYTES // (1024 * 1024)} MB).")
        if len(content) < MIN_REPLY_AUDIO_BYTES:
            raise ValueError("Recording too short — speak a little longer and try again.")
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
        if ext not in AUDIO_EXTENSIONS:
            raise ValueError(f"Unsupported audio type: .{ext}")
        lang = (language_code or "en-IN").strip() or "en-IN"
        try:
            pcm = self._audio_to_pcm16(content, filename)
            return asyncio.run(self._stream_transcribe_pcm(pcm, lang))
        except Exception as exc:
            logger.warning("Streaming agent reply transcription failed, using batch: %s", log_safe(exc))
            return self._transcribe_reply_batch(user_id, content, filename, language_code=lang)

    async def transcribe_reply_bytes_async(
        self,
        user_id: str,
        content: bytes,
        filename: str,
        *,
        language_code: str = "en-IN",
    ) -> str:
        """Async wrapper for agent reply transcription (streaming first)."""
        if not self.is_configured:
            raise RuntimeError(
                "AWS Transcribe is not configured (missing S3 bucket or transcribe client)."
            )
        if len(content) > MAX_REPLY_AUDIO_BYTES:
            raise ValueError(f"Recording too large (max {MAX_REPLY_AUDIO_BYTES // (1024 * 1024)} MB).")
        if len(content) < MIN_REPLY_AUDIO_BYTES:
            raise ValueError("Recording too short — speak a little longer and try again.")
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
        if ext not in AUDIO_EXTENSIONS:
            raise ValueError(f"Unsupported audio type: .{ext}")
        lang = (language_code or "en-IN").strip() or "en-IN"
        try:
            pcm = await asyncio.to_thread(self._audio_to_pcm16, content, filename)
            return await self._stream_transcribe_pcm(pcm, lang)
        except Exception as exc:
            logger.warning("Streaming agent reply transcription failed, using batch: %s", log_safe(exc))
            return await asyncio.to_thread(
                self._transcribe_reply_batch,
                user_id,
                content,
                filename,
                language_code=lang,
            )

    @staticmethod
    def _merge_transcript_parts(parts: list[str]) -> str:
        """Join streaming finals without duplicating repeated phrases."""
        cleaned: list[str] = []
        for part in parts:
            text = (part or "").strip()
            if not text:
                continue
            if cleaned and cleaned[-1].lower() == text.lower():
                continue
            if cleaned and text.lower().startswith(cleaned[-1].lower()):
                cleaned[-1] = text
                continue
            cleaned.append(text)
        return " ".join(cleaned).strip()

    @staticmethod
    def _audio_to_pcm16(content: bytes, filename: str) -> bytes:
        from pydub import AudioSegment

        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "webm").lower()
        fmt = _MEDIA_FORMAT.get(ext, ext)
        if fmt == "mp4":
            fmt = "mp4"
        seg = AudioSegment.from_file(io.BytesIO(content), format=fmt)
        seg = seg.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        return seg.raw_data

    async def _stream_transcribe_pcm(self, pcm: bytes, language_code: str) -> str:
        from amazon_transcribe.client import TranscribeStreamingClient
        from amazon_transcribe.handlers import TranscriptResultStreamHandler
        from amazon_transcribe.model import TranscriptEvent

        if not pcm:
            raise ValueError("Empty audio after conversion.")

        class _ReplyHandler(TranscriptResultStreamHandler):
            def __init__(self, transcript_result_stream) -> None:
                super().__init__(transcript_result_stream)
                self.final_parts: list[str] = []
                self.latest_partial = ""

            async def handle_transcript_event(self, transcript_event: TranscriptEvent) -> None:
                for result in transcript_event.transcript.results:
                    for alt in result.alternatives:
                        text = (alt.transcript or "").strip()
                        if not text:
                            continue
                        if result.is_partial:
                            self.latest_partial = text
                        else:
                            self.final_parts.append(text)

        client = TranscribeStreamingClient(region=self.region)
        stream = await client.start_stream_transcription(
            language_code=language_code,
            media_sample_rate_hz=16000,
            media_encoding="pcm",
        )
        handler = _ReplyHandler(stream.output_stream)

        async def _send_audio() -> None:
            for i in range(0, len(pcm), REPLY_STREAM_CHUNK_BYTES):
                chunk = pcm[i : i + REPLY_STREAM_CHUNK_BYTES]
                await stream.input_stream.send_audio_event(audio_chunk=chunk)
            await stream.input_stream.end_stream()

        await asyncio.gather(handler.handle_events(), _send_audio())
        text = self._merge_transcript_parts(handler.final_parts)
        if not text and handler.latest_partial:
            text = handler.latest_partial.strip()
        if not text:
            raise RuntimeError("Streaming transcription returned empty text.")
        return text

    def _transcribe_reply_batch(
        self,
        user_id: str,
        content: bytes,
        filename: str,
        *,
        language_code: str = "en-IN",
    ) -> str:
        """Batch fallback when streaming is unavailable (slower)."""
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
        job_id = uuid.uuid4().hex[:12]
        s3_key = transcribe_input_key(user_id, job_id, filename)
        content_type = _CONTENT_TYPE.get(ext, "application/octet-stream")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
        )

        job_name = f"vt-reply-{job_id}"[:200]
        media_uri = f"s3://{self.bucket}/{s3_key}"
        media_format = _MEDIA_FORMAT.get(ext, ext)
        lang = (language_code or "en-IN").strip() or "en-IN"

        kwargs: dict[str, Any] = {
            "TranscriptionJobName": job_name,
            "Media": {"MediaFileUri": media_uri},
            "MediaFormat": media_format,
            "LanguageCode": lang,
        }
        self.transcribe.start_transcription_job(**kwargs)

        deadline = time.time() + REPLY_MAX_WAIT_SEC
        try:
            while time.time() < deadline:
                resp = self.transcribe.get_transcription_job(TranscriptionJobName=job_name)
                job = resp["TranscriptionJob"]
                status = job["TranscriptionJobStatus"]
                if status == "COMPLETED":
                    uri = job["Transcript"]["TranscriptFileUri"]
                    data = _fetch_transcribe_result_json(uri)
                    results = data.get("results") or {}
                    plain = (results.get("transcripts") or [{}])[0].get("transcript", "")
                    text = (plain or "").strip()
                    if not text:
                        raise RuntimeError("Transcription returned empty text.")
                    return text
                if status == "FAILED":
                    reason = job.get("FailureReason", "Unknown error")
                    logger.warning(
                        "Reply transcription failed for %s: %s",
                        log_safe(job_name),
                        log_safe(reason),
                    )
                    raise RuntimeError("Transcription failed.")
                time.sleep(REPLY_POLL_INTERVAL_SEC)
            raise TimeoutError("Transcription timed out — try a shorter reply or type your message.")
        finally:
            try:
                self.transcribe.delete_transcription_job(TranscriptionJobName=job_name)
            except Exception as exc:
                logger.debug(
                    "Reply transcription job cleanup failed for %s: %s",
                    log_safe(job_name),
                    log_safe(exc),
                )

    def _start_transcription_job(
        self,
        job_name: str,
        media_uri: str,
        media_format: str,
        use_speaker_labels: bool,
    ) -> None:
        kwargs: dict[str, Any] = {
            "TranscriptionJobName": job_name,
            "Media": {"MediaFileUri": media_uri},
            "MediaFormat": media_format,
            "LanguageCode": "en-US",
        }
        if use_speaker_labels:
            kwargs["Settings"] = {
                "ShowSpeakerLabels": True,
                "MaxSpeakerLabels": 2,
            }
        self.transcribe.start_transcription_job(**kwargs)
