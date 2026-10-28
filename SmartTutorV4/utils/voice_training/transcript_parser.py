"""Parse uploaded call transcripts into training script turns."""

from __future__ import annotations

import re
from typing import Any

from utils.voice_training.script_turns import MIN_SCRIPT_TURNS, MAX_SCRIPT_TURNS

_AGENT_ALIASES = frozenset(
    {"agent", "trainee", "representative", "rep", "advisor", "support", "associate"}
)
_CUSTOMER_ALIASES = frozenset({"customer", "caller", "client", "user"})

_FILE_PRIMARY_HINTS = re.compile(
    r"\b(only|just|solely|exclusively)\b.{0,40}\b(file|upload|document|recording|transcript|audio|wav|mp3)\b"
    r"|\b(use|follow|from|based on)\b.{0,30}\b(file|upload|document|recording|transcript|audio)\b"
    r"|\bdo not\b.{0,30}\b(other|else|another)\b",
    re.I,
)

_SPEAKER_LINE = re.compile(
    r"^(?:#?\d+\s*[-.:)]*\s*)?"
    r"(Agent|Customer|Trainee|Caller|Representative|Rep|Support|Client)"
    r"(?:\s*\([^)]*\))?"
    r"\s*:?\s*(.*)$",
    re.I,
)

_NUMBERED_BLOCK = re.compile(
    r"#?(\d+)\s*\n\s*"
    r"(Agent|Customer|Trainee|Caller|Representative|Rep|Support|Client)"
    r"(?:\s*\([^)]*\))?"
    r"\s*\n\s*"
    r"(.+?)(?=\n#?\d+\s*\n|\Z)",
    re.I | re.S,
)

# Longest-first so "Representative" wins over "Rep"
_LABELED_SPEAKERS = (
    "representative",
    "customer",
    "trainee",
    "caller",
    "support",
    "client",
    "agent",
    "rep",
)


class _LabeledLineMatch:
    """Minimal re.Match stand-in for labeled Agent/Customer lines."""

    __slots__ = ("_speaker", "_body")

    def __init__(self, speaker: str, body: str):
        self._speaker = speaker
        self._body = body

    def group(self, n: int = 0) -> str:
        if n == 1:
            return self._speaker
        if n == 2:
            return self._body
        if n == 0:
            return f"{self._speaker}: {self._body}"
        raise IndexError(n)


def _match_labeled_line(line: str) -> _LabeledLineMatch | None:
    """
    Parse ``Speaker: text`` lines linearly (Sonar S2631-safe).
    Same shape as former ``^(Agent|...)\\s*:\\s*(.+)$`` with IGNORECASE.
    """
    if not line:
        return None
    lower = line.lower()
    for label in _LABELED_SPEAKERS:
        if not lower.startswith(label):
            continue
        j = len(label)
        n = len(line)
        while j < n and line[j].isspace():
            j += 1
        if j >= n or line[j] != ":":
            continue
        j += 1
        rest = line[j:]
        # Former ``\s*(.+)``: strip leading spaces unless that leaves an empty body.
        if not rest:
            continue
        stripped = rest.lstrip()
        body = stripped if stripped else rest
        return _LabeledLineMatch(line[: len(label)], body)
    return None


class _LabeledLineFinder:
    def match(self, line: str) -> _LabeledLineMatch | None:
        return _match_labeled_line(line)


_LABELED_LINE = _LabeledLineFinder()

_AGENT_GREETING_HINTS = (
    "thank you for calling",
    "thanks for calling",
    "how can i help",
    "my name is",
    "this call may be recorded",
)

_IVR_SYSTEM_PHRASES = (
    "please stay on the line",
    "while you are connected",
    "you are connected to",
    "please continue to hold",
    "all representatives are busy",
    "estimated wait time",
    "move on in",
    "thank you for holding",
    "your call is important",
)


def is_ivr_or_system_line(text: str) -> bool:
    """True for hold music / IVR lines that should not become agent script turns."""
    lower = (text or "").lower().replace("\u2019", "'")
    if not lower.strip():
        return True
    if any(phrase in lower for phrase in _IVR_SYSTEM_PHRASES):
        return True
    if "stay on the line" in lower and "connected" in lower:
        return True
    return False


def clean_raw_transcript(text: str) -> str:
    """Drop leading IVR/hold noise from a raw transcription block."""
    if not text or not text.strip():
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    started = False
    for part in parts:
        chunk = part.strip()
        if not chunk:
            continue
        if not started and is_ivr_or_system_line(chunk):
            continue
        started = True
        kept.append(chunk)
    if kept:
        return " ".join(kept)
    return text.strip()


_AGENT_LINE_HINTS = (
    "thank you for calling",
    "how may i help",
    "how can i help",
    "sorry for the inconvenience",
    "i do see",
    "we value your business",
    "we do really value",
    "last four digits",
    "email address",
    "for validation",
    "initiate a refund",
    "refund for",
    "five-device plan",
    "five device plan",
    "switch you to",
    "anything else i can help",
    "have a great day",
    "this call may be recorded",
    "let me check",
    "as i see here",
    "when it comes for the renewal",
    "automatic renewal",
    "mcafee account",
    "unlimited device plan",
    "you're welcome",
)

_CUSTOMER_LINE_HINTS = (
    "you are making a fool",
    "fool out of people",
    "on the website",
    "ten years",
    "existing with you",
    "one forty nine",
    "$149",
    "149.99",
    "forty nine dollars",
    "forty-nine dollars",
    "no, no, no",
    "but is this the value",
    "same plan with all",
    "i have five devices",
    "that's right",
    "name is",
    "card ending",
    "this is the first time",
    "basically you are",
    "charged me",
    "same plan for",
)


def infer_speaker_role(text: str) -> str | None:
    """Heuristic agent vs customer classification from line content."""
    lower = (text or "").lower().replace("\u2019", "'")
    if not lower.strip():
        return None
    agent_score = sum(1 for hint in _AGENT_LINE_HINTS if hint in lower)
    customer_score = sum(1 for hint in _CUSTOMER_LINE_HINTS if hint in lower)
    if agent_score > customer_score:
        return "trainee"
    if customer_score > agent_score:
        return "customer"
    if re.fullmatch(r"\d{4,5}[.!]?", lower.strip()) or re.search(
        r"^(it is|that'?s|yes,?)\s+\d{4}", lower
    ):
        return "customer"
    if any(hint in lower for hint in _AGENT_GREETING_HINTS):
        return "trainee"
    return None


_AGENT_ONLY_PHRASES = (
    "how may i help",
    "how can i help",
    "thank you for calling",
    "sorry for the inconvenience",
    "i do see",
    "we value your business",
    "we do really value",
    "let me check",
    "anything else i can help",
    "have a great day",
    "as i see here",
    "when it comes for the renewal",
    "initiate a refund",
    "switch you to",
)

_CUSTOMER_ONLY_PHRASES = (
    "you are making a fool",
    "fool out of people",
    "on the website",
    "is this how you value",
    "is this the value you give",
    "existing with you from",
    "ten years",
    "one forty nine",
    "149.99",
)


def script_speaker_sanity(turns: list[dict[str, str]]) -> bool:
    """False when obvious agent/customer lines are on the wrong speaker."""
    if not turns:
        return False
    if turns[0].get("speaker") != "trainee":
        return False
    violations = 0
    for turn in turns:
        text = (turn.get("text") or "").lower().replace("\u2019", "'")
        speaker = turn.get("speaker")
        if not text or speaker not in ("trainee", "customer"):
            continue
        if speaker == "customer" and any(phrase in text for phrase in _AGENT_ONLY_PHRASES):
            violations += 1
        if speaker == "trainee" and any(phrase in text for phrase in _CUSTOMER_ONLY_PHRASES):
            violations += 1
    return violations == 0


def relabel_speaker_turns(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """Fix speaker labels only — preserves turn count for validated scripts."""
    if not turns:
        return turns

    relabeled: list[dict[str, str]] = []
    for idx, turn in enumerate(turns):
        text = _clean_turn_text(turn.get("text", ""))
        if not text or is_ivr_or_system_line(text):
            continue

        inferred = infer_speaker_role(text)
        labeled = turn.get("speaker")
        if idx == 0:
            speaker = "trainee"
        elif inferred:
            speaker = inferred
        elif labeled in ("trainee", "customer"):
            speaker = labeled
        elif relabeled:
            last = relabeled[-1]["speaker"]
            speaker = "customer" if last == "trainee" else "trainee"
        else:
            speaker = "trainee" if idx % 2 == 0 else "customer"

        relabeled.append({"speaker": speaker, "text": text})
    return relabeled


def repair_speaker_labels_heuristic(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """Fix swapped agent/customer labels using content cues; may merge same-speaker lines."""
    relabeled = relabel_speaker_turns(turns)
    if not relabeled:
        return relabeled

    repaired: list[dict[str, str]] = []
    for turn in relabeled:
        speaker = turn["speaker"]
        text = turn["text"]
        if repaired and repaired[-1]["speaker"] == speaker:
            repaired[-1]["text"] = f"{repaired[-1]['text']} {text}".strip()
        else:
            repaired.append({"speaker": speaker, "text": text})
    return repaired


def finalize_upload_script_turns(
    turns: list[dict[str, str]],
    description: str = "",
    persona: str = "",
) -> list[dict[str, str]]:
    """Polish upload-derived turns: drop IVR noise and ensure a professional agent greeting."""
    from utils.voice_training.script_turns import (
        build_agent_opening_greeting,
        sanitize_agent_opening,
        shorten_speech_text,
    )

    cleaned = merge_short_turns(turns)

    while cleaned and cleaned[0].get("speaker") != "trainee":
        if is_ivr_or_system_line(cleaned[0].get("text", "")):
            cleaned.pop(0)
        else:
            break

    if not cleaned:
        cleaned = [
            {
                "speaker": "trainee",
                "text": shorten_speech_text(
                    build_agent_opening_greeting(description, persona, cleaned),
                    "trainee",
                ),
            }
        ]
    else:
        if cleaned[0].get("speaker") == "trainee":
            cleaned[0]["text"] = sanitize_agent_opening(
                cleaned[0].get("text", ""),
                description,
                persona,
                cleaned,
            )
        else:
            cleaned.insert(
                0,
                {
                    "speaker": "trainee",
                    "text": shorten_speech_text(
                        build_agent_opening_greeting(description, persona, cleaned),
                        "trainee",
                    ),
                },
            )

    normalized = _normalize_upload_script_turns(cleaned)
    from utils.voice_training.script_turns import repair_name_role_confusion

    return repair_name_role_confusion(normalized, description, persona)


def _normalize_upload_script_turns(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """Trim and merge upload turns without reordering agent/customer sequence."""
    from utils.voice_training.script_turns import (
        coalesce_script_turns,
        repair_dangling_text,
        shorten_speech_text,
    )

    merged = coalesce_script_turns(turns)
    result: list[dict[str, str]] = []
    for turn in merged:
        speaker = turn.get("speaker")
        if speaker not in ("trainee", "customer"):
            continue
        text = shorten_speech_text(repair_dangling_text(turn.get("text", "")), speaker)
        if text:
            result.append({"speaker": speaker, "text": text})
    return result


def is_structured_transcript(text: str) -> bool:
    """True when the upload already has parseable Agent/Customer lines."""
    turns = parse_transcript_to_turns(text)
    if len(turns) >= 4:
        return True
    labeled = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _LABELED_LINE.match(line) or _SPEAKER_LINE.match(line):
            labeled += 1
    return labeled >= 4


def trainer_notes_for_upload(
    custom_prompt: str | None,
    custom_use_case: str | None = None,
) -> str | None:
    """Trainer instructions for uploads — follow tone/rules; facts must come from the file."""
    sections: list[str] = []
    if custom_prompt and custom_prompt.strip():
        sections.append(
            "Story / call description (follow for training focus, tone, and emphasis):\n"
            f"{custom_prompt.strip()[:800]}"
        )
    if custom_use_case and custom_use_case.strip():
        sections.append(
            "Policies and resolution rules (apply when consistent with the uploaded content):\n"
            f"{custom_use_case.strip()[:400]}"
        )
    if not sections:
        return None
    return (
        "TRAINER INSTRUCTIONS — follow these for approach, de-escalation, and training goals. "
        "All customer names, products, dollar amounts, and dialogue facts MUST come from the "
        "uploaded file/transcript. Do NOT substitute different people, charges, or storylines "
        "from these notes.\n\n"
        + "\n\n".join(sections)
    )


def file_primary_generation(custom_prompt: str | None, has_uploads: bool) -> bool:
    """True when uploaded files should drive scenario content, not category templates."""
    if not has_uploads:
        return False
    if custom_prompt and _FILE_PRIMARY_HINTS.search(custom_prompt):
        return True
    return True


def _strip_document_header(block: str) -> str:
    """Remove combine_document_texts header line (--- filename ---) without regex."""
    lines = (block or "").splitlines()
    if not lines:
        return ""
    first = lines[0].strip()
    if first.startswith("--- ") and first.endswith(" ---"):
        return "\n".join(lines[1:]).strip()
    return block.strip()


def extract_transcript_body(document_context: str) -> str:
    """Strip combine_document_texts headers and return the raw transcript body."""
    if not document_context or not document_context.strip():
        return ""
    blocks: list[str] = []
    for chunk in document_context.split("\n\n"):
        body = _strip_document_header(chunk)
        if body:
            blocks.append(body)
    if blocks:
        return "\n\n".join(blocks)
    return document_context.strip()


def _normalize_speaker(label: str) -> str | None:
    key = (label or "").strip().lower()
    if key in _AGENT_ALIASES:
        return "trainee"
    if key in _CUSTOMER_ALIASES:
        return "customer"
    return None


def _clean_turn_text(text: str) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned.strip(' "\'')


def coerce_scenario_script_turns(raw_turns: list[Any]) -> list[dict[str, str]]:
    """Normalize Bedrock/LLM script turns to trainee|customer with non-empty text."""
    result: list[dict[str, str]] = []
    for item in raw_turns or []:
        if not isinstance(item, dict):
            continue
        speaker = _normalize_speaker(str(item.get("speaker") or ""))
        if not speaker:
            continue
        text = _clean_turn_text(str(item.get("text") or ""))
        if text:
            result.append({"speaker": speaker, "text": text})
    return result


def parse_transcript_to_turns(text: str) -> list[dict[str, str]]:
    """Best-effort parse of labeled or numbered transcript text into script turns."""
    if not text or not text.strip():
        return []

    turns: list[dict[str, str]] = []

    for match in _NUMBERED_BLOCK.finditer(text):
        speaker = _normalize_speaker(match.group(2))
        line = _clean_turn_text(match.group(3))
        if speaker and line:
            turns.append({"speaker": speaker, "text": line})

    if turns:
        return turns

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _SPEAKER_LINE.match(line)
        if match:
            speaker = _normalize_speaker(match.group(1))
            body = _clean_turn_text(match.group(2) or "")
            if speaker and body:
                turns.append({"speaker": speaker, "text": body})
                continue
        match = _LABELED_LINE.match(line)
        if match:
            speaker = _normalize_speaker(match.group(1))
            body = _clean_turn_text(match.group(2) or "")
            if speaker and body:
                turns.append({"speaker": speaker, "text": body})

    return turns


def turns_from_diarized_items(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build labeled turns from AWS Transcribe items with speaker_label."""
    if not items:
        return []

    grouped: list[tuple[str, str]] = []
    current_speaker: str | None = None
    current_words: list[str] = []

    for item in items:
        if item.get("type") != "pronunciation":
            continue
        speaker = str(item.get("speaker_label") or "").strip()
        word = str((item.get("alternatives") or [{}])[0].get("content") or "").strip()
        if not speaker or not word:
            continue
        if current_speaker and speaker != current_speaker and current_words:
            grouped.append((current_speaker, " ".join(current_words)))
            current_words = []
        current_speaker = speaker
        current_words.append(word)

    if current_speaker and current_words:
        grouped.append((current_speaker, " ".join(current_words)))

    if not grouped:
        return []

    speaker_roles = _map_diarization_speakers(grouped)
    turns: list[dict[str, str]] = []
    for speaker, utterance in grouped:
        role = speaker_roles.get(speaker)
        text = _clean_turn_text(utterance)
        if role and text:
            turns.append({"speaker": role, "text": text})
    return turns


def _map_diarization_speakers(grouped: list[tuple[str, str]]) -> dict[str, str]:
    """Map spk_0 / spk_1 labels to trainee (agent) vs customer."""
    speakers = []
    seen: dict[str, None] = {}
    for speaker, _ in grouped:
        if speaker not in seen:
            seen[speaker] = None
            speakers.append(speaker)

    if len(speakers) == 1:
        return {speakers[0]: "trainee"}

    scores: dict[str, int] = {spk: 0 for spk in speakers}
    for idx, (speaker, utterance) in enumerate(grouped[:6]):
        lower = utterance.lower()
        if any(hint in lower for hint in _AGENT_GREETING_HINTS):
            scores[speaker] = scores.get(speaker, 0) + 3
        if idx == 0:
            scores[speaker] = scores.get(speaker, 0) + 1

    agent_speaker = max(speakers, key=lambda spk: scores.get(spk, 0))
    mapping: dict[str, str] = {}
    for spk in speakers:
        mapping[spk] = "trainee" if spk == agent_speaker else "customer"
    return mapping


def format_turns_as_labeled_text(turns: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for turn in turns:
        speaker = turn.get("speaker", "")
        label = "Agent" if speaker == "trainee" else "Customer"
        text = turn.get("text", "")
        if text:
            lines.append(f"{label}: {text}")
    return "\n".join(lines)


def transcript_has_enough_turns(turns: list[dict[str, str]], minimum: int = MIN_SCRIPT_TURNS) -> bool:
    return len([t for t in turns if (t.get("text") or "").strip()]) >= minimum


def merge_short_turns(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge consecutive turns from the same speaker (common after diarization splits)."""
    if not turns:
        return []
    merged: list[dict[str, str]] = []
    for turn in turns:
        speaker = turn.get("speaker")
        text = _clean_turn_text(turn.get("text", ""))
        if not speaker or not text:
            continue
        if merged and merged[-1]["speaker"] == speaker:
            merged[-1]["text"] = f"{merged[-1]['text']} {text}".strip()
        else:
            merged.append({"speaker": speaker, "text": text})
    return merged


_UPLOAD_TRAINEE_BRIDGES = (
    "I understand.",
    "Thank you for explaining that.",
    "Let me check that for you.",
    "One moment please.",
    "Absolutely.",
)
_UPLOAD_CUSTOMER_BRIDGES = (
    "Okay.",
    "I see.",
    "Right.",
    "Mm-hmm.",
    "Yes.",
)


def expand_upload_turns_to_minimum(
    turns: list[dict[str, str]],
    minimum: int = MIN_SCRIPT_TURNS,
    maximum: int = MAX_SCRIPT_TURNS,
) -> list[dict[str, str]]:
    """Append brief closing beats at the end only — never split or reorder the main dialogue."""
    if not turns:
        return turns
    result = merge_short_turns(turns)
    if len(result) >= minimum:
        return result[:maximum]

    ti = ci = 0
    guard = 0
    while len(result) < minimum and guard < minimum * 3:
        guard += 1
        last = result[-1]["speaker"] if result else "customer"
        if last == "trainee":
            result.append(
                {"speaker": "customer", "text": _UPLOAD_CUSTOMER_BRIDGES[ci % len(_UPLOAD_CUSTOMER_BRIDGES)]}
            )
            ci += 1
        else:
            result.append(
                {"speaker": "trainee", "text": _UPLOAD_TRAINEE_BRIDGES[ti % len(_UPLOAD_TRAINEE_BRIDGES)]}
            )
            ti += 1

    return result[:maximum]
