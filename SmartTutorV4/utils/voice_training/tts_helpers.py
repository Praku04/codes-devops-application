"""TTS helpers — clear speech for digits, emails, and calendar dates."""

from __future__ import annotations

import html
import re

# 2+ consecutive digits (not part of a decimal like 149.99, and not ordinals).
_DIGIT_RUN_RE = re.compile(
    r"(?<!\d)(?<!\$)\d{2,}(?!\d)(?!\.\d)(?!(?:st|nd|rd|th)\b)",
    re.I,
)
# Day ordinals: 1st, 2nd, 3rd, 15th, 31st (bounded — no ReDoS).
_ORDINAL_RE = re.compile(r"(?<!\d)\b([1-9]|[12]\d|3[01])(st|nd|rd|th)\b", re.I)
# Month + day with optional ordinal: October 15 / Oct. 15th / 15 October
_MONTH = (
    r"January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_MONTH_DAY_RE = re.compile(
    rf"\b((?:{_MONTH})\.?)\s+([1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\b",
    re.I,
)
_DAY_MONTH_RE = re.compile(
    rf"\b([1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+((?:{_MONTH})\.?)\b",
    re.I,
)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _iter_email_spans(text: str):
    """
    Yield (start, end, email) for addresses matching former
    ``[\\w.+-]+@[\\w-]+(?:\\.[\\w-]+)+`` (linear; Sonar S5852).
    """
    if not text or "@" not in text:
        return
    n = len(text)
    i = 0
    while i < n:
        at = text.find("@", i)
        if at < 0:
            return
        j = at - 1
        while j >= 0 and (_is_word_char(text[j]) or text[j] in ".+-"):
            j -= 1
        local_start = j + 1
        if local_start >= at:
            i = at + 1
            continue
        k = at + 1
        while k < n and (_is_word_char(text[k]) or text[k] == "-"):
            k += 1
        if k == at + 1:
            i = at + 1
            continue
        dots = 0
        while k < n and text[k] == ".":
            k += 1
            label_start = k
            while k < n and (_is_word_char(text[k]) or text[k] == "-"):
                k += 1
            if k == label_start:
                break
            dots += 1
        if dots < 1:
            i = at + 1
            continue
        yield local_start, k, text[local_start:k]
        i = k


class _EmailSpan:
    __slots__ = ("_start", "_end", "_value")

    def __init__(self, start: int, end: int, value: str):
        self._start = start
        self._end = end
        self._value = value

    def start(self, _n: int = 0) -> int:
        return self._start

    def end(self, _n: int = 0) -> int:
        return self._end

    def group(self, _n: int = 0) -> str:
        return self._value


class _EmailFinder:
    """Drop-in for email ``finditer`` / ``sub`` without a backtracking regex."""

    def finditer(self, text: str):
        for start, end, value in _iter_email_spans(text or ""):
            yield _EmailSpan(start, end, value)

    def sub(self, repl, text: str) -> str:
        raw = text or ""
        parts: list[str] = []
        last = 0
        for start, end, value in _iter_email_spans(raw):
            parts.append(raw[last:start])
            span = _EmailSpan(start, end, value)
            parts.append(repl(span) if callable(repl) else repl)
            last = end
        parts.append(raw[last:])
        return "".join(parts)


_EMAIL_RE = _EmailFinder()

_DIGIT_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)

_ORDINAL_WORDS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
    20: "twentieth",
    21: "twenty-first",
    22: "twenty-second",
    23: "twenty-third",
    24: "twenty-fourth",
    25: "twenty-fifth",
    26: "twenty-sixth",
    27: "twenty-seventh",
    28: "twenty-eighth",
    29: "twenty-ninth",
    30: "thirtieth",
    31: "thirty-first",
}


def _digits_to_words(digits: str) -> str:
    return " ".join(_DIGIT_WORDS[int(ch)] for ch in digits if ch.isdigit())


def _day_to_ordinal_word(day: int) -> str:
    return _ORDINAL_WORDS.get(day, str(day))


def _email_to_spoken(email: str) -> str:
    """john.doe@example.com → john dot doe at example dot com"""
    local, sep, domain = email.partition("@")
    if not sep:
        return email

    def _spell_part(part: str) -> str:
        return (
            part.replace(".", " dot ")
            .replace("_", " underscore ")
            .replace("-", " hyphen ")
            .replace("+", " plus ")
        )

    return f"{_spell_part(local)} at {_spell_part(domain)}"


def _merge_spans(raw: str, events: list[tuple[int, int, str]]) -> str:
    """Build output from non-overlapping (start, end, replacement) spans."""
    events.sort(key=lambda x: x[0])
    parts: list[str] = []
    last = 0
    for start, end, replacement in events:
        if start < last:
            continue
        if start > last:
            parts.append(raw[last:start])
        parts.append(replacement)
        last = end
    if last < len(raw):
        parts.append(raw[last:])
    return "".join(parts)


def expand_dates_for_speech(text: str) -> str:
    """Turn calendar day ordinals into spoken words so TTS does not spell 'th'."""
    raw = text or ""
    if not raw.strip():
        return raw

    events: list[tuple[int, int, str]] = []

    for match in _MONTH_DAY_RE.finditer(raw):
        day = int(match.group(2))
        events.append((match.start(), match.end(), f"{match.group(1)} {_day_to_ordinal_word(day)}"))

    for match in _DAY_MONTH_RE.finditer(raw):
        if any(start <= match.start() < end for start, end, _ in events):
            continue
        day = int(match.group(1))
        events.append((match.start(), match.end(), f"{_day_to_ordinal_word(day)} {match.group(2)}"))

    for match in _ORDINAL_RE.finditer(raw):
        if any(start <= match.start() < end for start, end, _ in events):
            continue
        day = int(match.group(1))
        events.append((match.start(), match.end(), _day_to_ordinal_word(day)))

    if not events:
        return raw
    return _merge_spans(raw, events)


def _replace_emails_with_spoken(text: str) -> str:
    return _EMAIL_RE.sub(lambda m: _email_to_spoken(m.group(0)), text or "")


def format_digits_for_speech(text: str) -> str:
    """Plain text for browser speechSynthesis — dates, emails, and digits spoken clearly."""

    raw = expand_dates_for_speech(text or "")
    if not raw.strip():
        return raw

    events: list[tuple[int, int, str]] = []
    for match in _EMAIL_RE.finditer(raw):
        events.append((match.start(), match.end(), _email_to_spoken(match.group(0))))
    for match in _DIGIT_RUN_RE.finditer(raw):
        if any(start <= match.start() < end for start, end, _ in events):
            continue
        events.append((match.start(), match.end(), _digits_to_words(match.group(0))))

    if not events:
        return raw
    return _merge_spans(raw, events)


def wrap_digits_for_polly_ssml(text: str) -> str:
    """Neural-safe Polly SSML — spoken dates/emails, say-as digits only (no <sub> or pitch)."""

    raw = expand_dates_for_speech((text or "").strip())
    raw = _replace_emails_with_spoken(raw)
    if not raw:
        return ""

    parts: list[str] = []
    last = 0
    for match in _DIGIT_RUN_RE.finditer(raw):
        if match.start() > last:
            parts.append(html.escape(raw[last : match.start()]))
        digits = match.group(0)
        parts.append(f'<say-as interpret-as="digits">{digits}</say-as>')
        last = match.end()
    if last < len(raw):
        parts.append(html.escape(raw[last:]))
    return "".join(parts) if parts else html.escape(raw)
