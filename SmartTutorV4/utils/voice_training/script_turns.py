"""Script-aligned turn tracking for practice conversations."""

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Optional

# Voice-call style: short turns, more back-and-forth
MIN_SCRIPT_TURNS = 25
MAX_SCRIPT_TURNS = 30
MAX_CUSTOMER_WORDS = 22
MAX_TRAINEE_WORDS = 28
MAX_SENTENCES_PER_TURN = 2

_DANGLING_TAIL_WORDS = frozenset(
    {
        "with", "the", "our", "your", "a", "an", "to", "for", "and", "or", "in", "on",
        "at", "is", "are", "was", "be", "have", "has", "will", "can", "of", "by", "as",
        "if", "but", "so", "we", "i", "my", "their", "this", "that",
    }
)


def _ends_with_dangling_tail(text: str) -> bool:
    """True if text ends with a dangling function-word (former DANGLING_TAIL regex)."""
    t = (text or "").rstrip()
    if t.endswith("."):
        t = t[:-1].rstrip()
    if not t:
        return False
    i = len(t) - 1
    while i >= 0 and not t[i].isspace():
        i -= 1
    last = t[i + 1 :].lower()
    return last in _DANGLING_TAIL_WORDS


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def find_email(text: str) -> Optional[str]:
    """
    First ``local@domain.tld`` match (linear).
    Same shape as former ``EMAIL_RE`` / ``[\\w.+-]+@[\\w-]+\\.[a-z]{2,}``.
    """
    if not text or "@" not in text:
        return None
    n = len(text)
    i = 0
    while i < n:
        at = text.find("@", i)
        if at < 0:
            return None
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
        if k == at + 1 or k >= n or text[k] != ".":
            i = at + 1
            continue
        tld_start = k + 1
        t = tld_start
        while t < n and text[t].isalpha():
            t += 1
        if t - tld_start < 2:
            i = at + 1
            continue
        return text[local_start:t]
    return None


class _SimpleMatch:
    """Minimal re.Match stand-in for ``.group(0)`` callers."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def group(self, _n: int = 0) -> str:
        return self._value


class _EmailFinder:
    """Drop-in for ``EMAIL_RE.search`` without a backtracking regex (Sonar S5852)."""

    def search(self, text: str) -> Optional[_SimpleMatch]:
        found = find_email(text or "")
        return _SimpleMatch(found) if found else None


EMAIL_RE = _EmailFinder()
CARD_LAST4 = re.compile(
    r"(?:card|ending in|last four|last 4|digits?)\s*(?:is\s*)?(?:#|number\s*)?(\d{4})\b",
    re.I,
)
STANDALONE_LAST4 = re.compile(r"\b(\d{4})\b")
BIRTH_YEAR_HINTS = ("born", "birth", "dob", "date of birth", "birthday", "birth date")
MONTH_DATE_YEAR = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+\d{1,2},?\s+(\d{4})\b",
    re.I,
)
CARD_CONTEXT_HINTS = (
    "card", "ending in", "last four", "last 4", "last four digits", "last 4 digits",
    "visa", "mastercard", "amex", "american express", "debit", "credit card",
    "payment card", "billing card",
)
PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
AMOUNT_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
ACCOUNT_RE = re.compile(r"\b(?:account|order|ticket|case)\s*(?:#|number\s*)?([A-Z0-9-]{4,})\b", re.I)
AGENT_NAME_PAT = re.compile(
    r"(?:thank you|thanks|understand(?:\s+your)?(?:\s+urgency)?|sorry|hi|hello|dear),?\s+([A-Z][a-z]+)\b"
)
# "I'm Jane" / "my name is Jane Doe" — allow ! after a real intro.
CUSTOMER_IAM_NAME_PAT = re.compile(
    r"(?:my name is|i'm|i am)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
    r"(?=\s*(?:,|\.|!|$|and\b|from\b|with\b|calling\b|here\b|at\b))",
    re.I,
)
# "this is Jane." / "this is Jane from Acme" — do NOT treat "this is ridiculous!" as a name.
CUSTOMER_THIS_IS_NAME_PAT = re.compile(
    r"\bthis is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
    r"(?=\s*(?:,|\.|$|and\b|from\b|with\b|calling\b|here\b|at\b))",
    re.I,
)
# Back-compat alias used by intro helpers
CUSTOMER_NAME_PAT = CUSTOMER_IAM_NAME_PAT
TIMING_PAT = re.compile(r"within (?:the next )?(\d+\s*(?:minutes?|hours?|business days?))", re.I)

# Mood, severity and sentiment words. A generated persona puts the mood in the same comma-
# separated string as the name ("Lily Chen, student, frustrated, ..."), and the name is read
# out of slot 0, so any mood word missing from here becomes the caller's name. Shared by
# INVALID_NAME_WORDS and FALSE_CUSTOMER_NAME_TOKENS below so the two cannot drift apart; the
# browser keeps the same list in static/voice_training.js.
MOOD_AND_SEVERITY_WORDS = frozenset({
    "urgent", "critical", "priority", "important", "immediate", "asap", "severe",
    "frustrated", "angry", "impatient", "concerned", "upset", "annoyed",
    "disappointed", "confused", "worried", "stressed", "escalated",
    "ridiculous", "unacceptable", "absurd", "crazy", "insane", "horrible",
    "terrible", "awful", "outrageous", "unbelievable", "frustrating",
    "annoying", "pathetic", "ludicrous", "nonsense", "enough", "finally",
    "irate", "livid", "furious", "fuming", "hostile", "desperate", "distraught",
    "unhappy", "dissatisfied", "unsatisfied", "displeased", "exasperated",
    "indignant", "aggravated", "irritated", "agitated", "anxious", "panicked",
    "overwhelmed", "inconvenienced", "mad", "tense", "neutral", "calm", "polite",
    "satisfied", "pleased", "fed",
})

INVALID_NAME_WORDS = frozenset({
    "from", "the", "a", "an", "your", "our", "this", "that", "sure", "yes",
    "okay", "alright", "please", "well", "oh", "um", "so", "and", "for",
    "quite", "very", "really", "having", "calling", "wondering", "experiencing",
    "not", "here", "there", "still", "already", "also", "just", "about", "because",
    "great", "good", "fine", "sorry", "happy", "glad", "able", "unable",
    "trying", "looking", "getting", "going", "being", "doing",
    "installing", "updating", "restarting", "resetting", "connecting", "checking",
    "waiting", "holding", "done", "forgot", "reconnected",
}) | MOOD_AND_SEVERITY_WORDS

# Adjective-like endings that are almost never first names in support calls. "ly" and "ed" are
# deliberately absent: they rejected ordinary names (Lily, Emily, Kelly, Molly, Jared, Fred),
# and the mood and verb forms they were meant to catch -- frustrated, annoyed, escalated,
# finally, really -- are listed by name in MOOD_AND_SEVERITY_WORDS and INVALID_NAME_WORDS.
_NON_NAME_SUFFIXES = ("ing", "ous", "ible", "ical", "ful", "less", "ish", "ive")

AGENT_TOPIC_GROUPS = (
    ("model", "number", "printer", "serial"),
    ("network", "settings", "wi-fi", "wifi", "connect", "found"),
    ("restart", "reboot", "power", "turn off", "both"),
    ("driver", "update", "install", "installing"),
    ("reset", "factory"),
    ("range", "signal", "router"),
    ("forget", "reconnect", "reconnected"),
    ("summarize", "recap", "summary"),
    ("verify", "confirm", "name", "email", "account", "pet", "maiden", "security", "identity"),
    ("escalat", "specialist", "billing team", "senior", "ticket", "handoff"),
    ("charge", "billing", "dispute", "duplicate", "refund", "pull up", "review"),
    ("sorry", "help", "assist", "today"),
)

AGENT_STEP_CUSTOMER_PAIRS = (
    (("network setting", "network settings", "wi-fi setting"), ("network", "no network", "settings", "found", "wifi", "shows")),
    (("restart", "reboot", "power cycle", "both devices"), ("restart", "restarted", "rebooted")),
    (("driver", "update available", "printer update"), ("install", "update", "driver", "installing")),
    (("after the update", "connecting again", "try connect", "connect again"), ("still not", "not connect", "what should")),
    (("range", "wi-fi signal", "signal"), ("router", "next to")),
    (("forget", "reconnect", "reconnected"), ("forgot", "reconnect", "no luck", "still")),
    (("factory reset", "reset on the", "reset on your"), ("reset", "factory", "reconnect", "trying")),
    (("to summarize", "summarize", "recap", "walked through"), ("sounds good", "thank", "explaining", "appreciate")),
    (("anything else", "something else", "help you with"), ("no", "that's all", "nothing else", "everything")),
    (("thank you for calling", "great day", "goodbye"), ("thank", "you too", "great day", "bye")),
    (("escalat", "billing team", "specialist", "will review", "handed off", "discrepancy"), ("appreciate", "thank", "great", "finally", "that's all", "no, that's")),
    (("pull up", "review the", "billing system", "look up", "check that charge"), ("charged", "twice", "charge", "$", "same service")),
    (("sorry about that", "i'm sorry about", "sorry about the charge"), ("alright", "thank", "appreciate")),
    (("found a service", "activation fee", "service activation", "service fee from"), ("didn't request", "no new service", "we didn't", "not request")),
    (("appears to be an error", "this is an error", "billing error"), ("great", "when will", "updated", "when should")),
    (("adjustment will reflect", "next bill", "reflect in your", "few business days"), ("thank", "resolving", "appreciate", "confirmation")),
    (("billing history", "pull up your billing"), ("match", "statement", "didn't request", "when should", "when will", "thank")),
)

MID_CALL_ACK_WORDS = (
    "thank", "thanks", "urgent", "great", "appreciate", "correct", "trying",
    "done", "okay", "alright", "sure", "yes", "hold", "waiting", "update",
    "sounds good", "that works", "got it", "perfect", "wonderful",
)

GENERIC_SUMMARY_MARKERS = (
    "your request is being processed",
    "helped you,",
    "verified your details, completed the required steps",
    "alex from",
    "we helped you",
)

APOLOGY_MARKERS = (
    "sorry to hear", "i'm sorry", "i am sorry", "apologize", "that must be",
    "how frustrating", "understand your frustration", "sorry you're", "sorry you are",
    "i understand how", "that sounds frustrating",
)

PROBLEM_MARKERS = (
    "problem", "issue", "error", "failed", "cannot", "can't", "won't", "not working",
    "offline", "charged", "charge", "charges", "fraudulent", "fraud", "invoice",
    "refund", "activate", "activation", "invalid", "broken",
    "stuck", "down", "outage", "glitch", "frustrated", "angry", "complaint",
    "duplicate", "billing", "credentials", "account number", "won't work",
    "doesn't work", "need help with", "still not resolved", "can't figure",
    "sync", "syncing", "email", "emails", "mail", "outlook", "inbox",
    "disconnect", "disconnected", "login", "log in", "password",
    "unauthorized", "unexpected", "disputed", "dispute",
)

VERIFY_ASK_MARKERS = (
    "may i have", "can i have", "could i get", "could you provide", "please provide",
    "verify", "confirm your", "what is your", "email address", "work email",
    "account number", "employee id", "user id", "last four", "date of birth",
    "may i get", "can you confirm", "do you have", "tell me your",
)

# Do NOT include bare "from " — it false-matches date phrases like "from October 19th".
INTRO_ONLY_HINTS = (
    "this is", "my name is", "calling from", "i'm calling", "i am calling",
    "hi ", "hello ", "good morning", "good afternoon",
)

CLOSING_KEYWORDS = (
    "thank", "anything else", "great day", "goodbye", "wrap up", "summarize",
    "to summarize", "before we end", "appreciate", "that's all", "recap",
)

MID_CALL_FILLER = [
    ("customer", "Okay, what should I do next?"),
    ("trainee", "One moment — I'm checking that now."),
    ("customer", "Sure, I'll hold."),
    ("trainee", "Thanks. I see the issue on my side."),
    ("customer", "Is there any update yet?"),
    ("trainee", "Yes — I'm applying the fix now."),
    ("customer", "Great, keep me posted."),
    ("trainee", "Will do. I'll confirm once it's done."),
]

ESCALATION_FILLER = [
    ("customer", "How long will this take? We need this resolved soon."),
    ("trainee", "I'll prioritize your case and review the details right now."),
    ("customer", "Please make sure someone follows up with me directly."),
    ("trainee", "Absolutely — I'll note that and ensure you're contacted promptly."),
    ("customer", "Alright. I'll wait for the callback."),
    ("trainee", "Thank you for your patience while we get this escalated."),
]

BILLING_FILLER = [
    ("customer", "Can you confirm the amount that was charged?"),
    ("trainee", "Let me pull up your billing history right now."),
    ("customer", "Yes — that matches what I see on my statement."),
    ("trainee", "I'll document this and process the correction on my end."),
    ("customer", "Good. When should I see the update?"),
    ("trainee", "You should see it reflected within a few business days."),
    ("customer", "Will I get an email confirmation?"),
    ("trainee", "Yes — you'll receive a confirmation email once it's processed."),
]

GENERIC_FILLER_PHRASES = frozenset(
    (text or "").lower().strip()
    for pairs in (BILLING_FILLER, MID_CALL_FILLER, ESCALATION_FILLER)
    for _, text in pairs
)

GENERIC_FILLER_MARKERS = (
    "matches what i see on my statement",
    "will i get an email confirmation",
    "when should i see the update",
    "good. when should i see the update",
    "can you confirm the amount that was charged",
    "pull up your billing history right now",
    "document this and process the correction",
    "reflected within a few business days",
    "confirmation email once it's processed",
    "how long will this take? we need this resolved",
    "please make sure someone follows up with me directly",
    "is there any update yet",
    "great, keep me posted",
    "will do. i'll confirm once it's done",
    "i'm applying the fix now",
    "sure, i'll hold",
)


def is_generic_filler_line(text: str) -> bool:
    """True for injected padding lines — not part of the scenario narrative timeline."""
    norm = (text or "").lower().strip()
    if not norm:
        return False
    if norm in GENERIC_FILLER_PHRASES:
        return True
    return any(marker in norm for marker in GENERIC_FILLER_MARKERS)


def strip_injected_filler_turns(script: list[dict]) -> list[dict]:
    """Remove generic category filler turns from stored scripts (legacy padding)."""
    if not script:
        return script
    out: list[dict] = []
    for turn in script:
        speaker = turn.get("speaker") if isinstance(turn, dict) else getattr(turn, "speaker", None)
        text = turn.get("text") if isinstance(turn, dict) else getattr(turn, "text", "")
        if not speaker or not (text or "").strip():
            continue
        if is_generic_filler_line(text):
            continue
        out.append({"speaker": speaker, "text": (text or "").strip()})
    return out


def dedupe_repeated_sentences(text: str) -> str:
    """Remove duplicated sentences (e.g. same empathy line twice in one turn)."""
    line = (text or "").strip()
    if not line:
        return line
    parts = _split_sentences(line)
    kept: list[str] = []
    for part in parts:
        norm = part.lower().strip()
        if norm and not any(norm == k.lower() for k in kept):
            kept.append(part.strip())
    return " ".join(kept) if kept else line


def _text_overlaps_empathy_prefix(core: str, prefix: str) -> bool:
    c = (core or "").lower().strip()
    p = (prefix or "").lower().strip()
    if not c or not p:
        return False
    if c.startswith(p[: max(12, min(len(p), 28))].rstrip()):
        return True
    c_first = _split_sentences(core)[0].lower() if core else ""
    p_first = _split_sentences(prefix)[0].lower() if prefix else ""
    return bool(c_first and p_first and (c_first == p_first or c_first in p_first or p_first in c_first))


def _is_summary_turn(text: str) -> bool:
    lower = (text or "").lower()
    return "to summarize" in lower or "let me summarize" in lower


def _is_closing_customer_turn(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "that sounds good",
            "that sounds great",
            "thank you for explaining",
            "no, that's everything",
            "no, that's all",
            "appreciate your help today",
        )
    )


def _is_closing_agent_turn(text: str) -> bool:
    lower = (text or "").lower()
    if _is_summary_turn(text):
        return False
    return any(
        w in lower
        for w in (
            "anything else",
            "thank you for calling",
            "wonderful day",
            "great day",
            "have a good",
            "you're welcome",
        )
    )


def _is_premature_customer_closing(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "that's all for now",
            "no, that's all",
            "that's everything",
            "no, that is all",
            "nothing else for now",
        )
    )


def _count_summary_turns(script: list[dict]) -> int:
    return sum(
        1
        for turn in script
        if turn.get("speaker") == "trainee" and _is_summary_turn(turn.get("text", ""))
    )


def _find_closing_start_index(script: list[dict]) -> int | None:
    for i, turn in enumerate(script):
        text = turn.get("text", "")
        speaker = turn.get("speaker", "")
        if speaker == "trainee" and _is_summary_turn(text):
            return i
        if i >= 4 and (
            (speaker == "customer" and _is_closing_customer_turn(text))
            or (speaker == "trainee" and _is_closing_agent_turn(text))
        ):
            return i
    return None


def _strip_closing_segment(script: list[dict]) -> list[dict]:
    """Keep only the main-call body — drop any summary/closing block (mid-script or tail)."""
    idx = _find_closing_start_index(script)
    body = list(script[:idx] if idx is not None else script)
    while body and body[-1]["speaker"] == "customer" and _is_premature_customer_closing(
        body[-1].get("text", "")
    ):
        body.pop()
    return body


def _vary_agent_line(
    text: str,
    last_customer: str,
    description: str,
    recent: list[str],
) -> str:
    lower = (text or "").lower().strip()
    recent_set = {r.lower().strip() for r in recent}
    alts: list[str] = []
    if "pull up that charge" in lower or "pull up your billing" in lower:
        alts = [
            "One moment — I'm comparing the charges on your account now.",
            "I'll document this and process the correction on my end.",
            "Good question — let me verify the billing history for you.",
            "You're welcome. I'm continuing to work on this for you.",
        ]
    elif "escalat" in lower:
        alts = [
            "I'll prioritize your case and review the details right now.",
            "Absolutely — I'll note that and ensure you're contacted promptly.",
        ]
    else:
        candidate = build_mid_call_agent_response(last_customer, description)
        if candidate.lower() not in recent_set and candidate.lower() != lower:
            return candidate
        alts = [
            "Thanks for the update. Let me take the next step for you.",
            "One moment — I'm checking that now.",
            "I'll confirm the details and follow up with you shortly.",
        ]
    for alt in alts:
        if alt.lower() not in recent_set and alt.lower() != lower:
            return alt
    return text


def _vary_repeated_trainee_turns(
    script: list[dict],
    description: str = "",
) -> list[dict]:
    out: list[dict] = []
    recent_trainee: list[str] = []
    last_customer = ""
    for turn in script:
        speaker = turn.get("speaker", "customer")
        text = repair_dangling_text(turn.get("text", ""))
        if speaker == "customer":
            last_customer = text
            out.append({"speaker": speaker, "text": shorten_speech_text(text, speaker)})
            continue
        if speaker == "trainee" and not _is_summary_turn(text):
            norm = text.lower().strip()
            if norm and norm in {r.lower().strip() for r in recent_trainee[-3:]}:
                text = _vary_agent_line(text, last_customer, description, recent_trainee)
            recent_trainee.append(text)
        out.append({"speaker": speaker, "text": shorten_speech_text(dedupe_repeated_sentences(text), speaker)})
    return out


def dedupe_script_lines(script: list[dict], description: str = "") -> list[dict]:
    out: list[dict] = []
    for turn in script:
        speaker = turn.get("speaker", "customer")
        text = dedupe_repeated_sentences(repair_dangling_text(turn.get("text", "")))
        if not text:
            continue
        norm = text.lower().strip()
        if out and out[-1]["speaker"] == speaker and out[-1]["text"].lower().strip() == norm:
            continue
        out.append({"speaker": speaker, "text": shorten_speech_text(text, speaker)})
    return _vary_repeated_trainee_turns(out, description)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _word_count(text: str) -> int:
    return len((text or "").split())


def _is_dangling(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if _ends_with_dangling_tail(t):
        return True
    if t.endswith(",") or t.endswith(";"):
        return True
    if len(t.split()) <= 2 and t[-1] not in ".!?":
        return True
    return False


def repair_dangling_text(text: str) -> str:
    line = (text or "").strip()
    if not line or not _is_dangling(line):
        return line
    sentences = _split_sentences(line)
    if len(sentences) > 1:
        trimmed = " ".join(sentences[:-1]).strip()
        if trimmed and not _is_dangling(trimmed):
            return trimmed
    words = line.split()
    while len(words) > 3 and _is_dangling(" ".join(words)):
        words = words[:-1]
    repaired = " ".join(words).strip()
    if repaired and repaired[-1] not in ".!?":
        repaired += "."
    if _is_dangling(repaired):
        return line.rsplit(".", 1)[0].strip() + "." if "." in line else line
    return repaired


def _strip_and_numeric_ids(text: str) -> str:
    """Remove `` and 123`` / `` and 12345`` tails (former ``\\s+and\\s+\\d{3,6}\\b``)."""
    if not text:
        return text
    lower = text.lower()
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            if lower.startswith("and", j):
                after_and = j + 3
                if after_and >= n or not _is_word_char(lower[after_and]):
                    k = after_and
                    if k < n and text[k].isspace():
                        while k < n and text[k].isspace():
                            k += 1
                        if k < n and text[k].isdigit():
                            d0 = k
                            while k < n and text[k].isdigit():
                                k += 1
                            digs = k - d0
                            if 3 <= digs <= 6 and (k >= n or not _is_word_char(text[k])):
                                i = k
                                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _strip_trailing_dash_amount(text: str) -> str:
    """
    Replace trailing `` — $12`` / `` - $12.00.`` with ``.``
    Linear equivalent of ``\\s*[—\\-]\\s*\\$\\s*\\d+(?:\\.\\d+)?\\.?\\s*$``.
    """
    if not text:
        return text
    t = text.rstrip()
    i = len(t)
    if i > 0 and t[i - 1] == ".":
        i -= 1
    if i == 0 or not t[i - 1].isdigit():
        return text
    while i > 0 and t[i - 1].isdigit():
        i -= 1
    if i > 0 and t[i - 1] == ".":
        j = i - 1
        if j == 0 or not t[j - 1].isdigit():
            return text
        while j > 0 and t[j - 1].isdigit():
            j -= 1
        i = j
    while i > 0 and t[i - 1].isspace():
        i -= 1
    if i == 0 or t[i - 1] != "$":
        return text
    i -= 1
    while i > 0 and t[i - 1].isspace():
        i -= 1
    if i == 0 or t[i - 1] not in "—-":
        return text
    i -= 1
    while i > 0 and t[i - 1].isspace():
        i -= 1
    return (t[:i] + ".").strip()


def _ends_with_from_clause(text: str) -> bool:
    """True for ``... from`` / ``... from.`` at EOS (former ``\\bfrom\\s*\\.?\\s*$``)."""
    t = (text or "").rstrip()
    if t.endswith("."):
        t = t[:-1].rstrip()
    low = t.lower()
    if not low.endswith("from"):
        return False
    if len(low) == 4:
        return True
    prev = low[-5]
    return not (prev.isalnum() or prev == "_")


def customer_stated_problem(text: str) -> bool:
    lower = (text or "").lower()
    if any(m in lower for m in PROBLEM_MARKERS):
        return True
    if re.search(r"\d{6,}", lower):
        return True
    if "account number" in lower and re.search(r"\d{4,}", lower):
        return True
    # Money or calendar dates usually mean the customer is describing the issue.
    if re.search(r"\$\s*\d", lower):
        return True
    if re.search(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\b\s+\d{1,2}(?:st|nd|rd|th)?\b",
        lower,
    ):
        return True
    return False


def is_intro_only_customer(text: str) -> bool:
    """True when the customer has only introduced themselves — no issue stated yet."""
    lower = (text or "").lower().strip()
    if not lower or customer_stated_problem(text):
        return False
    if any(w in lower for w in MID_CALL_ACK_WORDS):
        return False
    if re.search(r"\d{4,}", lower):
        return False
    if re.search(r"\$\s*\d", lower):
        return False
    word_count = len(lower.split())
    if word_count <= 16 and any(h in lower for h in INTRO_ONLY_HINTS):
        if re.search(r"(?:this is|my name is|i'm|i am)\s+([a-z]+)", lower):
            maybe_name = re.search(r"(?:this is|my name is|i'm|i am)\s+([a-z]+)", lower).group(1)
            if maybe_name in INVALID_NAME_WORDS:
                return False
        return True
    return False


def is_recycled_customer_opening(text: str) -> bool:
    """True for first-turn style openings, including short 'Hi… this is… I have a…' pads."""
    if is_intro_only_customer(text):
        return True
    lower = (text or "").lower().strip()
    if not lower or len(lower.split()) > 22:
        return False
    if re.search(r"\d{4,}", lower) or re.search(r"\$\s*\d", lower):
        return False
    if any(w in lower for w in MID_CALL_ACK_WORDS):
        return False
    # Legacy padding recycles name intros that also name the dispute category.
    return bool(
        re.search(r"\b(?:hi|hello)\b", lower)
        and re.search(r"\b(?:this is|my name is|i'm|i am)\b", lower)
    )


def agent_asks_for_issue(agent_text: str) -> bool:
    """True when the agent is asking the customer to state their problem."""
    lower = (agent_text or "").lower()
    return any(
        p in lower
        for p in (
            "what seems to be",
            "what's the issue",
            "what is the issue",
            "what's the problem",
            "what is the problem",
            "what brings you",
            "what's going on",
            "what seems to be the problem",
            "tell me about the issue",
            "tell me what's going on",
            "how can i help you today",
            "how may i help you today",
            "how can i assist you today",
            "what can i do for you",
        )
    )


def agent_line_fits_intro_response(agent_text: str) -> bool:
    """True when the agent appropriately asks how to help after a customer intro."""
    lower = (agent_text or "").lower()
    if any(
        p in lower
        for p in (
            "how can i help",
            "how may i",
            "what issue",
            "what problem",
            "may i have",
            "could you tell me",
            "what can i help",
            "how can i assist",
            "what brings you",
        )
    ):
        return True
    return "?" in (agent_text or "")


def is_premature_apology(agent_text: str) -> bool:
    lower = (agent_text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return any(m in lower for m in APOLOGY_MARKERS)


def should_replace_after_intro(agent_text: str, customer_text: str) -> bool:
    """Replace agent lines that apologize or empathize before the customer states a problem."""
    if not is_intro_only_customer(customer_text):
        return False
    if is_premature_apology(agent_text):
        return True
    lower = (agent_text or "").lower()
    if any(p in lower for p in ("do my best", "i understand", "frustrating", "inconvenience", "glad to help")):
        return True
    return not agent_line_fits_intro_response(agent_text)


def is_welcome_opening(agent_text: str) -> bool:
    """True when the agent opens with a professional greeting — no apology or assumed problem."""
    lower = (agent_text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    if not lower.strip():
        return False
    if is_premature_apology(agent_text):
        return False
    return any(
        p in lower
        for p in (
            "thank you for calling",
            "thanks for calling",
            "thank you for contacting",
            "how may i help",
            "how can i help you today",
            "how may i assist",
            "how can i assist",
            "my name is",
            "what can i do for you",
        )
    )


def is_premature_opening(agent_text: str) -> bool:
    """True when the first agent turn apologizes or assumes the customer's problem."""
    if not (agent_text or "").strip():
        return True
    if is_welcome_opening(agent_text):
        return False
    if is_premature_apology(agent_text):
        return True
    lower = agent_text.lower()
    if any(
        p in lower
        for p in (
            "trouble you're",
            "about the trouble",
            "hear about the",
            "your frustration",
            "inconvenience",
            "experiencing",
        )
    ):
        return True
    if re.search(r"\b(?:mr|mrs|ms|miss)\.\s+[A-Z]", agent_text):
        return True
    return False


def _extract_agent_name_from_script(script: list[Any] | None) -> str | None:
    """Return agent name only if a trainee turn actually introduces it; else None."""
    if not script:
        return None
    customer = extract_customer_name_from_script(script) or ""
    customer_first = customer.split()[0].lower() if customer else ""
    for turn in script:
        speaker = turn.get("speaker") if isinstance(turn, dict) else getattr(turn, "speaker", "")
        text = turn.get("text", "") if isinstance(turn, dict) else getattr(turn, "text", "")
        if speaker != "trainee" or not text:
            continue
        match = re.search(
            r"(?:my name is|i'?m|i am|this is)\s+([A-Z][a-z]+)"
            r"(?:\s+(?:from|with|at|speaking)\b|,|\.|!|\s|$)",
            text,
            re.I,
        )
        if not match:
            continue
        name = match.group(1)
        if name.lower() in INVALID_NAME_WORDS:
            continue
        if customer_first and name.lower() == customer_first:
            continue
        return name
    return None


def _strip_unintroduced_agent_address(
    text: str,
    *,
    known_agent_name: str | None = None,
) -> str:
    """Remove customer addressing of an agent name that was never introduced."""
    line = (text or "").strip()
    if not line:
        return line
    keep = (known_agent_name or "").strip().lower()

    def _should_strip_directed_name(name: str) -> bool:
        nl = (name or "").lower()
        if not nl or nl in INVALID_NAME_WORDS:
            return False
        if keep and nl == keep:
            return False
        return True

    # "That sounds great, Alex — thank you..."
    def _tsg(match: re.Match) -> str:
        return match.group(1) if _should_strip_directed_name(match.group(2)) else match.group(0)

    line = re.sub(
        r"\b(that sounds great),?\s+([A-Z][a-z]+)\b",
        _tsg,
        line,
        flags=re.I,
    )

    # "Thank you, Alex" / "Thanks, Alex" (not in "this is ..." self-intro)
    if not re.search(r"\b(?:this is|my name is)\b", line, re.I):
        def _thanks(match: re.Match) -> str:
            return match.group(1) if _should_strip_directed_name(match.group(2)) else match.group(0)

        line = re.sub(
            r"\b(thanks|thank you),?\s+([A-Z][a-z]+)\b",
            _thanks,
            line,
            flags=re.I,
        )

    # "Hi Alex," / "Hello Alex," at start — keep only if agent introduced that name
    def _hi(match: re.Match) -> str:
        name = match.group(2)
        if keep and name.lower() == keep:
            return match.group(0)
        if _should_strip_directed_name(name):
            return f"{match.group(1)}, "
        return match.group(0)

    line = re.sub(
        r"^(hi|hello|hey),?\s+([A-Z][a-z]+)([,.!]?\s*)",
        _hi,
        line,
        flags=re.I,
    )
    return re.sub(r"\s{2,}", " ", line).strip(" ,")


def build_agent_opening_greeting(
    description: str = "",
    persona: str = "",
    script: list[Any] | None = None,
) -> str:
    """Standard agent welcome — no apology, no assumed customer problem."""
    # When we synthesize an opening, include a real intro name so later customer lines may use it.
    agent_name = _extract_agent_name_from_script(script) or "Alex"
    return f"Hello, thank you for calling support. I'm {agent_name}, how may I help you today?"


def sanitize_agent_opening(
    line: str,
    description: str = "",
    persona: str = "",
    script: list[Any] | None = None,
) -> str:
    if line and is_welcome_opening(line) and not is_premature_opening(line):
        return shorten_speech_text(line, "trainee")
    return shorten_speech_text(
        build_agent_opening_greeting(description, persona, script), "trainee"
    )


def build_customer_problem_statement(
    description: str,
    persona: str = "",
    intro_line: str = "",
) -> str:
    """Customer intro plus problem — used when the script only has a name/greeting."""
    name = _parse_persona_name(persona) or ""
    if intro_line:
        name = (
            extract_customer_name_from_script([{"speaker": "customer", "text": intro_line}])
            or name
        )
    problem = (description or "").strip()
    if len(problem) > 140:
        problem = problem[:137].rsplit(" ", 1)[0] + "..."

    base = (intro_line or "").strip().rstrip(".")
    if base and customer_stated_problem(base):
        return shorten_speech_text(base, "customer")
    if base:
        if name and name.lower() not in base.lower():
            base = f"Hi, this is {name}. {base}"
        return shorten_speech_text(f"{base}. {problem}", "customer")
    if name and problem:
        return shorten_speech_text(f"Hi, this is {name}. {problem}", "customer")
    if problem:
        return shorten_speech_text(f"Hi. {problem}", "customer")
    return shorten_speech_text("Hi, I need some help with my account.", "customer")


def build_gather_info_response(
    customer_text: str,
    description: str = "",
    known_name: str | None = None,
) -> str:
    """Agent line after customer intro — ask how to help, do not apologize yet."""
    name = known_name or extract_customer_name_from_script(
        [{"speaker": "customer", "text": customer_text}]
    )
    first = (name or "").split()[0] if name and _valid_customer_name(name) else ""
    desc_lower = (description or "").lower()
    if "activat" in desc_lower:
        topic = "your account activation"
    elif any(w in desc_lower for w in ("billing", "charge", "refund", "invoice")):
        topic = "your billing concern"
    elif any(w in desc_lower for w in ("escalat", "glitch", "outage", "offline")):
        topic = "the issue you're experiencing"
    elif any(w in desc_lower for w in ("printer", "connectivity", "network", "router", "wi-fi", "wifi")):
        topic = "your printer connection issue"
    elif any(w in desc_lower for w in ("email", "sync", "mail", "outlook", "exchange")):
        topic = "your email sync issue"
    elif any(w in desc_lower for w in ("renewal", "subscription")):
        topic = "your renewal"
    else:
        topic = "your request today"
    if first:
        return f"Thank you, {first}. How can I help you with {topic}?"
    return f"Thank you for calling. How can I help you with {topic} today?"


def is_misplaced_gather_info(agent_text: str) -> bool:
    """Agent re-asks how to help mid-call after the issue is already underway."""
    lower = (agent_text or "").lower()
    return any(
        p in lower
        for p in (
            "how can i help you with",
            "how may i help you",
            "what brings you",
            "how can i assist you with",
        )
    )


def build_mid_call_agent_response(last_customer: str, description: str = "") -> str:
    """Contextual agent line after mid-call customer updates — not a re-introduction."""
    lower = (last_customer or "").lower()
    desc_lower = (description or "").lower()
    if EMAIL_RE.search(last_customer or "") and any(
        w in desc_lower for w in ("email sync", "syncing", "outlook", "exchange", "mailbox")
    ):
        return "I'm sorry about the email sync issue. May I have your work email address to check your account?"
    if any(w in lower for w in ("way too long", "too long", "unacceptable", "ridiculous", "frustrat")):
        return "I know that timeline is frustrating. Let me see what we can do to speed this up."
    if any(w in lower for w in ("expedite", "how long", "when will", "notified", "outcome")):
        return "Good question. I'll confirm the timeline and how we'll notify you."
    if any(w in lower for w in ("didn't authorize", "never agreed", "unauthorized", "unexpected charge")):
        return "Let me review that charge in our billing system now."
    if any(w in lower for w in ("$", "charged", "charge")) or "disput" in desc_lower or "billing" in desc_lower:
        return "Let me pull up that charge and review the details."
    if any(w in lower for w in ("urgent", "asap", "right away", "immediately")):
        return "I'll prioritize your case right now."
    if any(w in lower for w in ("thank", "appreciate", "great")):
        return "You're welcome. I'm continuing to work on this for you."
    if any(w in lower for w in ("account", "card", "ends in", "digits")):
        return "Thanks. Let me review those details in our system now."
    if any(w in lower for w in ("error", "credentials", "invalid", "account number", "activation")):
        return "Thank you for that information. Let me check the details in our system."
    if any(w in lower for w in ("trying", "done", "works", "logged in", "correct")):
        if "activat" in desc_lower:
            return "Perfect. Let me confirm everything is working on our end."
        return "Great. Let me verify that the fix is complete."
    if "?" in last_customer:
        return "Good question. Let me check that and get back to you shortly."
    return "Thank you for the update. Let me take the next step for you."


def agent_requested_verification_info(agent_text: str) -> bool:
    """True when the agent explicitly asked for credentials or verification details."""
    lower = (agent_text or "").lower()
    if any(m in lower for m in VERIFY_ASK_MARKERS):
        return True
    if "?" in lower and any(
        w in lower
        for w in ("email", "account", "number", "verify", "confirm", "employee", "id", "password")
    ):
        return True
    return False


def customer_discloses_verification_data(text: str) -> bool:
    """True when the customer volunteers email, account numbers, or unexplained digits."""
    if not (text or "").strip():
        return False
    lower = text.lower()
    if EMAIL_RE.search(text):
        return True
    if any(w in lower for w in ("account number", "employee id", "user id", "last four", "ends in")):
        return True
    if re.search(r"\b(?:and\s+)?\d{3,6}\b", lower) and any(
        w in lower for w in ("sure", "it's", "it is", "yes", "@", "email")
    ):
        return True
    return False


def build_verification_ask_before_disclosure(
    customer_context: str,
    disclosure: str,
    description: str = "",
    known_name: str | None = None,
) -> str:
    """Agent line inserted when the customer volunteered credentials without being asked."""
    first = (known_name or "").split()[0] if known_name else ""
    name_bit = f", {first}" if first else ""
    disp_lower = (disclosure or "").lower()
    desc_lower = (description or "").lower()
    if EMAIL_RE.search(disclosure):
        if any(w in desc_lower for w in ("email sync", "syncing", "outlook", "exchange", "mailbox")):
            return (
                f"I'm sorry about the email sync issue{name_bit}. "
                f"May I have your work email address to check your account?"
            )
        return f"Thank you{name_bit}. May I have your email address to verify your account?"
    if re.search(r"\b\d{4,}\b", disclosure):
        return f"Thank you{name_bit}. May I have your account number please?"
    return build_mid_call_agent_response(customer_context, description)


def normalize_customer_disclosure(customer_text: str, agent_ask: str) -> str:
    """Keep customer verification replies natural — only share what the agent asked for."""
    text = (customer_text or "").strip()
    agent_lower = (agent_ask or "").lower()
    email_match = EMAIL_RE.search(text)
    if email_match:
        if any(w in agent_lower for w in ("email", "e-mail", "@", "work email")):
            reply = f"Sure, it's {email_match.group(0)}."
            if any(w in agent_lower for w in ("employee id", "user id", "id number", "pin")):
                id_match = re.search(r"\b(?:and\s+)?(?:my\s+)?(?:employee\s+id\s+is\s+)?(\d{3,6})\b", text, re.I)
                if id_match:
                    return f"{reply.rstrip('.')}, and my employee ID is {id_match.group(1)}."
            return reply
        return shorten_speech_text(
            "I can provide that once you let me know what details you need.", "customer"
        )
    id_match = re.search(r"\b(\d{4,})\b", text)
    if id_match and any(w in agent_lower for w in ("account number", "employee id", "user id", "verify")):
        label = "account number" if "account" in agent_lower else "employee ID"
        return f"Sure, my {label} is {id_match.group(1)}."
    cleaned = _strip_and_numeric_ids(text).strip()
    return shorten_speech_text(cleaned or text, "customer")


EMPATHY_OPENERS = (
    "i completely understand",
    "i totally understand",
    "i certainly understand",
    "i understand",
    "i'm sorry",
    "i am sorry",
    "i'm so sorry",
    "i apologize",
    "i apologise",
    "i hear your",
    "i hear you",
    "i appreciate",
)

# A conjunction joining the empathy clause to the real content: "I understand, and I'll fix it."
_EMPATHY_TAIL_CONJUNCTION = re.compile(r"^(?:and|but|so|however)\s+", re.I)

# Where the real content starts when a generated line runs empathy straight into it with no
# punctuation ("I understand John please provide your account number"). Only openers that
# cannot belong to the empathy clause itself, so "I understand how frustrating this is" is
# left whole instead of being cut down to "how frustrating this is".
_CONTENT_CLAUSE_START = re.compile(
    r"\b(?:please|let me|let's|thank you|thanks|here's|here is|may i|can you|could you"
    r"|would you|i'll|i will|i can|i'd|i would|i'm going to|i am going to"
    r"|we'll|we will|we can|we're|we are)\b",
    re.I,
)


def _empathy_clause_remainder(sentence: str) -> Optional[str]:
    """
    None when the sentence does not open with empathy. Otherwise the substantive remainder of
    that sentence — "" when the whole sentence is empathy and the caller may drop it.

    A comma clause carrying real content is kept ("I understand, and I'll fix it now." ->
    "I'll fix it now."); a trailing address ("I understand your urgency, Lily.") is not
    content, so the sentence goes as a whole.
    """
    stripped = (sentence or "").strip()
    if not stripped.lower().startswith(EMPATHY_OPENERS):
        return None
    _, _, tail = stripped.partition(",")
    tail = _EMPATHY_TAIL_CONJUNCTION.sub("", tail.strip()).strip()
    if _word_count(tail.rstrip(".!?,")) >= 3:
        return tail[0].upper() + tail[1:]
    # No usable comma clause: an unpunctuated line may still carry its content inline.
    match = _CONTENT_CLAUSE_START.search(stripped)
    if match and match.start() > 0:
        inline = stripped[match.start():].strip()
        if _word_count(inline.rstrip(".!?,")) >= 2:
            return inline[0].upper() + inline[1:]
    return ""


def _strip_redundant_empathy_prefix(text: str) -> str:
    """
    Drop a leading empathy sentence so a line does not repeat empathy the previous turn gave.

    Whole sentences only. Trimming a character prefix instead used to cut one word past the
    verb, so "I understand your concern. We're working on it." lost "I understand your" and
    was spoken as "concern. We're working on it." — a turn starting mid-sentence. When empathy
    is all the sentence says, the line is returned unchanged rather than emptied.
    """
    line = (text or "").strip()
    while line:
        sentences = _split_sentences(line)
        if not sentences:
            break
        remainder = _empathy_clause_remainder(sentences[0])
        if remainder is None:
            break
        candidate = " ".join([remainder, *sentences[1:]]).strip()
        if not candidate:
            break
        line = candidate
    return line


# Debris left by the old character-prefix empathy strip. These are not customer names — they
# are the noun that followed "I understand your …" before the cut ate the opener.
_ORPHAN_EMPATHY_HEAD = re.compile(
    r"^(?P<head>"
    r"concern|frustration|urgency|disappointment|inconvenience|situation|issue|"
    r"frustrating(?:\s+this\s+is)?|"
    r"about\s+the\s+(?:delay|wait|wait\s*time)|"
    r"that"
    r")"
    r"(?:\s*[.!,])?\s*"
    r"(?P<rest>.*)$",
    re.I | re.S,
)


def repair_mid_sentence_agent_line(text: str, known_name: str | None = None) -> str:
    """
    Restore agent lines that start mid-sentence after a bad empathy strip.

    Example: "concern. We're working on it as quickly as possible." becomes
    "I understand your concern, Lily. We're working on it as quickly as possible."
    when the caller's name is known. "concern" is never treated as a name.
    """
    line = (text or "").strip()
    if not line:
        return line
    # Already a normal sentence (capital start) and not an orphan head — leave alone.
    if line[0].isupper() and not _ORPHAN_EMPATHY_HEAD.match(line):
        return line
    match = _ORPHAN_EMPATHY_HEAD.match(line)
    if not match:
        # Lowercase start with no known orphan head: capitalize only, do not invent empathy.
        if line[0].islower():
            return line[0].upper() + line[1:]
        return line

    head = (match.group("head") or "").strip()
    rest = (match.group("rest") or "").strip()
    name = _safe_person_token(known_name) or ""
    # Never put a mood/orphan word back as the caller's name.
    if name and name.lower() in MOOD_AND_SEVERITY_WORDS:
        name = ""
    if name and name.lower() in INVALID_NAME_WORDS:
        name = ""
    name_bit = f", {name}" if name else ""

    head_l = head.lower()
    if head_l.startswith("frustrating"):
        restored = f"I understand how {head_l}{name_bit}."
    elif head_l.startswith("about "):
        restored = f"I understand {head_l}{name_bit}."
    elif head_l == "that":
        restored = f"I understand that{name_bit}."
    else:
        restored = f"I understand your {head_l}{name_bit}."

    if not rest:
        return restored[0].upper() + restored[1:] if restored else restored
    rest_cap = rest[0].upper() + rest[1:] if rest[0].islower() else rest
    return f"{restored} {rest_cap}".strip()


def _agent_starts_with_empathy_filler(text: str) -> bool:
    lower = (text or "").lower().strip()
    if not lower:
        return False
    return lower.startswith(
        (
            "i understand",
            "i completely understand",
            "i'm sorry",
            "i am sorry",
            "i hear your",
            "i hear you",
            "i appreciate",
        )
    )


def _is_weak_empathy_spam(text: str) -> bool:
    """
    True for empty 'I understand' filler — not for a real scripted beat.

    "I understand your concern, Lily. We're working on it…" is a full timeline line
    (empathy + action, often with the customer name). Treating every I-understand opener
    as spam used to strip that beat down to "We're working…", losing the name and making
    restored mid-sentence repairs ("concern. We're…") look unfinished.
    """
    lower = (text or "").lower().strip()
    if not lower.startswith(("i understand", "i completely understand", "i totally understand")):
        return False
    sentences = _split_sentences(text)
    if len(sentences) >= 2 and _word_count(sentences[1].rstrip(".!?")) >= 3:
        return False
    # Long single sentence with substance (e.g. understand + ask for ID) is not filler.
    if _word_count(lower.rstrip(".!?")) >= 8:
        return False
    return True


def _is_weak_ack_only(text: str) -> bool:
    lower = (text or "").lower().strip().rstrip(".!?,")
    if not lower:
        return True
    weak = {"thank you", "thanks", "understand", "okay", "ok", "sure", "alright"}
    if lower in weak:
        return True
    words = lower.split()
    return len(words) <= 4 and any(w in lower for w in weak)


def contextual_empathy_prefix(
    last_customer: str,
    known_name: str | None = None,
    description: str = "",
) -> str:
    """One-line empathy opener matched to what the customer just said."""
    lower = (last_customer or "").lower()
    desc = (description or "").lower()
    name_bit = f", {known_name}" if known_name else ""

    if any(w in lower for w in ("way too long", "too long", "unacceptable", "ridiculous", "frustrat")):
        return f"I hear your frustration{name_bit}. "
    if any(w in lower for w in ("didn't authorize", "didn't agree", "unauthorized", "never agreed", "unexpected")):
        return f"I'm sorry about that unauthorized charge{name_bit}. "
    if any(w in lower for w in ("$", "charged", "charge we", "billing")):
        return f"I'm sorry about that charge{name_bit}. "
    if any(w in lower for w in ("expedite", "how long", "when will", "waiting", "notified", "outcome")):
        return f"Thanks for your patience{name_bit}. "
    if any(w in lower for w in ("account", "card", "ends in", "digits")) and "charge" not in lower:
        return f"Thanks for those details{name_bit}. "
    if any(w in lower for w in ("can't", "cannot", "not connect", "not working", "no luck", "offline", "down")):
        return f"I'm sorry to hear that{name_bit}. "
    if any(w in desc for w in ("billing", "dispute", "charge", "refund")):
        return f"I'm sorry you're dealing with this billing issue{name_bit}. "
    return f"I appreciate you sharing that{name_bit}. "


def _agent_lacks_empathy(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    empathy_words = (
        "sorry", "understand", "frustrating", "apologize", "appreciate",
        "hear you", "inconvenience", "i know this", "that must be",
        "thank you for", "thanks for", "confirming those", "you're welcome",
        "got it", "absolutely", "good question", "let me review", "let me pull",
    )
    return not any(w in lower for w in empathy_words)


def is_verification_question(agent_text: str) -> bool:
    """True when the agent is asking a security/identity question — not empathy."""
    lower = (agent_text or "").lower()
    if any(m in lower for m in VERIFY_ASK_MARKERS):
        return True
    return any(
        p in lower
        for p in (
            "first pet", "mother's maiden", "maiden name", "date of birth",
            "what is the name", "what's the name", "last four", "security question",
            "full name", "company name", "first job",
        )
    )


def _prepend_empathy_to_agent_line(
    agent_text: str,
    last_customer: str,
    known_name: str | None,
    description: str = "",
) -> str:
    if is_verification_question(agent_text) or _is_summary_turn(agent_text):
        return shorten_speech_text(dedupe_repeated_sentences(agent_text), "trainee")
    core = _strip_redundant_empathy_prefix(agent_text)
    prefix = contextual_empathy_prefix(last_customer, known_name, description)
    if _text_overlaps_empathy_prefix(core, prefix):
        return shorten_speech_text(dedupe_repeated_sentences(core), "trainee")
    combined = dedupe_repeated_sentences(f"{prefix}{core.strip()}")
    return shorten_speech_text(combined, "trainee")


def agent_empathy_already_shown(conversation_history: list[dict]) -> bool:
    return any(
        not _agent_lacks_empathy(t.get("text", ""))
        for t in conversation_history
        if t.get("speaker") == "trainee"
    )


def sanitize_suggested_agent_line(
    text: str | None,
    conversation_history: list[dict],
    last_customer: str | None,
    description: str = "",
    known_name: str | None = None,
    persona: str = "",
) -> str | None:
    """Practice hints: one contextual empathy moment, then actionable replies only."""
    if not text:
        return text
    # Heal stored mid-sentence debris before practice read-aloud (concern. ≠ customer name).
    text = repair_mid_sentence_agent_line(text, known_name)
    if "to summarize" in text.lower():
        live = [
            {"speaker": t.get("speaker"), "text": (t.get("text") or "").strip()}
            for t in (conversation_history or [])
            if t.get("speaker") in ("trainee", "customer") and (t.get("text") or "").strip()
        ]
        if live:
            text = derive_resolution_summary(live, description, persona)
    empathy_shown = agent_empathy_already_shown(conversation_history)
    if empathy_shown and _agent_starts_with_empathy_filler(text):
        # Keep full scripted empathy lines for practice read-aloud (= timeline).
        return (text or "").strip()
    if (
        not empathy_shown
        and last_customer
        and customer_stated_problem(last_customer)
        and _agent_lacks_empathy(text)
        and not is_verification_question(text)
    ):
        return _prepend_empathy_to_agent_line(text, last_customer, known_name, description)
    if empathy_shown:
        # Do not strip empathy from later timeline lines either.
        return (text or "").strip()
    return (text or "").strip()


def build_resolution_check_reply(
    conversation_history: list[dict],
    scenario_context: dict,
    last_agent: str,
) -> str:
    """Answer 'is it resolved?' using full conversation + scenario timeline."""
    if _customer_already_confirmed_resolution(conversation_history):
        return shorten_speech_text("Yes — it's still working fine. Thank you!", "customer")

    last_cust = (last_message_from(conversation_history, "customer") or "").lower()
    if any(w in last_cust for w in ("trying to reconnect", "done the reset", "after the reset")):
        return shorten_speech_text(
            "Not yet — I'm still trying to reconnect after the factory reset.", "customer"
        )
    if "installing" in last_cust:
        return shorten_speech_text(
            "I'm still installing the update — I haven't been able to connect yet.", "customer"
        )

    resolved = infer_issue_resolved(conversation_history, scenario_context, last_agent)
    if resolved is True:
        desc_lower = (scenario_context.get("description") or "").lower()
        if any(w in desc_lower for w in ("email", "sync", "mail", "outlook")):
            return shorten_speech_text(
                "Yes — my emails are syncing now. Thank you for your help.", "customer"
            )
        return shorten_speech_text("Yes, it's working now. Thank you for your help.", "customer")
    if resolved is False:
        desc_lower = (scenario_context.get("description") or "").lower()
        if any(w in desc_lower for w in ("email", "sync", "mail", "outlook")):
            return shorten_speech_text(
                "Not yet — my emails still aren't syncing properly.", "customer"
            )
        return shorten_speech_text("Not yet — I'm still unable to get it connected.", "customer")

    if any(w in last_cust for w in ("still no luck", "still not", "not connect", "what should i do")):
        return shorten_speech_text("Not yet — it's still not connecting.", "customer")

    script = scenario_context.get("success_script", [])
    customer_count = count_speakers(conversation_history, "customer")
    ref = (next_customer_line(script, customer_count) or "").lower()
    if ref and any(w in ref for w in ("sounds good", "thank you for explaining", "that works")):
        return shorten_speech_text(
            "Not yet — I'm still working through the troubleshooting steps.", "customer"
        )

    return shorten_speech_text("Not yet — the issue isn't fully resolved on my end.", "customer")


def resolve_known_customer_name(script: list[dict], persona: str, description: str) -> str | None:
    """Resolve caller first name for any scenario category (billing, tech, custom, …).

    Order: persona/description (scenario card) → customer self-intro → agent address → facts.
    Persona wins so the call sticks to one name even when the model invents another in the
    script. Never treat severity/mood words (critical, urgent, frustrated) as names.
    """
    for source in (persona, description):
        source_name = _parse_persona_name(source)
        if source_name and is_plausible_person_name(source_name):
            return source_name
    self_name = extract_customer_name_from_script(script, allow_agent_address=False)
    if self_name and is_plausible_person_name(self_name):
        return self_name.split()[0]
    addressed = extract_customer_name_from_script(script, allow_agent_address=True)
    if addressed and is_plausible_person_name(addressed):
        return addressed.split()[0]
    facts = extract_scenario_facts(script, persona, description)
    fact_name = (facts.get("customer_name") or "").strip()
    if fact_name and is_plausible_person_name(fact_name):
        return fact_name.split()[0]
    return None


# Tokens the model sometimes inserts where a person name belongs (scrub even if persona has no name).
FALSE_CUSTOMER_NAME_TOKENS = MOOD_AND_SEVERITY_WORDS


def _safe_person_token(name: str | None) -> str:
    """Return a plain alphabetic first name suitable for regex/replacements, else ''."""
    first = ((name or "").strip().split() or [""])[0]
    if not re.fullmatch(r"[A-Za-z]{2,30}", first):
        return ""
    if first.lower() in INVALID_NAME_WORDS or first.lower() in FALSE_CUSTOMER_NAME_TOKENS:
        return ""
    if first.lower().endswith(_NON_NAME_SUFFIXES):
        return ""
    return first


def _scrub_false_customer_name_in_agent_line(text: str, correct_name: str | None = None) -> str:
    """Remove/replace severity/mood words wrongly used as the customer's name in agent lines."""
    line = (text or "").strip()
    if not line:
        return line
    correct = _safe_person_token(correct_name)
    for bad in FALSE_CUSTOMER_NAME_TOKENS:
        bad_esc = re.escape(bad)
        if correct:
            line = re.sub(
                rf"(\bto summarize),?\s+{bad_esc}\s*:",
                rf"\1, {correct}:",
                line,
                flags=re.I,
            )
            line = re.sub(
                rf"(,\s*){bad_esc}(\s*[:.!?]|\s*$)",
                rf"\1{correct}\2",
                line,
                flags=re.I,
            )
        else:
            # No real caller name — drop the false address entirely
            line = re.sub(
                rf"(\bto summarize),?\s+{bad_esc}\s*:",
                r"\1:",
                line,
                flags=re.I,
            )
            line = re.sub(
                rf",\s*{bad_esc}(\s*[:.!?]|\s*$)",
                r"\1",
                line,
                flags=re.I,
            )
    return re.sub(r"\s{2,}", " ", line).strip()


def _scrub_false_customer_self_intro(text: str, correct_name: str | None = None) -> str:
    """
    Repair a customer line that introduces the caller with a mood word — "Hi, this is
    Frustrated." The name parsers rightly refuse such a word, which is why no name is left to
    inject, so without this the line shipped as written, was spoken by Polly, and read as if
    the customer were called Frustrated.

    Only the unambiguous introduction shapes are touched: a greeting plus "this is <mood>", or
    "my name is <mood>". A bare "This is frustrating." is an ordinary complaint, not a name.
    """
    line = (text or "").strip()
    if not line:
        return line
    correct = _safe_person_token(correct_name)
    for bad in FALSE_CUSTOMER_NAME_TOKENS:
        bad_esc = re.escape(bad)
        greeted = rf"^(hi|hello|hey)([,!]?\s+)this is\s+{bad_esc}\b"
        named = rf"\bmy name is\s+{bad_esc}\b"
        if not re.search(greeted, line, flags=re.I) and not re.search(named, line, flags=re.I):
            continue
        if correct:
            line = re.sub(greeted, rf"\1\2this is {correct}", line, flags=re.I)
            line = re.sub(named, f"my name is {correct}", line, flags=re.I)
            continue
        # No real caller name to put back: drop the introduction, keep what they called about.
        line = re.sub(rf"{greeted}\s*[,.!]?\s*", "", line, flags=re.I)
        line = re.sub(rf"{named}\s*[,.!]?\s*", "", line, flags=re.I)
        line = line.strip(" ,.!")
        if line:
            line = line[0].upper() + line[1:]
    return re.sub(r"\s{2,}", " ", line).strip()


def _inject_persona_name_into_customer_line(text: str, customer_first: str) -> str:
    """Ensure the first customer turn names the caller from persona when missing or wrong."""
    line = (text or "").strip()
    if not line or not customer_first:
        return line
    existing = _customer_self_intro_match(line)
    if existing:
        current = existing.group(1).split()[0]
        if current.lower() == customer_first.lower():
            return line
        if _valid_customer_name(current) or current.lower() in FALSE_CUSTOMER_NAME_TOKENS:
            # Align invented / mood intros to the scenario's customer name.
            return re.sub(
                rf"(\b(?:this is|my name is|i'?m|i am)\s+){re.escape(current)}\b",
                rf"\1{customer_first}",
                line,
                count=1,
                flags=re.I,
            )
    if customer_first.lower() in line.lower():
        return line
    if re.match(r"^(hi|hello|hey)\b", line, re.I):
        rest = re.sub(r"^(hi|hello|hey)[,!]?\s*", "", line, count=1, flags=re.I).strip()
        if rest:
            rest = rest[0].upper() + rest[1:]
        return f"Hi, this is {customer_first}. {rest}".strip()
    return f"Hi, this is {customer_first}. {line}"


def _unify_agent_customer_addresses(
    text: str,
    customer_first: str,
    agent_name: str | None = None,
) -> str:
    """
    Make every agent address use the same customer first name.

    Replaces a different person name (or leftover mood token) in address slots such as
    ", Emma." / "to summarize, Frustrated:" with the canonical caller name.
    """
    line = (text or "").strip()
    if not line or not customer_first:
        return line
    cust = customer_first.lower()
    agent = (agent_name or "").lower()

    def _should_replace(name: str) -> bool:
        low = (name or "").lower()
        if not low or low == cust or low == agent:
            return False
        if low in FALSE_CUSTOMER_NAME_TOKENS or low in INVALID_NAME_WORDS:
            return True
        return bool(is_plausible_person_name(name))

    def _comma_address(match: re.Match) -> str:
        name = match.group("name")
        if not _should_replace(name):
            return match.group(0)
        return f"{match.group('pre')}{customer_first}{match.group('post')}"

    # Name slots stay case-sensitive ([A-Z][a-z]+) so "Thank you for calling" never
    # treats "for" as a person name (re.I would make [A-Z] match "f").
    line = re.sub(
        r"(?P<pre>,\s*)(?P<name>[A-Z][a-z]{1,29})(?P<post>\s*[:.!]|\s*$)",
        _comma_address,
        line,
    )
    line = re.sub(
        r"(?P<pre>(?i:to summarize)),?\s+(?P<name>[A-Z][a-z]{1,29})(?P<post>\s*:)",
        lambda m: (
            f"To summarize, {customer_first}{m.group('post')}"
            if _should_replace(m.group("name"))
            else m.group(0)
        ),
        line,
    )
    line = re.sub(
        r"(?P<pre>(?i:thank you(?: so much)?|thanks)),?\s+(?P<name>[A-Z][a-z]{1,29})\b",
        lambda m: (
            f"{m.group('pre')}, {customer_first}"
            if _should_replace(m.group("name"))
            else m.group(0)
        ),
        line,
    )
    return re.sub(r"\s{2,}", " ", line).strip()


def _dedupe_redundant_agent_thanks(text: str, customer_first: str) -> str:
    """Collapse 'Thanks …, Jane. Thank you, Jane.' into one thanks."""
    line = (text or "").strip()
    if not line or not customer_first:
        return line
    name = re.escape(customer_first)
    thanks_hits = re.findall(
        rf"\b(?:thank you|thanks)\b[^.!?]*\b{name}\b",
        line,
        flags=re.I,
    )
    if len(thanks_hits) < 2:
        return line
    # Remove the shortest standalone "Thank you, Name." / "Thanks, Name."
    line = re.sub(
        rf"\bThank you,\s*{name}\.\s*",
        "",
        line,
        count=1,
        flags=re.I,
    )
    line = re.sub(
        rf"\bThanks,\s*{name}\.\s*",
        "",
        line,
        count=1,
        flags=re.I,
    )
    return re.sub(r"\s{2,}", " ", line).strip()


def _fix_customer_self_address(
    text: str,
    customer_first: str,
    agent_name: str | None,
) -> str:
    """Customer must never thank/address themselves; use agent name when known."""
    line = (text or "").strip()
    if not line or not customer_first:
        return line
    name = re.escape(customer_first)
    if agent_name:
        line = re.sub(
            rf"\b(that sounds great),?\s+{name}\b",
            rf"\1, {agent_name}",
            line,
            flags=re.I,
        )
        line = re.sub(
            rf"\b(thank you(?: so much)?),?\s+{name}\b",
            rf"\1, {agent_name}",
            line,
            flags=re.I,
        )
        line = re.sub(
            rf"\b(thanks),?\s+{name}\b",
            rf"\1, {agent_name}",
            line,
            flags=re.I,
        )
    else:
        line = re.sub(
            rf"\b(that sounds great),?\s+{name}\b",
            r"\1",
            line,
            flags=re.I,
        )
        line = re.sub(
            rf"\b(thank you(?: so much)?),?\s+{name}\b",
            r"\1",
            line,
            flags=re.I,
        )
        line = re.sub(
            rf"\b(thanks),?\s+{name}\b",
            r"\1",
            line,
            flags=re.I,
        )
    return re.sub(r"\s{2,}", " ", line).strip(" ,")


def _customer_thanks_own_name(
    script: list[dict],
    persona: str = "",
    description: str = "",
) -> bool:
    """True when a closing customer line addresses the customer's own name."""
    customer = resolve_known_customer_name(script, persona, description) or ""
    first = customer.split()[0] if customer else ""
    if not first:
        return False
    for turn in script[-8:]:
        if turn.get("speaker") != "customer":
            continue
        text = turn.get("text") or ""
        if re.search(
            rf"\b(?:that sounds great|thank you(?: so much)?|thanks),?\s+{re.escape(first)}\b",
            text,
            re.I,
        ):
            return True
    return False


def repair_name_role_confusion(
    script: list[dict],
    description: str = "",
    persona: str = "",
) -> list[dict]:
    """
    Keep customer vs agent names distinct for every scenario.
    Customer closing thanks must address the agent (e.g. Alex), never themselves (Jane).
    """
    if not script:
        return script
    customer = resolve_known_customer_name(script, persona, description) or ""
    customer_first = _safe_person_token(customer)
    agent_name = _safe_person_token(_extract_agent_name_from_script(script))
    if (
        agent_name
        and customer_first
        and agent_name.lower() == customer_first.lower()
    ):
        agent_name = None
    trainee_intro_name = agent_name or "Alex"
    repaired: list[dict] = []
    first_customer_seen = False
    for turn in script:
        speaker = turn.get("speaker", "")
        text = turn.get("text", "") or ""
        if speaker == "customer":
            # Runs whether or not a real name is known: a mood word in the name slot must go
            # even when there is nothing to put back.
            text = _scrub_false_customer_self_intro(text, customer_first or None)
            if not first_customer_seen:
                if customer_first:
                    text = _inject_persona_name_into_customer_line(text, customer_first)
                first_customer_seen = True
            if customer_first:
                text = _fix_customer_self_address(text, customer_first, agent_name)
            # Strip Hi/Thanks/Alex-style address unless agent truly introduced that name
            text = _strip_unintroduced_agent_address(
                text,
                known_agent_name=agent_name,
            )
        elif speaker == "trainee":
            # Always scrub false name tokens (ridiculous/critical/…) even when persona has no name
            text = _scrub_false_customer_name_in_agent_line(
                text, customer_first or None
            )
            if customer_first:
                text = _unify_agent_customer_addresses(
                    text, customer_first, agent_name
                )
                text = _dedupe_redundant_agent_thanks(text, customer_first)
                # Agent must not introduce themselves as the customer
                text = re.sub(
                    rf"\b((?:my name is|i'?m|i am|this is)\s+){re.escape(customer_first)}\b",
                    rf"\1{trainee_intro_name}",
                    text,
                    flags=re.I,
                )
        repaired.append({"speaker": speaker, "text": text})
    return repaired


def repair_dialogue_flow(
    script: list[dict],
    description: str = "",
    persona: str = "",
) -> list[dict]:
    """Fix opening greetings and agent apologies before the customer states their problem."""
    if not script:
        return script

    known_name = resolve_known_customer_name(script, persona, description)
    repaired: list[dict] = []
    last_customer: str | None = None
    customer_spoke = False
    problem_stated = False
    empathy_applied = False
    recent_trainee: list[str] = []

    for turn in script:
        speaker = turn.get("speaker", "")
        text = turn.get("text", "")

        if speaker == "customer":
            last_agent_line = ""
            for prev in reversed(repaired):
                if prev.get("speaker") == "trainee":
                    last_agent_line = prev.get("text", "")
                    break
            if customer_discloses_verification_data(text):
                if not agent_requested_verification_info(last_agent_line):
                    if repaired and repaired[-1].get("speaker") == "trainee":
                        repaired[-1]["text"] = shorten_speech_text(
                            build_verification_ask_before_disclosure(
                                last_customer or "", text, description, known_name
                            ),
                            "trainee",
                        )
                        last_agent_line = repaired[-1]["text"]
                text = normalize_customer_disclosure(text, last_agent_line)
            if customer_stated_problem(text):
                problem_stated = True
            repaired.append({"speaker": "customer", "text": repair_dangling_text(text)})
            last_customer = text
            customer_spoke = True
            continue

        if speaker == "trainee":
            if not customer_spoke and is_premature_opening(text):
                text = build_agent_opening_greeting(description, persona, script)
            elif (
                last_customer
                and not problem_stated
                and should_replace_after_intro(text, last_customer)
            ):
                text = build_gather_info_response(last_customer, description, known_name)
                last_customer = None
            elif last_customer and problem_stated and is_misplaced_gather_info(text):
                text = build_mid_call_agent_response(last_customer, description)
                last_customer = None
            elif (
                last_customer
                and problem_stated
                and is_premature_apology(text)
                and _is_weak_ack_only(text)
            ):
                text = build_mid_call_agent_response(last_customer, description)
                last_customer = None
            elif (
                last_customer
                and problem_stated
                and empathy_applied
                and _agent_starts_with_empathy_filler(text)
            ):
                if not _is_weak_empathy_spam(text):
                    # Keep strong timeline empathy (I'm sorry / I hear) — practice = preview.
                    pass
                else:
                    # Drop repeated "I understand" spam; re-empathize on strong emotion.
                    cleaned = _strip_redundant_empathy_prefix(text)
                    if cleaned and not _is_weak_ack_only(cleaned):
                        cust_l = last_customer.lower()
                        if any(
                            w in cust_l
                            for w in (
                                "way too long",
                                "too long",
                                "unacceptable",
                                "ridiculous",
                                "frustrat",
                                "expedite",
                            )
                        ):
                            text = _prepend_empathy_to_agent_line(
                                cleaned, last_customer, known_name, description
                            )
                        else:
                            text = cleaned
                    else:
                        text = build_mid_call_agent_response(last_customer, description)
                last_customer = None
            elif (
                last_customer
                and problem_stated
                and not empathy_applied
                and _agent_starts_with_empathy_filler(text)
            ):
                if _is_weak_empathy_spam(text):
                    # Replace "I understand…" spam with real empathy or a clean verify ask.
                    cleaned = _strip_redundant_empathy_prefix(text)
                    if is_verification_question(cleaned or text):
                        text = cleaned or text
                    elif cleaned and not _is_weak_ack_only(cleaned):
                        text = _prepend_empathy_to_agent_line(
                            cleaned, last_customer, known_name, description
                        )
                        empathy_applied = True
                    else:
                        text = _prepend_empathy_to_agent_line(
                            build_mid_call_agent_response(last_customer, description),
                            last_customer,
                            known_name,
                            description,
                        )
                        empathy_applied = True
                else:
                    # Strong scripted empathy (e.g. "I'm sorry you're dealing…") — keep full line.
                    empathy_applied = True
                last_customer = None
            elif (
                last_customer
                and problem_stated
                and not empathy_applied
                and _agent_lacks_empathy(text)
                and not is_welcome_opening(text)
                and not is_verification_question(text)
                and not is_misplaced_gather_info(text)
                and "to summarize" not in text.lower()
            ):
                text = _prepend_empathy_to_agent_line(text, last_customer, known_name, description)
                empathy_applied = True
                last_customer = None
            elif problem_stated and empathy_applied and _agent_starts_with_empathy_filler(text):
                if not _is_weak_empathy_spam(text):
                    pass  # keep strong scripted empathy beats
                else:
                    cleaned = _strip_redundant_empathy_prefix(text)
                    if cleaned and not _is_weak_ack_only(cleaned):
                        text = cleaned
                    else:
                        text = build_mid_call_agent_response(last_customer or "", description)
                last_customer = None
            else:
                last_customer = None
            if problem_stated and "to summarize" not in text.lower():
                norm = text.lower().strip()
                if norm and norm in {r.lower().strip() for r in recent_trainee[-3:]}:
                    text = _vary_agent_line(text, last_customer or "", description, recent_trainee)
                recent_trainee.append(text)
            repaired.append({"speaker": "trainee", "text": repair_dangling_text(dedupe_repeated_sentences(text))})
            continue

        repaired.append({"speaker": speaker, "text": text})

    return dedupe_script_lines(repaired, description)


def _valid_customer_name(name: str) -> bool:
    parts = (name or "").strip().split()
    if not parts:
        return False
    first = parts[0]
    lower = first.lower()
    if not first or lower in INVALID_NAME_WORDS:
        return False
    if lower in FALSE_CUSTOMER_NAME_TOKENS:
        return False
    if len(first) <= 2:
        return False
    if lower.endswith(_NON_NAME_SUFFIXES):
        return False
    return True


def is_plausible_person_name(name: str) -> bool:
    return _valid_customer_name(name)


def _customer_self_intro_match(text: str) -> re.Match | None:
    """Match a real name self-intro — not exclamations like 'this is ridiculous!'."""
    match = CUSTOMER_IAM_NAME_PAT.search(text or "")
    if match:
        return match
    return CUSTOMER_THIS_IS_NAME_PAT.search(text or "")


def extract_customer_name_from_script(
    script: list[Any],
    *,
    allow_agent_address: bool = True,
) -> str | None:
    """Prefer customer self-intro; optionally fall back to agent addressing the caller."""
    self_names: list[str] = []
    addressed: list[str] = []
    for turn in script:
        text = turn.get("text", "") if isinstance(turn, dict) else getattr(turn, "text", "")
        speaker = turn.get("speaker") if isinstance(turn, dict) else getattr(turn, "speaker", "")
        if not text:
            continue
        if speaker == "customer":
            match = _customer_self_intro_match(text)
            if match:
                captured = match.group(1).strip()
                first = captured.split()[0]
                if _valid_customer_name(first):
                    self_names.append(captured if " " in captured else first)
        elif speaker == "trainee" and allow_agent_address:
            for match in AGENT_NAME_PAT.finditer(text):
                name = match.group(1)
                if _valid_customer_name(name):
                    addressed.append(name)
    if self_names:
        return Counter(self_names).most_common(1)[0][0]
    if allow_agent_address and addressed:
        return Counter(addressed).most_common(1)[0][0]
    return None


def _looks_like_birth_year(blob: str, year: str, start: int, end: int) -> bool:
    """True when a 4-digit token is a DOB year, not card digits."""
    window = blob[max(0, start - 60): min(len(blob), end + 30)]
    lower = window.lower()
    if any(h in lower for h in BIRTH_YEAR_HINTS):
        return True
    if MONTH_DATE_YEAR.search(window):
        return True
    try:
        y = int(year)
    except ValueError:
        return False
    if 1900 <= y <= 2015:
        card_window = blob[max(0, start - 45): min(len(blob), end + 45)].lower()
        if not any(h in card_window for h in CARD_CONTEXT_HINTS):
            wider = blob[max(0, start - 90): min(len(blob), end + 45)].lower()
            if any(h in wider for h in BIRTH_YEAR_HINTS) or MONTH_DATE_YEAR.search(wider):
                return True
    return False


def _extract_birth_year(blob: str) -> str | None:
    for match in MONTH_DATE_YEAR.finditer(blob):
        return match.group(1)
    for hint in BIRTH_YEAR_HINTS:
        pat = re.compile(rf"{re.escape(hint)}[^\d]{{0,35}}(\d{{4}})\b", re.I)
        match = pat.search(blob)
        if match:
            return match.group(1)
    return None


def _extract_card_last4(blob: str) -> str | None:
    card = CARD_LAST4.search(blob)
    if card:
        val = card.group(1)
        if not _looks_like_birth_year(blob, val, card.start(), card.end()):
            return val
    for match in STANDALONE_LAST4.finditer(blob):
        val = match.group(1)
        if _looks_like_birth_year(blob, val, match.start(), match.end()):
            continue
        window = blob[max(0, match.start() - 45): min(len(blob), match.end() + 45)].lower()
        if any(h in window for h in CARD_CONTEXT_HINTS):
            return val
    return None


def _parse_persona_name(persona: str) -> str | None:
    persona = (persona or "").strip()
    if not persona:
        return None
    head = persona.split(",")[0].strip()
    if head.lower().startswith(("a ", "an ")) and " " in head:
        head = head.split(" ", 1)[1].strip()
    if " from " in head.lower():
        head = head.split(" from ", 1)[0].strip()
    words = head.split()
    if not words:
        return None
    first = words[0]
    if not first or not first[0].isupper() or first.lower() in INVALID_NAME_WORDS:
        return None
    if not _valid_customer_name(first):
        return None
    if len(words) >= 2 and words[1][0].isupper():
        return first
    if len(words) == 1:
        return first
    return None


def extract_scenario_facts(script: list[Any] | None = None, *text_blobs: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    script_blob = ""
    if script:
        script_blob = " ".join(
            (t.get("text", "") if isinstance(t, dict) else getattr(t, "text", "")) for t in script
        )
    blob = " ".join([script_blob, *(t for t in text_blobs if t)]).strip()
    if not blob:
        return facts

    # Same priority for every scenario: self-intro → persona/description → agent address
    if script:
        name = extract_customer_name_from_script(script, allow_agent_address=False)
        if name and is_plausible_person_name(name):
            facts["customer_name"] = name.split()[0]

    if "customer_name" not in facts:
        for text in text_blobs:
            persona_name = _parse_persona_name(text or "")
            if persona_name and is_plausible_person_name(persona_name):
                facts["customer_name"] = persona_name
                break

    if "customer_name" not in facts and script:
        addressed = extract_customer_name_from_script(script, allow_agent_address=True)
        if addressed and is_plausible_person_name(addressed):
            facts["customer_name"] = addressed.split()[0]

    birth_year = _extract_birth_year(blob)
    if birth_year:
        facts["birth_year"] = birth_year

    card_last4 = _extract_card_last4(blob)
    if card_last4 and birth_year and card_last4 == birth_year:
        card_last4 = None
    if card_last4:
        facts["card_last4"] = card_last4

    email = EMAIL_RE.search(blob)
    if email:
        facts["email"] = email.group(0)

    phone = PHONE_RE.search(blob)
    if phone:
        facts["phone"] = phone.group(0)

    amount = AMOUNT_RE.search(blob)
    if amount:
        facts["amount"] = amount.group(0)

    account = ACCOUNT_RE.search(blob)
    if account:
        facts["account_ref"] = account.group(1)

    return facts


def _agent_lines(script: list[dict], tail: int = 8) -> str:
    lines = [t.get("text", "") for t in script if t.get("speaker") == "trainee"]
    return " ".join(lines[-tail:]).lower()


def _is_hollow_summary(text: str) -> bool:
    lower = (text or "").lower()
    if "let me summarize" in lower and "to summarize" not in lower:
        return True
    if "to summarize" in lower and any(marker in lower for marker in GENERIC_SUMMARY_MARKERS):
        return True
    if re.search(r"\bhelped you,\s+\w+\s+from\b", lower):
        return True
    if _ends_with_from_clause(lower) and "to summarize" in lower:
        return True
    return False


def derive_resolution_summary(
    script: list[dict],
    description: str = "",
    persona: str = "",
) -> str:
    """Build a closing summary that matches what the agent actually said in the script."""
    facts = extract_scenario_facts(script, description, persona)
    name = facts.get("customer_name", "")
    if not is_plausible_person_name(name):
        name = _parse_persona_name(persona) or ""
    if not is_plausible_person_name(name):
        name = ""
    name_bit = f", {name.split()[0]}" if name else ""
    agent_tail = _agent_lines(script, tail=10)
    full_agent = _agent_lines(script, tail=999)
    desc_lower = (description or "").lower()

    timing_match = TIMING_PAT.search(agent_tail) or TIMING_PAT.search(full_agent)
    timing = timing_match.group(1) if timing_match else ""

    if any(w in agent_tail for w in ("senior technician", "technician should contact", "technician will contact", "call you back", "call you directly", "contact you within", "escalat")):
        when = f" within {timing}" if timing else " shortly"
        return (
            f"To summarize{name_bit}: we've escalated your case and a senior technician "
            f"will contact you directly{when}."
        )

    if "refund" in agent_tail:
        amt = facts.get("amount") or (AMOUNT_RE.search(agent_tail) or AMOUNT_RE.search(full_agent))
        amt_text = f" of {amt}" if isinstance(amt, str) else (f" of {amt.group()}" if amt else "")
        return f"To summarize{name_bit}: we processed your refund{amt_text}. It should post within 3-5 business days."

    if any(w in agent_tail for w in ("credit", "credited", "applied a credit")):
        return f"To summarize{name_bit}: we applied a credit to your account and documented the issue."

    if any(w in agent_tail for w in ("scheduled", "appointment", "technician visit")):
        return f"To summarize{name_bit}: we scheduled a technician visit and you'll receive confirmation shortly."

    if any(
        w in agent_tail
        for w in (
            "account is active", "welcome aboard", "activation is complete", "now active",
            "now activated", "account is now activated", "manually activ", "activated your account",
        )
    ):
        return f"To summarize{name_bit}: your account is now active and you're all set to get started."

    if any(w in desc_lower for w in ("activat", "onboarding", "new account")):
        if facts.get("birth_year") or facts.get("account_ref"):
            return (
                f"To summarize{name_bit}: we verified your details "
                f"and completed your account activation."
            )
        return f"To summarize{name_bit}: we completed your activation steps and your account is being set up."

    if any(w in agent_tail for w in ("update the email", "updated the email", "activation link", "email address", "invalid credentials", "credentials")):
        if any(w in agent_tail for w in ("imap", "smtp", "re-add", "server settings", "syncing", "sync")):
            return (
                f"To summarize{name_bit}: we reconfigured your email account settings "
                f"and your messages should sync normally now."
            )
        return f"To summarize{name_bit}: we corrected your account details and your activation should be complete."

    if any(
        w in agent_tail
        for w in ("imap", "smtp", "re-add", "server settings", "syncing", "remove and re-add", "accounts")
    ) or any(w in desc_lower for w in ("email sync", "sync", "outlook", "exchange")):
        return (
            f"To summarize{name_bit}: we walked through the email setup steps "
            f"and your messages should sync normally now."
        )

    if any(w in agent_tail for w in ("applied the fix", "issue should be resolved", "working now", "resolved")):
        return f"To summarize{name_bit}: we've applied the fix and your issue should be resolved."

    if any(w in agent_tail for w in ("diagnostic", "troubleshoot", "checked", "reviewed your case", "reviewing your case", "check the details")):
        return f"To summarize{name_bit}: we reviewed your case and documented the next steps for resolution."

    if any(
        w in agent_tail
        for w in (
            "printer", "driver", "network settings", "factory reset", "wi-fi", "wifi",
            "reconnect", "router", "power cycle", "connecting again", "troubleshoot",
            "network found", "installing",
        )
    ) or any(
        w in desc_lower
        for w in ("printer", "connectivity", "network", "router", "technical", "troubleshoot")
    ):
        return (
            f"To summarize{name_bit}: we walked through the troubleshooting steps "
            f"and documented the next actions for your connection issue."
        )

    if facts.get("card_last4"):
        return f"To summarize{name_bit}: we verified your account and confirmed card ending in {facts['card_last4']}."

    if "escalat" in agent_tail or "specialist will follow" in agent_tail:
        return (
            f"To summarize{name_bit}: we've escalated your case and a specialist "
            f"will follow up with you directly."
        )

    if any(w in desc_lower for w in ("billing", "charge", "refund")):
        return f"To summarize{name_bit}: we reviewed your billing concern and documented the resolution."

    if "escalat" in desc_lower and "escalat" in agent_tail:
        return (
            f"To summarize{name_bit}: we've escalated your case and a specialist "
            f"will follow up with you directly."
        )

    return (
        f"To summarize{name_bit}: we documented your issue and the next steps we discussed "
        f"during this call."
    )


def _summary_needs_rebuild(text: str, description: str, persona: str, script: list[dict]) -> bool:
    if _is_hollow_summary(text):
        return True
    lower = (text or "").lower()
    desc_lower = (description or "").lower()
    agent_tail = _agent_lines(script, tail=12)
    if re.search(r",\s*(installing|updating|restarting|checking|connecting|reconnecting)\s*:", lower):
        return True
    named = re.search(r"\bto summarize,\s*([a-z]+)\s*:", lower)
    if named:
        token = named.group(1)
        if token in INVALID_NAME_WORDS:
            return True
        known = resolve_known_customer_name(script, persona, description) or ""
        if known and token != known.split()[0].lower():
            return True
    tech_desc = any(
        w in desc_lower
        for w in ("printer", "connectivity", "network", "router", "technical", "troubleshoot")
    )
    if tech_desc and any(w in lower for w in ("escalat", "specialist will follow")):
        if "escalat" not in agent_tail:
            return True
    if "activat" in desc_lower and any(w in lower for w in ("escalat", "specialist will follow")):
        if not any(w in agent_tail for w in ("escalat", "specialist", "technician")):
            return True
    if any(w in desc_lower for w in ("billing", "charge", "refund")) and "escalat" in lower:
        if "escalat" not in desc_lower and "escalat" not in agent_tail:
            return True
    expected = derive_resolution_summary(script, description, persona)
    exp_lower = expected.lower().strip()
    if exp_lower != lower.strip():
        if any(w in lower for w in ("escalat", "specialist will follow")) and "escalat" not in exp_lower:
            return True
        if "card ending in" in lower and "card ending in" not in exp_lower:
            return True
        if "activat" in desc_lower:
            return True
    return False


def refresh_script_summary(
    script: list[dict],
    description: str = "",
    persona: str = "",
) -> list[dict]:
    """Rebuild any 'to summarize' agent line so it matches the actual call context."""
    if not script:
        return script
    rebuilt = list(script)
    for i, turn in enumerate(rebuilt):
        if turn.get("speaker") != "trainee":
            continue
        text = turn.get("text", "")
        if "to summarize" not in text.lower():
            continue
        body = rebuilt[:i]
        if _summary_needs_rebuild(text, description, persona, body):
            summary = derive_resolution_summary(body, description, persona)
            rebuilt[i] = {"speaker": "trainee", "text": shorten_speech_text(summary, "trainee")}
    return rebuilt


def build_summary_line(script: list[dict], description: str = "", persona: str = "") -> str:
    return derive_resolution_summary(script, description, persona)


def build_closing_turns(
    script: list[dict],
    description: str = "",
    persona: str = "",
) -> list[tuple[str, str]]:
    summary = derive_resolution_summary(script, description, persona)
    facts = extract_scenario_facts(script, persona, description)
    raw_name = (facts.get("customer_name") or _parse_persona_name(persona) or "").strip()
    customer_first = raw_name.split()[0] if raw_name else ""
    # Customer may thank the agent by name ONLY if the agent introduced that name.
    agent_name = _extract_agent_name_from_script(script)
    if (
        agent_name
        and customer_first
        and agent_name.lower() == customer_first.lower()
    ):
        agent_name = None
    agent_bit = (
        f", {agent_name}"
        if agent_name and is_plausible_person_name(agent_name)
        else ""
    )
    return [
        ("trainee", summary),
        ("customer", f"That sounds great{agent_bit} — thank you for explaining everything clearly."),
        ("trainee", "You're welcome! I'm glad we could help. Is there anything else I can help you with today?"),
        ("customer", "No, that's everything. I really appreciate your help today."),
        ("trainee", "Thank you for calling. Have a wonderful day!"),
    ]


def _closing_dicts(
    script: list[dict] | None = None,
    description: str = "",
    persona: str = "",
) -> list[dict]:
    turns = build_closing_turns(script or [], description, persona)
    return [{"speaker": s, "text": t} for s, t in turns]


def shorten_speech_text(text: str, speaker: str = "customer") -> str:
    line = repair_dangling_text((text or "").strip())
    if not line:
        return line

    max_words = MAX_CUSTOMER_WORDS if speaker == "customer" else MAX_TRAINEE_WORDS
    sentences = _split_sentences(line)

    kept: list[str] = []
    word_total = 0
    for sent in sentences[:MAX_SENTENCES_PER_TURN]:
        wc = _word_count(sent)
        if word_total + wc > max_words and kept:
            break
        kept.append(sent)
        word_total += wc

    if kept:
        shortened = " ".join(kept).strip()
    else:
        words = line.split()
        shortened = " ".join(words[:max_words]).strip()
        if shortened and shortened[-1] not in ".!?":
            shortened += "."

    if speaker == "trainee":
        sop_markers = (
            "verify", "summarize", "recorded", "resolve", "policy",
            "account number", "may i have", "could you please", "model", "serial",
        )
        if not any(m in shortened.lower() for m in sop_markers):
            for sent in sentences:
                if any(m in sent.lower() for m in sop_markers) and sent not in kept:
                    shortened = f"{shortened.rstrip('.')}. {sent.strip()}".strip()
                    break

    shortened = repair_dangling_text(shortened)

    # Preserve critical facts only when truncation dropped them AND the kept text
    # is still about that fact (avoids "confirmed your identity — $500").
    dropped_blob = " ".join(s for s in sentences if s not in kept)
    facts = extract_scenario_facts(None, line)
    shortened_l = shortened.lower()
    for key in ("card_last4", "amount", "account_ref"):
        val = facts.get(key)
        if not val or val in shortened:
            continue
        if val not in dropped_blob and val not in line:
            continue
        if key == "card_last4":
            if any(w in shortened_l for w in ("card", "ending", "last four", "last 4", "digits")):
                if not re.search(rf"\b{re.escape(val)}\b", shortened):
                    shortened = f"{shortened.rstrip('.')} — card ending in {val}."
        elif key == "amount":
            if any(
                w in shortened_l
                for w in ("charge", "amount", "invoice", "billing", "refund", "payment", "dollar")
            ):
                shortened = f"{shortened.rstrip('.')} — {val}."
        elif key == "account_ref":
            if any(w in shortened_l for w in ("account", "reference", "ref")):
                shortened = f"{shortened.rstrip('.')} — {val}."

    return shortened


def normalize_script(script: list[Any]) -> list[dict]:
    merged = ensure_agent_opens_first(coalesce_script_turns(script))
    result: list[dict] = []
    for turn in merged:
        speaker = turn["speaker"]
        raw = repair_dangling_text(turn["text"])
        text = shorten_speech_text(raw, speaker)
        if text:
            result.append({"speaker": speaker, "text": text})
    return result


def normalize_script_manual_edit(script: list[Any]) -> list[dict]:
    """Preserve trainer edits — structural cleanup only, no shortening or filler."""
    cleaned: list[dict] = []
    for turn in script or []:
        speaker = str(turn.get("speaker") or "").strip().lower()
        if speaker in ("agent", "trainee", "support"):
            speaker = "trainee"
        elif speaker != "customer":
            continue
        text = repair_dangling_text(str(turn.get("text") or "").strip())
        if text:
            cleaned.append({"speaker": speaker, "text": text})
    return ensure_agent_opens_first(coalesce_script_turns(cleaned))


def finalize_generated_script(
    script: list[dict],
    description: str = "",
    persona: str = "",
    *,
    min_turns: int = MIN_SCRIPT_TURNS,
    max_turns: int = MAX_SCRIPT_TURNS,
) -> list[dict]:
    """Guarantee AI-generated scripts have min_turns–max_turns dialogue turns."""
    if not script:
        return script

    body = _strip_closing_segment(list(script))
    closing = _closing_dicts(body, description, persona)
    target_body = max(len(body), min_turns - len(closing))

    filler_pairs = _contextual_filler(description)
    fi = 0
    guard = 0
    while len(body) < target_body and guard < min_turns * 4:
        speaker, text = filler_pairs[fi % len(filler_pairs)]
        fi += 1
        guard += 1
        if body and body[-1].get("speaker") == speaker:
            continue
        norm = text.lower().strip()
        if any(
            (t.get("text") or "").lower().strip() == norm
            for t in body
            if t.get("speaker") == speaker
        ):
            text = f"{text.rstrip('.')}, thanks."
        body.append({"speaker": speaker, "text": text})

    if body and closing and body[-1]["speaker"] == closing[0]["speaker"]:
        body.pop()

    result = body + closing
    if len(result) > max_turns:
        trim_body = result[: max(1, max_turns - len(closing))]
        if trim_body and closing and trim_body[-1]["speaker"] == closing[0]["speaker"]:
            trim_body.pop()
        result = trim_body + closing[: max_turns - len(trim_body)]
    return result[:max_turns]


def _tail_text(script: list[dict], n: int = 6) -> str:
    return " ".join((t.get("text") or "").lower() for t in script[-n:])


def has_proper_closing(script: list[dict]) -> bool:
    if len(script) < 5:
        return False
    if _count_summary_turns(script) != 1:
        return False
    summary_idx = next(
        (
            i
            for i, turn in enumerate(script)
            if turn.get("speaker") == "trainee" and _is_summary_turn(turn.get("text", ""))
        ),
        None,
    )
    if summary_idx is None or summary_idx < len(script) - 5:
        return False
    summary_text = script[summary_idx].get("text", "").lower()
    if any(marker in summary_text for marker in GENERIC_SUMMARY_MARKERS):
        return False
    tail = _tail_text(script, 8)
    has_goodbye = any(
        w in tail for w in ("wonderful day", "great day", "thank you for calling", "goodbye", "have a good")
    )
    has_thanks = "thank" in tail
    has_wrap = "anything else" in tail or "you're welcome" in tail
    return has_goodbye and has_thanks and has_wrap


def ensure_proper_closing(
    script: list[dict],
    description: str = "",
    persona: str = "",
) -> list[dict]:
    if not script:
        return _closing_dicts([], description, persona)
    script = ensure_timing_answer_before_summary(script)
    # Rebuild when closing looks complete but customer thanks their own name (Jane → Jane)
    if has_proper_closing(script) and not _customer_thanks_own_name(
        script, persona, description
    ):
        return script

    body = _strip_closing_segment(script)
    body = [
        turn
        for turn in body
        if not (turn.get("speaker") == "trainee" and _is_summary_turn(turn.get("text", "")))
    ]
    body = ensure_timing_answer_before_summary(body)

    closing = _closing_dicts(body, description, persona)
    if body and body[-1]["speaker"] == closing[0]["speaker"]:
        body.pop()

    return body + closing


_UPDATE_TIMING_QUESTION_RE = re.compile(
    r"\b(?:when should i see|when will (?:i|we|the)|when can i expect|how long (?:until|before|will)|"
    r"when (?:does|do|will) (?:it|the|this)|how soon)\b",
    re.I,
)
_UPDATE_TIMING_ANSWER = (
    "You should see the update reflected within a few business days."
)
_UPDATE_TIMING_ACK = "Thank you — that timeline works for me."


def _agent_answers_update_timing(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "business day",
            "few days",
            "within",
            "reflected",
            "next bill",
            "next statement",
            "24 to 48",
            "1-2 business",
            "3-5 business",
            "you'll see",
            "you should see",
        )
    )


def ensure_timing_answer_before_summary(script: list[dict]) -> list[dict]:
    """If the customer asks when the update appears, answer before jumping to summary."""
    if not script:
        return script
    out: list[dict] = []
    i = 0
    while i < len(script):
        turn = script[i]
        speaker = turn.get("speaker")
        text = (turn.get("text") or "").strip()
        out.append({"speaker": speaker, "text": text})
        if speaker == "customer" and text and _UPDATE_TIMING_QUESTION_RE.search(text):
            nxt = script[i + 1] if i + 1 < len(script) else None
            nxt_text = (nxt.get("text") or "") if nxt else ""
            nxt_speaker = nxt.get("speaker") if nxt else None
            needs_answer = nxt_speaker != "trainee" or _is_summary_turn(nxt_text) or (
                nxt_speaker == "trainee" and not _agent_answers_update_timing(nxt_text)
            )
            if needs_answer:
                # Avoid duplicating an answer we already inserted.
                if not (out and out[-1].get("text") == _UPDATE_TIMING_ANSWER):
                    out.append({"speaker": "trainee", "text": _UPDATE_TIMING_ANSWER})
                    out.append({"speaker": "customer", "text": _UPDATE_TIMING_ACK})
        i += 1
    return out


def scrub_false_amount_on_identity_lines(script: list[dict]) -> list[dict]:
    """Remove glued amounts like 'confirmed your identity — $500'."""
    if not script:
        return script
    out: list[dict] = []
    for turn in script:
        speaker = turn.get("speaker")
        text = (turn.get("text") or "").strip()
        if speaker == "trainee" and text:
            lower = text.lower()
            if any(w in lower for w in ("identity", "verified", "confirm your", "confirmed your")):
                if not any(w in lower for w in ("charge", "refund", "invoice", "billing", "remove", "reverse")):
                    text = _strip_trailing_dash_amount(text)
                    text = re.sub(r"\.+", ".", text)
                    if text and text[-1] not in ".!?":
                        text += "."
        out.append({"speaker": speaker, "text": text})
    return out


def _contextual_filler(description: str) -> list[tuple[str, str]]:
    desc = (description or "").lower()
    if any(w in desc for w in ("escalat", "glitch", "manager", "supervisor", "angry")):
        return ESCALATION_FILLER
    if any(w in desc for w in ("billing", "charge", "refund", "invoice", "renewal")):
        return BILLING_FILLER
    return MID_CALL_FILLER


def strip_duplicate_narrative_turns(script: list[dict]) -> list[dict]:
    """Remove mid-script repeats of intro/greeting beats from legacy padding cycles."""
    if not script:
        return script
    body = _strip_closing_segment(script)
    seen_customer: set[str] = set()
    seen_trainee: set[str] = set()
    intro_seen = False
    greeting_seen = False
    cleaned: list[dict] = []
    for turn in body:
        speaker = turn.get("speaker")
        text = (turn.get("text") or "").strip()
        if not speaker or not text:
            continue
        # Keep timeline beats that match filler templates — practice must follow preview order.
        norm = text.lower().strip()
        if speaker == "customer":
            if is_recycled_customer_opening(text):
                if intro_seen:
                    continue
                intro_seen = True
            elif norm in seen_customer:
                continue
            seen_customer.add(norm)
        elif speaker == "trainee":
            if is_welcome_opening(text):
                if greeting_seen:
                    continue
                greeting_seen = True
            elif norm in seen_trainee:
                continue
            seen_trainee.add(norm)
        cleaned.append({"speaker": speaker, "text": text})
    return cleaned


def canonical_customer_lines(success_script: list[Any]) -> list[str]:
    """Customer beats in exact preview timeline order."""
    return customer_lines_from_script(success_script, skip_filler=False)


def canonical_trainee_lines(success_script: list[Any]) -> list[str]:
    """Agent beats in exact preview timeline order."""
    return trainee_lines_from_script(success_script, skip_filler=False)


def customer_turns_in_script(success_script: list[Any]) -> int:
    return len(canonical_customer_lines(success_script))


def count_expected_session_turns(success_script: list[Any]) -> int:
    """Playable practice/assessment turns matching the preview timeline order."""
    trainees = len(canonical_trainee_lines(success_script))
    customers = len(canonical_customer_lines(success_script))
    total = trainees + customers
    if total > 0:
        return total
    cleaned = 0
    for turn in success_script or []:
        speaker = turn.get("speaker") if isinstance(turn, dict) else getattr(turn, "speaker", None)
        text = turn.get("text") if isinstance(turn, dict) else getattr(turn, "text", "")
        if speaker in ("trainee", "customer") and (text or "").strip():
            cleaned += 1
    return max(cleaned, 1)


def script_customer_exhausted(success_script: list[Any], customer_count: int) -> bool:
    total = customer_turns_in_script(success_script)
    if total == 0:
        return False
    return customer_count >= total


def should_mark_conversation_complete(
    success_script: list[Any],
    conversation_history: list[dict],
    customer_count_after_reply: int,
    last_agent: str,
    customer_reply: str,
) -> bool:
    """End the call only after scripted customer beats are delivered and the exchange is closing."""
    total_customer = customer_turns_in_script(success_script)
    if total_customer == 0:
        return False
    if customer_count_after_reply < total_customer:
        return False

    pending_history = list(conversation_history) + [
        {"speaker": "customer", "text": customer_reply or ""},
    ]
    if conversation_should_complete(last_agent, customer_reply):
        return True
    if conversation_in_closing_phase(pending_history):
        return True
    if _customer_accepted_closure(customer_reply):
        return True
    return False


def _extend_script_from_body(
    body: list[dict],
    target_len: int,
    description: str = "",
) -> list[dict]:
    """Pad short scripts by reusing existing scenario turns — never generic category filler."""
    extended = list(body)
    if not body or len(extended) >= target_len:
        return extended
    pool = [t for t in body if (t.get("text") or "").strip() and t.get("speaker") in ("customer", "trainee")]
    if len(pool) < 2:
        pool = list(body)
    last_customer = ""
    trainee_seen: list[str] = []
    for turn in extended:
        if turn.get("speaker") == "customer":
            last_customer = turn.get("text", "")
        elif turn.get("speaker") == "trainee":
            trainee_seen.append(turn.get("text", ""))
    idx = 0
    guard = 0
    while len(extended) < target_len and guard < target_len * 4:
        turn = dict(pool[idx % len(pool)])
        if extended and extended[-1].get("speaker") == turn.get("speaker"):
            idx += 1
            guard += 1
            continue
        if turn.get("speaker") == "customer":
            norm = (turn.get("text") or "").lower().strip()
            if is_intro_only_customer(turn.get("text", "")) and extended:
                idx += 1
                guard += 1
                continue
            if any(
                texts_similar(norm, (e.get("text") or "").lower(), 0.72)
                for e in extended
                if e.get("speaker") == "customer"
            ):
                idx += 1
                guard += 1
                continue
            last_customer = turn.get("text", "")
        elif turn.get("speaker") == "trainee":
            if is_welcome_opening(turn.get("text", "")) and extended:
                idx += 1
                guard += 1
                continue
            norm = (turn.get("text") or "").lower().strip()
            if norm in {r.lower().strip() for r in trainee_seen}:
                turn["text"] = _vary_agent_line(turn["text"], last_customer, description, trainee_seen)
            trainee_seen.append(turn["text"])
        extended.append(turn)
        idx += 1
        guard += 1
    return extended


def ensure_min_script_turns(
    script: list[dict],
    min_turns: int = MIN_SCRIPT_TURNS,
    description: str = "",
    persona: str = "",
) -> list[dict]:
    body = _strip_closing_segment(script)
    body = strip_injected_filler_turns(body)
    body = strip_duplicate_narrative_turns(body)
    body = [
        turn
        for turn in body
        if not (turn.get("speaker") == "trainee" and _is_summary_turn(turn.get("text", "")))
    ]

    closing = _closing_dicts(body, description, persona)
    if len(body) + len(closing) >= min_turns:
        return ensure_proper_closing(body, description, persona)

    target_body = max(len(body), min_turns - len(closing))
    body = _extend_script_from_body(body, target_body, description)
    body = strip_duplicate_narrative_turns(body)
    body = _vary_repeated_trainee_turns(body, description)
    closing = _closing_dicts(body, description, persona)

    if body and closing and body[-1]["speaker"] == closing[0]["speaker"]:
        body.pop()

    pad_guard = 0
    while len(body) + len(closing) < min_turns and pad_guard < min_turns * 2:
        prev_len = len(body)
        body = _extend_script_from_body(body, len(body) + 1, description)
        pad_guard += 1
        if len(body) <= prev_len:
            break

    result = body + closing
    if has_proper_closing(result):
        return result
    return ensure_proper_closing(body, description, persona)


def repair_script_continuity(
    script: list[dict],
    description: str = "",
    persona: str = "",
) -> list[dict]:
    """Fix dangling lines and rebuild only the closing block at the end."""
    if not script:
        return script

    known_name = resolve_known_customer_name(script, persona, description)
    repaired: list[dict] = []
    for turn in script:
        text = repair_dangling_text(turn.get("text", ""))
        if turn.get("speaker") == "trainee":
            # Heal "concern. We're working…" debris; attach the real customer name, never the orphan.
            text = repair_mid_sentence_agent_line(text, known_name)
        if _is_hollow_summary(text):
            continue
        if "let me summarize" in text.lower() and "to summarize" not in text.lower():
            continue
        repaired.append({"speaker": turn["speaker"], "text": text})

    repaired = coalesce_script_turns(repaired)
    # Do not strip billing/follow-up timeline beats — practice must match preview order.
    repaired = strip_duplicate_narrative_turns(repaired)
    repaired = ensure_timing_answer_before_summary(repaired)
    repaired = _strip_closing_segment(repaired)
    repaired = repair_dialogue_flow(repaired, description, persona)
    repaired = _vary_repeated_trainee_turns(repaired, description)
    repaired = refresh_script_summary(repaired, description, persona)
    repaired = ensure_proper_closing(repaired, description, persona)
    repaired = repair_name_role_confusion(repaired, description, persona)
    repaired = scrub_false_amount_on_identity_lines(repaired)
    # Final pass: any mid-sentence orphan that survived earlier steps is healed with the
    # real customer name (never the orphan word itself).
    known_name = resolve_known_customer_name(repaired, persona, description)
    final: list[dict] = []
    for turn in repaired:
        text = turn.get("text", "")
        if turn.get("speaker") == "trainee":
            text = repair_mid_sentence_agent_line(text, known_name)
        final.append({"speaker": turn["speaker"], "text": text})
    return dedupe_script_lines(final, description)


def customer_lines_from_script(success_script: list[Any], *, skip_filler: bool = False) -> list[str]:
    """Customer lines in script order. Default keeps all preview beats (no filler skip)."""
    lines = []
    for turn in success_script:
        speaker = turn.get("speaker") if isinstance(turn, dict) else turn.speaker
        text = turn.get("text") if isinstance(turn, dict) else turn.text
        if speaker == "customer" and text:
            if skip_filler and is_generic_filler_line(text):
                continue
            lines.append(text)
    return lines


def trainee_lines_from_script(success_script: list[Any], *, skip_filler: bool = False) -> list[str]:
    """Agent lines in script order. Default keeps all preview beats (no filler skip)."""
    lines = []
    for turn in success_script:
        speaker = turn.get("speaker") if isinstance(turn, dict) else turn.speaker
        text = turn.get("text") if isinstance(turn, dict) else turn.text
        if speaker == "trainee" and text:
            if skip_filler and is_generic_filler_line(text):
                continue
            lines.append(text)
    return lines


def count_speakers(conversation: list[dict], speaker: str) -> int:
    return sum(1 for t in conversation if t.get("speaker") == speaker)


def next_customer_line(success_script: list[Any], customer_count: int) -> str | None:
    lines = canonical_customer_lines(success_script)
    if customer_count < 0 or customer_count >= len(lines):
        return None
    seen_earlier = {ln.lower().strip() for ln in lines[:customer_count]}
    for line in lines[customer_count:]:
        norm = line.lower().strip()
        # Skip mid-script recycled intros and exact duplicate padding beats.
        if customer_count > 0 and is_recycled_customer_opening(line):
            continue
        if norm in seen_earlier:
            continue
        return line
    return None


def next_trainee_line(success_script: list[Any], trainee_count: int) -> str | None:
    lines = canonical_trainee_lines(success_script)
    if trainee_count < 0 or trainee_count >= len(lines):
        return None
    seen_earlier = {ln.lower().strip() for ln in lines[:trainee_count]}
    for line in lines[trainee_count:]:
        norm = line.lower().strip()
        if trainee_count > 0 and is_welcome_opening(line):
            continue
        if norm in seen_earlier:
            continue
        return line
    return None


def texts_similar(a: str, b: str, threshold: float = 0.28) -> bool:
    """Rough overlap check — whether the agent stayed close to the reference script line."""
    aw = set((a or "").lower().split())
    bw = set((b or "").lower().split())
    if not aw or not bw:
        return False
    return len(aw & bw) / max(len(aw), len(bw)) >= threshold


_WEAK_THEME_WORDS = frozenset({
    "what", "which", "today", "help", "assist", "sorry", "number", "check",
})


def agent_thematically_aligned(expected: str, actual: str) -> bool:
    """True when both lines share a meaningful topic cue (not only weak words like 'what')."""
    if not expected or not actual:
        return False
    el, al = expected.lower(), actual.lower()
    for group in AGENT_TOPIC_GROUPS:
        el_hits = [w for w in group if w in el]
        al_hits = [w for w in group if w in al]
        if not el_hits or not al_hits:
            continue
        strong_shared = (set(el_hits) & set(al_hits)) - _WEAK_THEME_WORDS
        if strong_shared:
            return True
        strong_el = [w for w in el_hits if w not in _WEAK_THEME_WORDS]
        strong_al = [w for w in al_hits if w not in _WEAK_THEME_WORDS]
        if strong_el and strong_al:
            return True
    return False


_SCRIPT_MATCH_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "i", "you", "my", "your", "to",
    "and", "or", "it", "that", "this", "for", "on", "with", "have", "be", "can",
    "not", "just", "hi", "hello", "yeah", "please", "we", "me", "our", "of", "in",
    "at", "as", "so", "do", "if", "will", "would", "could", "may", "let",
})


def _script_content_tokens(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9']+", (text or "").lower())
        if w not in _SCRIPT_MATCH_STOPWORDS and len(w) > 1
    }


def script_line_similarity(actual: str, expected: str) -> float:
    """Score how closely an agent utterance matches a scripted line (0–1)."""
    a = (actual or "").strip().lower()
    b = (expected or "").strip().lower()
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = _script_content_tokens(a), _script_content_tokens(b)
    if not tb:
        return seq
    union = ta | tb
    jaccard = len(ta & tb) / len(union) if union else 0.0
    cover = len(ta & tb) / max(len(tb), 1)
    return max(seq, (0.55 * jaccard) + (0.45 * cover))


def agent_follows_expected_script(actual: str, expected: str, mode: str) -> bool:
    """
    Practice and assessment: if wording or context matches the script beat, customer replies.
    Only a complete off-script message holds the customer (toast, no next turn).
    Practice still surfaces the suggested line in the UI; exact wording is not required.
    """
    if not (actual or "").strip():
        return False
    if not (expected or "").strip():
        return True
    sim = script_line_similarity(actual, expected)
    ta, tb = _script_content_tokens(actual), _script_content_tokens(expected)
    strong_ta = ta - _WEAK_THEME_WORDS
    strong_tb = tb - _WEAK_THEME_WORDS
    # Require real topic overlap — shared weak words like "what"/"which" alone must not pass.
    strong_cover = (len(strong_ta & strong_tb) / max(len(strong_tb), 1)) if strong_tb else 0.0
    if strong_tb and strong_cover < 0.25 and not agent_thematically_aligned(expected, actual):
        return False
    if sim >= 0.55 and strong_cover >= 0.35:
        return True
    if sim >= 0.40 and strong_cover >= 0.50:
        return True
    if SequenceMatcher(None, actual.lower(), expected.lower()).ratio() >= 0.62 and strong_cover >= 0.35:
        return True
    if texts_similar(actual, expected, threshold=0.28) and strong_cover >= 0.40:
        return True
    # Same topic / intent even when phrased differently (practice + assessment)
    if agent_thematically_aligned(expected, actual):
        return True
    return False  # mode arg kept for callers; practice and assessment use the same gate


def should_hold_for_script_drift(
    actual: str,
    expected: str | None,
    mode: str,
    conversation_history: list[dict] | None = None,
) -> bool:
    """True when customer must not advance because the agent went off-script."""
    history = conversation_history or []
    if not expected:
        return False
    if conversation_in_closing_phase(history):
        return False
    if agent_asks_repeat(actual) or agent_asks_resolution_check(actual):
        return False
    if agent_asks_anything_else(actual):
        return False
    if agent_said_goodbye(actual) or agent_closing_the_call(actual):
        return False
    return not agent_follows_expected_script(actual, expected, mode)


def agent_gave_action_instruction(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    if any(
        w in lower
        for w in (
            "please", "try ", "try to", "check ", "restart", "reset", "install", "update",
            "ensure", "forget", "reconnect", "after the", "let's", "go ahead",
            "walk through", "power cycle", "great.", "good.",
        )
    ):
        return True
    return lower.startswith(("try", "check", "ensure", "restart", "install"))


CUSTOMER_ACCEPTANCE_PHRASES = (
    "sounds good", "sounds great", "thank you for explaining", "that works", "appreciate",
    "perfect", "that sounds", "glad to hear", "makes sense",
    "that's all", "thanks again", "that's everything", "no, that's all",
    "no more questions", "all i needed", "everything i needed",
)

CUSTOMER_CLOSING_DONE_PHRASES = (
    "that's all", "thanks again", "that's everything", "no, that's all",
    "no, that's everything", "everything. thanks", "all i needed",
)

CUSTOMER_UNRESOLVED_PHRASES = (
    "still not", "no luck", "doesn't work", "not connect", "same issue",
    "didn't work", "what should i do", "unable to", "not working", "not yet",
)

CUSTOMER_RESOLVED_PHRASES = (
    "all set", "works now", "connected", "it's working", "that fixed",
    "problem solved", "working fine", "working now", "it works",
    "syncing now", "emails are syncing", "messages are syncing", "coming through",
)


def agent_asked_question(agent_text: str) -> bool:
    text = agent_text or ""
    lower = text.lower()
    if "?" in text:
        return True
    return any(
        w in lower
        for w in (
            "could you tell", "can you tell", "may i ask", "would you",
            "please confirm", "let me know if", "did you", "have you",
            "is it", "is the", "are you", "anything else", "tell me if",
            "come again", "say again", "repeat",
        )
    )


def agent_gave_wrap_up_summary(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    return any(
        w in lower
        for w in (
            "summar", "to summarize", "walked through", "walk through",
            "documented", "next action", "next step", "recap", "we completed",
            "troubleshoot steps", "troubleshooting steps",
            "escalated your case", "senior technician", "technician will contact",
            "specialist will contact", "team will contact", "will get back to you",
        )
    )


def _agent_set_up_closure(agent_text: str) -> bool:
    if agent_gave_wrap_up_summary(agent_text):
        return True
    if agent_announced_escalation_or_handoff(agent_text):
        return True
    if agent_asks_anything_else(agent_text):
        return True
    return False


def _customer_accepted_closure(customer_text: str) -> bool:
    lower = (customer_text or "").lower()
    if any(phrase in lower for phrase in CUSTOMER_ACCEPTANCE_PHRASES):
        return True
    if "appreciate" in lower and any(w in lower for w in ("thank", "that's all", "help")):
        return True
    return False


def conversation_in_closing_phase(conversation_history: list[dict]) -> bool:
    """Customer accepted wrap-up, escalation, or said they're done — no more unresolved loops."""
    if not conversation_history:
        return False
    for i in range(len(conversation_history) - 1, -1, -1):
        turn = conversation_history[i]
        if turn.get("speaker") != "customer":
            continue
        text = turn.get("text") or ""
        if not _customer_accepted_closure(text):
            continue
        lower = text.lower()
        for j in range(i - 1, max(-1, i - 10), -1):
            prior = conversation_history[j]
            if prior.get("speaker") == "trainee" and _agent_set_up_closure(prior.get("text", "")):
                return True
        if any(p in lower for p in CUSTOMER_CLOSING_DONE_PHRASES):
            for j in range(i - 1, max(-1, i - 12), -1):
                prior = conversation_history[j]
                if prior.get("speaker") == "trainee" and (
                    agent_announced_escalation_or_handoff(prior.get("text", ""))
                    or agent_gave_wrap_up_summary(prior.get("text", ""))
                ):
                    return True
        break
    return False


def _acceptance_turn_index(conversation_history: list[dict]) -> int | None:
    for i in range(len(conversation_history) - 1, -1, -1):
        turn = conversation_history[i]
        if turn.get("speaker") != "customer":
            continue
        text = turn.get("text") or ""
        if not _customer_accepted_closure(text):
            continue
        lower = text.lower()
        for j in range(i - 1, max(-1, i - 10), -1):
            prior = conversation_history[j]
            if prior.get("speaker") == "trainee" and _agent_set_up_closure(prior.get("text", "")):
                return i
        if any(p in lower for p in CUSTOMER_CLOSING_DONE_PHRASES):
            for j in range(i - 1, max(-1, i - 12), -1):
                prior = conversation_history[j]
                if prior.get("speaker") == "trainee" and (
                    agent_announced_escalation_or_handoff(prior.get("text", ""))
                    or agent_gave_wrap_up_summary(prior.get("text", ""))
                ):
                    return i
        break
    return None


def _latest_customer_troubleshooting_status(conversation_history: list[dict]) -> bool | None:
    """Explicit issue status from troubleshooting phase (before closing acceptance)."""
    cutoff = _acceptance_turn_index(conversation_history)
    search = conversation_history[:cutoff] if cutoff is not None else list(conversation_history)
    in_progress = ("trying to reconnect", "installing", "trying it now", "checking now", "one moment")
    for turn in reversed(search):
        if turn.get("speaker") != "customer":
            continue
        text = (turn.get("text") or "").lower()
        if any(w in text for w in CUSTOMER_UNRESOLVED_PHRASES):
            return False
        if any(w in text for w in CUSTOMER_RESOLVED_PHRASES):
            return True
        if any(w in text for w in in_progress):
            return None
    return None


def agent_asks_resolution_check(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    cues = (
        "resolved", "fixed", "working now", "work now", "got result", "issue got",
        "all set", "did that work", "is the printer", "is it connect", "connection work",
        "problem solved", "everything fine", "everything good", "working fine",
        "got it working", "issue resolved", "printer issue",
        "is your issue", "is the issue", "emails syncing", "syncing now", "syncing properly",
    )
    if not any(w in lower for w in cues):
        return False
    return "?" in agent_text or any(w in lower for w in ("tell me", "confirm", "check", "right now", "is the", "did ", "has ", "got "))


def agent_asks_anything_else(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    if any(w in lower for w in ("anything else", "something else", "other question")):
        return True
    if "need anything else" in lower or "anything else i can" in lower:
        return True
    if any(w in lower for w in ("before we end", "before i let you go", "wrap up")):
        return "anything" in lower or "else" in lower
    return False


def agent_asks_repeat(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    return any(w in lower for w in ("come again", "say again", "repeat", "didn't catch", "didn't hear", "pardon", "one more time"))


def agent_asks_satisfaction_check(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    return any(
        w in lower
        for w in (
            "happy with", "satisfied", "clarity about", "got the clarity",
            "hope you are", "are you happy", "good experience", "happy with the conversation",
        )
    )


def agent_said_goodbye(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    return any(
        w in lower
        for w in (
            "thank you for calling", "have a great day", "have a nice day",
            "goodbye", "good bye", "bye bye", "bye for now", "take care",
            "thanks for your time", "nice day",
        )
    )


def agent_said_brief_thanks(agent_text: str) -> bool:
    lower = (agent_text or "").strip().lower().rstrip(".!")
    if lower in ("thank you", "thanks", "thank you so much", "thanks a lot"):
        return True
    return lower.startswith(("thank you", "thanks")) and len(lower.split()) <= 8


def infer_issue_resolved(
    conversation_history: list[dict],
    scenario_context: dict,
    last_agent: str = "",
) -> bool | None:
    if conversation_in_closing_phase(conversation_history):
        return True

    if agent_asks_resolution_check(last_agent):
        status = _latest_customer_troubleshooting_status(conversation_history)
        if status is not None:
            return status
        al = (last_agent or "").lower()
        if any(w in al for w in ("hope", "confirm", "check")) and conversation_history:
            recent_agent = " ".join(
                (t.get("text") or "").lower()
                for t in conversation_history[-6:]
                if t.get("speaker") == "trainee"
            )
            if agent_gave_wrap_up_summary(recent_agent):
                return True
        return None

    status = _latest_customer_troubleshooting_status(conversation_history)
    if status is not None:
        return status

    desc = (scenario_context.get("description") or "").lower()
    if any(w in desc for w in ("cannot", "can't", "unable", "not connect", "issue", "problem")):
        return False
    return None


def reference_matches_agent_step(reference_line: str, last_agent: str) -> bool:
    if not reference_line or not last_agent:
        return False
    rl = reference_line.lower()
    al = last_agent.lower()
    for agent_keys, customer_keys in AGENT_STEP_CUSTOMER_PAIRS:
        if any(k in al for k in agent_keys):
            return any(k in rl for k in customer_keys)
    if any(w in al for w in ("model", "which printer", "what printer", "model number")):
        return any(w in rl for w in ("hp", "laserjet", "model", "m426", "it's a", "printer is"))
    if texts_similar(reference_line, last_agent, threshold=0.35):
        return True
    return False


def agent_announced_escalation_or_handoff(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    return any(
        w in lower
        for w in (
            "escalat", "billing team", "specialist", "senior technician",
            "will review", "will contact", "follow up", "team will", "discrepancy",
        )
    )


def agent_gave_weak_filler(agent_text: str) -> bool:
    lower = (agent_text or "").strip().lower().rstrip(".!?,")
    if not lower:
        return True
    weak = {
        "ok", "okay", "sure", "yes", "show", "ok show", "right", "got it",
        "alright", "fine", "good", "thanks", "thank you", "yep", "yeah",
    }
    return lower in weak or (len(lower.split()) <= 3 and lower in weak)


def reference_is_acknowledgment_beat(reference_line: str) -> bool:
    rl = (reference_line or "").lower()
    return any(
        w in rl
        for w in ("appreciate", "thank", "sounds good", "that's all", "finally", "great,")
    )


def customer_reply_invents_off_timeline_question(text: str, reference_line: str) -> bool:
    """True when Bedrock invents a new customer question not in the script beat."""
    lower = (text or "").lower()
    ref = (reference_line or "").lower()
    if not lower or not ref:
        return False
    invention_markers = (
        "when will", "could you confirm when", "please confirm when",
        "how long will", "how long does", "what is the timeline", "when can i expect",
        "when does the", "when do you think",
    )
    if not any(m in lower for m in invention_markers):
        return False
    return not any(m in ref for m in invention_markers)


def should_advance_to_script_beat(
    reference_line: str,
    last_agent: str,
    success_script: list,
    conversation_history: list[dict],
) -> bool:
    """Use the next scripted customer line — stay on scenario timeline, no invented questions."""
    if not reference_line or not last_agent:
        return False
    if conversation_in_closing_phase(conversation_history):
        return False

    if count_speakers(conversation_history, "customer") == 0:
        trainee_count = count_speakers(conversation_history, "trainee")
        if trainee_count >= 1 and (
            is_welcome_opening(last_agent) or agent_line_fits_intro_response(last_agent)
        ):
            return True

    if agent_asks_resolution_check(last_agent) or agent_asks_repeat(last_agent):
        return False
    if agent_asks_anything_else(last_agent):
        return reference_answers_agent_question(reference_line, last_agent)

    # Asking for the issue → use the next scripted problem/intro beat as-is.
    if agent_asks_for_issue(last_agent) and reference_line:
        return True

    if agent_asked_question(last_agent) and not agent_gave_weak_filler(last_agent):
        if reference_answers_agent_question(reference_line, last_agent):
            return True
        if reference_is_acknowledgment_beat(reference_line):
            return True
        return False

    if agent_announced_escalation_or_handoff(last_agent):
        return True
    if agent_gave_wrap_up_summary(last_agent):
        return True

    # DF_S3: do not skip ahead when the agent left the scripted beat
    if agent_deviated_from_script(success_script, conversation_history):
        return False

    # On-script statement — advance to the matching customer beat
    return True


def agent_on_expected_script_beat(
    last_agent: str,
    success_script: list,
    conversation_history: list[dict],
) -> bool:
    """True when the agent's last line is the scripted beat the timeline expects next."""
    expected_agent = next_trainee_line(
        success_script,
        max(0, count_speakers(conversation_history, "trainee") - 1),
    )
    if not expected_agent:
        return False
    return agent_follows_expected_script(last_agent, expected_agent, "practice")


def needs_contextual_customer_reply(
    reference_line: str | None,
    last_agent: str,
    success_script: list,
    conversation_history: list[dict],
) -> bool:
    """True only when the agent needs a direct answer the next script beat cannot provide."""
    if not last_agent:
        return False
    if agent_asks_repeat(last_agent):
        return True
    # The agent delivered the scripted line, so the next scripted beat IS the answer to it --
    # by construction, whatever the domain. Judging that by keyword table instead (see
    # AGENT_STEP_CUSTOMER_PAIRS, which only covers printers and billing) threw the beat away
    # for every other scenario and regenerated it, so practice drifted off the preview timeline.
    if (reference_line or "").strip() and agent_on_expected_script_beat(
        last_agent, success_script, conversation_history
    ):
        return False
    if agent_asks_for_issue(last_agent) and (reference_line or "").strip():
        return False
    if agent_asks_anything_else(last_agent):
        return not reference_answers_agent_question(reference_line or "", last_agent)
    if agent_asked_question(last_agent) and not agent_gave_weak_filler(last_agent):
        if reference_answers_agent_question(reference_line or "", last_agent):
            return False
        if reference_is_acknowledgment_beat(reference_line or ""):
            return False
        return True
    return False


def _script_reference_customer_text(
    reference_line: str,
    scenario_context: dict,
    conversation_history: list[dict],
) -> str:
    # Keep the exact preview timeline line so practice matches the script.
    text = repair_dangling_text((reference_line or "").strip())
    # Only add an opening introduction when the scripted line lacks one. When it already names
    # the caller, ensure_customer_intro would append the scenario description to it unless the
    # wording happens to hit its problem-word list, which reads as a stray narration on the call.
    last_agent = last_message_from(conversation_history, "trainee") or ""
    if count_speakers(conversation_history, "customer") == 0 and not _customer_self_intro_match(text):
        return ensure_customer_intro(
            scenario_context,
            text,
            0,
            last_agent,
            conversation_history,
        )
    # The scripted line may greet an agent name this trainee never gave; correct only that.
    return adapt_customer_line_to_conversation(text, conversation_history, last_agent)


def reference_answers_agent_question(reference_line: str, last_agent: str) -> bool:
    if not agent_asked_question(last_agent):
        return True
    rl = (reference_line or "").lower()
    if agent_asks_resolution_check(last_agent):
        return any(
            w in rl
            for w in (
                "yes", "no", "not yet", "still not", "still unable", "working now",
                "it's working", "it works", "resolved", "unable to", "not connect",
                "not working", "not resolved",
            )
        )
    if agent_asks_anything_else(last_agent):
        return any(w in rl for w in ("no", "that's all", "nothing else", "everything i"))
    if agent_asks_repeat(last_agent):
        return True
    return reference_matches_agent_step(reference_line or "", last_agent)


def _customer_already_confirmed_resolution(conversation_history: list[dict]) -> bool:
    for turn in reversed(conversation_history):
        if turn.get("speaker") != "customer":
            continue
        text = (turn.get("text") or "").lower()
        if any(w in text for w in CUSTOMER_UNRESOLVED_PHRASES):
            return False
        if any(
            w in text
            for w in (
                "working now", "working fine", "it's working", "yes, it", "yes it",
                "printer is working", "all set now", "seems to be working",
            )
        ):
            return True
    return False


def agent_closing_the_call(agent_text: str) -> bool:
    lower = (agent_text or "").lower()
    if agent_said_goodbye(agent_text):
        return True
    if "nothing else" in lower and any(w in lower for w in ("thank", "welcome")):
        return True
    if agent_said_brief_thanks(agent_text) and any(w in lower for w in ("nothing else", "that's all", "all set")):
        return True
    return False


def _last_customer_substantive_info(conversation_history: list[dict]) -> str | None:
    skip_starts = ("thank", "okay", "sure", "yes", "no,", "no ")
    for turn in reversed(conversation_history):
        if turn.get("speaker") != "customer":
            continue
        text = (turn.get("text") or "").strip()
        if len(text) > 12 and not any(text.lower().startswith(s) for s in skip_starts):
            return text
    return None


def build_closing_phase_customer_reply(
    last_agent: str,
    conversation_history: list[dict],
    scenario_context: dict,
) -> str:
    """Stay consistent after customer accepted escalation/summary — never regress to unresolved complaints."""
    if agent_said_goodbye(last_agent) or agent_closing_the_call(last_agent):
        return shorten_speech_text("Thank you. Have a great day!", "customer")
    if agent_asks_satisfaction_check(last_agent) or agent_asks_resolution_check(last_agent):
        desc = (scenario_context.get("description") or "").lower()
        if any(w in desc for w in ("escalat", "billing", "dispute")):
            return shorten_speech_text(
                "Yes — I'm satisfied with the escalation. I'll wait for your team's follow-up. Thank you.",
                "customer",
            )
        return shorten_speech_text(
            "Yes, I'm satisfied with how you handled this. Thank you for your help.", "customer"
        )
    if agent_asks_anything_else(last_agent):
        return shorten_speech_text("No, that's everything. Thanks for your help today.", "customer")
    if agent_said_brief_thanks(last_agent):
        return shorten_speech_text("You're welcome. Thank you — have a great day!", "customer")
    return shorten_speech_text("Thank you for your help today.", "customer")


CUSTOMER_REGRESSION_MARKERS = (
    "still waiting", "not resolved", "billing error to be", "still not fixed",
    "hasn't been resolved", "not been resolved", "still unresolved",
)


def enforce_closing_customer_reply(
    reply: str,
    conversation_history: list[dict],
    last_agent: str,
    scenario_context: dict,
) -> str:
    """Prevent hallucination loops — customer cannot contradict closure they already accepted."""
    if not conversation_in_closing_phase(conversation_history):
        if not agent_said_goodbye(last_agent):
            return reply
    lower = (reply or "").lower()
    if any(m in lower for m in CUSTOMER_REGRESSION_MARKERS):
        return build_closing_phase_customer_reply(last_agent, conversation_history, scenario_context)
    if conversation_in_closing_phase(conversation_history) and agent_said_goodbye(last_agent):
        if "still" in lower or "waiting" in lower or "not resolved" in lower:
            return shorten_speech_text("Thank you. Have a great day!", "customer")
    return reply


def build_contextual_agent_followup_response(
    last_agent: str,
    conversation_history: list[dict],
    scenario_context: dict,
) -> str | None:
    """Answer what the agent actually asked — used when script lines are wrong or exhausted."""
    if not last_agent:
        return None

    if conversation_in_closing_phase(conversation_history):
        return build_closing_phase_customer_reply(last_agent, conversation_history, scenario_context)

    if agent_asks_repeat(last_agent):
        prev = _last_customer_substantive_info(conversation_history)
        if prev:
            return shorten_speech_text(f"As I mentioned, {prev}", "customer")
        return shorten_speech_text("Sure — what would you like me to repeat?", "customer")

    if agent_asks_resolution_check(last_agent):
        if conversation_in_closing_phase(conversation_history):
            return shorten_speech_text(
                "Yes — the escalation is in place. I'll wait for your team's follow-up. Thank you.", "customer"
            )
        return build_resolution_check_reply(conversation_history, scenario_context, last_agent)

    if agent_asks_anything_else(last_agent):
        return shorten_speech_text("No, that's everything. Thanks for your help today.", "customer")

    if agent_closing_the_call(last_agent):
        return shorten_speech_text("Thank you. Have a great day!", "customer")

    if agent_said_goodbye(last_agent):
        return shorten_speech_text("Thank you. Have a great day!", "customer")

    if agent_said_brief_thanks(last_agent):
        return shorten_speech_text("You're welcome. Thanks for your help today.", "customer")

    # A comprehension check, not merely a question containing "understand": the empathy opener
    # "I understand your urgency, Lily. May I have your student ID?" used to land here and be
    # answered "Yes, that makes sense." instead of the student ID the script asks for.
    if "?" in last_agent and _agent_asks_comprehension_check(last_agent):
        return shorten_speech_text("Yes, that makes sense. I'll try that.", "customer")

    return None


_COMPREHENSION_CHECK_PAT = re.compile(
    r"(?:make[s]?\s+sense"
    r"|is\s+(?:that|this|it)\s+clear"
    r"|are\s+you\s+(?:clear|with\s+me|following)"
    r"|do(?:es)?\s+(?:you|that|this)\s+(?:understand|follow)"
    r"|did\s+you\s+(?:understand|follow|get)\s+(?:that|this)"
    r"|any\s+questions)",
    re.I,
)


def _agent_asks_comprehension_check(agent_text: str) -> bool:
    """True for 'Does that make sense?' / 'Is that clear?' — not for 'I understand your ...'."""
    return bool(_COMPREHENSION_CHECK_PAT.search(agent_text or ""))


def conversation_should_complete(last_agent: str, customer_reply: str) -> bool:
    if agent_said_goodbye(last_agent) or agent_closing_the_call(last_agent):
        return True
    lower = (last_agent or "").lower()
    if any(w in lower for w in ("bye bye", "thanks for your time", "have a nice day")):
        return True
    if agent_asks_anything_else(last_agent):
        cl = (customer_reply or "").lower()
        return any(p in cl for p in ("that's all", "no,", "nothing else", "everything", "thanks again"))
    if agent_said_brief_thanks(last_agent):
        cl = (customer_reply or "").lower()
        return any(p in cl for p in ("welcome", "thank", "great day", "you too", "working fine"))
    return False


def agent_deviated_from_script(
    success_script: list[Any],
    conversation_history: list[dict],
) -> bool:
    trainee_count = count_speakers(conversation_history, "trainee")
    if trainee_count <= 0:
        return False
    last_agent = last_message_from(conversation_history, "trainee") or ""
    expected = next_trainee_line(success_script, trainee_count - 1)
    if not expected or not last_agent:
        return False
    if texts_similar(last_agent, expected):
        return False
    if agent_thematically_aligned(expected, last_agent):
        return False
    el = expected.lower()
    al = last_agent.lower()
    if any(p in el for p in ("how can i help", "how may i", "verify", "check", "model")):
        if any(p in al for p in ("how can i help", "how may i", "verify", "check", "model")):
            return False
    if agent_gave_action_instruction(last_agent) and agent_thematically_aligned(expected, last_agent):
        return False
    return True


def should_use_reference_customer_line(
    reference_line: str,
    last_agent: str,
    success_script: list,
    conversation_history: list[dict],
) -> bool:
    if not reference_line:
        return False
    return not needs_contextual_customer_reply(
        reference_line, last_agent, success_script, conversation_history
    )


def _finalize_customer_reply(
    text: str,
    source: str,
    conversation_history: list[dict],
    last_agent: str,
    scenario_context: dict,
) -> tuple[str, str]:
    # Exact preview timeline beat — never rewrite (prevents jumping to later acks).
    if source == "reference":
        return repair_dangling_text((text or "").strip()), source
    if conversation_in_closing_phase(conversation_history):
        lower = (text or "").lower()
        if source == "fallback" or any(m in lower for m in CUSTOMER_REGRESSION_MARKERS):
            text = build_closing_phase_customer_reply(last_agent, conversation_history, scenario_context)
    text = enforce_closing_customer_reply(text, conversation_history, last_agent, scenario_context)
    text = adapt_customer_line_to_conversation(text, conversation_history, last_agent)
    return repair_dangling_text(shorten_speech_text(text, "customer")), source


def build_off_script_customer_nudge(
    last_agent: str,
    expected_agent_line: str | None = None,
    description: str = "",
) -> str:
    """Customer redirect when the agent leaves the scripted beat."""
    expected = (expected_agent_line or "").strip()
    if expected and is_verification_question(expected):
        return shorten_speech_text(
            "Sorry — I'm not sure I follow. Were you going to ask me a security question?",
            "customer",
        )
    if agent_asked_question(last_agent):
        return shorten_speech_text(
            "I'm not sure what you mean — can we get back to my issue?",
            "customer",
        )
    desc = (description or "").strip()
    if desc:
        short = desc.split(".")[0][:80].rstrip(".")
        return shorten_speech_text(
            f"I appreciate that, but I still need help with {short}.",
            "customer",
        )
    return shorten_speech_text(
        "I'm not sure I follow — could you continue helping me with my issue?",
        "customer",
    )


def resolve_customer_turn_text(
    reference_line: str,
    last_agent: str,
    conversation_history: list[dict],
    scenario_context: dict,
    success_script: list,
) -> tuple[str, str]:
    """Return (customer_text, source) — source is reference, contextual, or fallback."""
    # On-script agent + a scripted beat left: replay the beat verbatim so practice shows exactly
    # the preview timeline. Checked before the closing/resolution branches below, which would
    # otherwise substitute a generated line for the script's own closing beats.
    if (reference_line or "").strip() and agent_on_expected_script_beat(
        last_agent, success_script, conversation_history
    ):
        text = _script_reference_customer_text(reference_line, scenario_context, conversation_history)
        return _finalize_customer_reply(
            text, "reference", conversation_history, last_agent, scenario_context
        )

    if conversation_in_closing_phase(conversation_history):
        reply = build_closing_phase_customer_reply(last_agent, conversation_history, scenario_context)
        return _finalize_customer_reply(reply, "contextual", conversation_history, last_agent, scenario_context)

    if agent_asks_resolution_check(last_agent):
        reply = build_resolution_check_reply(conversation_history, scenario_context, last_agent)
        return _finalize_customer_reply(reply, "contextual", conversation_history, last_agent, scenario_context)

    contextual_needed = needs_contextual_customer_reply(
        reference_line, last_agent, success_script, conversation_history
    )
    expected_agent = next_trainee_line(
        success_script,
        max(0, count_speakers(conversation_history, "trainee") - 1),
    )
    on_script = (
        not expected_agent
        or agent_follows_expected_script(last_agent, expected_agent, "practice")
    )

    # Off-script agent — never dump the next scripted customer beat (e.g. pet name for MCP).
    if not on_script:
        followup = build_contextual_agent_followup_response(
            last_agent, conversation_history, scenario_context
        )
        if followup:
            return _finalize_customer_reply(
                followup, "contextual", conversation_history, last_agent, scenario_context
            )
        nudge = build_off_script_customer_nudge(
            last_agent,
            expected_agent,
            scenario_context.get("description") or "",
        )
        return _finalize_customer_reply(
            nudge, "contextual", conversation_history, last_agent, scenario_context
        )

    if reference_line and not contextual_needed:
        text = _script_reference_customer_text(reference_line, scenario_context, conversation_history)
        return _finalize_customer_reply(text, "reference", conversation_history, last_agent, scenario_context)

    if contextual_needed:
        followup = build_contextual_agent_followup_response(last_agent, conversation_history, scenario_context)
        if followup:
            return _finalize_customer_reply(followup, "contextual", conversation_history, last_agent, scenario_context)

    if reference_line:
        text = _script_reference_customer_text(reference_line, scenario_context, conversation_history)
        return _finalize_customer_reply(text, "reference", conversation_history, last_agent, scenario_context)

    text = build_contextual_customer_fallback(
        reference_line,
        last_agent,
        conversation_history,
        scenario_context,
        deviated=False,
    )
    return _finalize_customer_reply(text, "fallback", conversation_history, last_agent, scenario_context)


def upcoming_customer_beats(success_script: list[Any], customer_count: int, limit: int = 3) -> list[str]:
    lines = canonical_customer_lines(success_script)
    return lines[customer_count : customer_count + limit]


def build_contextual_customer_fallback(
    reference_line: str,
    last_agent: str,
    conversation_history: list[dict],
    scenario_context: dict,
    deviated: bool = False,
) -> str:
    """Rule-based customer reply when Bedrock is unavailable."""
    script = scenario_context.get("success_script", [])

    if reference_line and not needs_contextual_customer_reply(
        reference_line, last_agent, script, conversation_history
    ):
        text = _script_reference_customer_text(reference_line, scenario_context, conversation_history)
        return text

    followup = build_contextual_agent_followup_response(last_agent, conversation_history, scenario_context)
    if followup:
        return followup

    ref = (reference_line or "").strip()
    if ref:
        return shorten_speech_text(ref, "customer")

    agent_lower = (last_agent or "").lower()
    if deviated and any(w in agent_lower for w in ("sorry", "apologize", "understand")):
        desc = (scenario_context.get("description") or "")[:120]
        if desc:
            return shorten_speech_text(f"I appreciate that, but I still need help with {desc.rstrip('.')}.", "customer")

    return shorten_speech_text("Okay — I'll wait for your update.", "customer")


def last_message_from(conversation: list[dict], speaker: str) -> str | None:
    for turn in reversed(conversation):
        if turn.get("speaker") == speaker:
            return turn.get("text")
    return None


def coalesce_script_turns(script: list[Any]) -> list[dict]:
    result: list[dict] = []
    for turn in script:
        speaker = turn.get("speaker") if isinstance(turn, dict) else getattr(turn, "speaker", None)
        text = turn.get("text") if isinstance(turn, dict) else getattr(turn, "text", "")
        text = repair_dangling_text((text or "").strip())
        if not text or not speaker:
            continue
        if result and result[-1]["speaker"] == speaker:
            result[-1]["text"] = f"{result[-1]['text']} {text}".strip()
            result[-1]["text"] = repair_dangling_text(result[-1]["text"])
        else:
            result.append({"speaker": speaker, "text": text})
    return result


def alternating_script_turns(script: list[Any]) -> list[dict]:
    merged = coalesce_script_turns(script)
    return ensure_agent_opens_first(merged)


def ensure_agent_opens_first(script: list[dict]) -> list[dict]:
    """Agent (trainee) must greet first — customer lines follow, never lead the call."""
    if not script:
        return script
    if script[0].get("speaker") == "trainee":
        return script
    agent_i = next((i for i, t in enumerate(script) if t.get("speaker") == "trainee"), None)
    if agent_i is None or agent_i == 0:
        return script
    agent_turn = script[agent_i]
    before = script[:agent_i]
    after = script[agent_i + 1 :]
    return [agent_turn] + before + after


def extract_agent_name_from_conversation(conversation_history: list[dict]) -> str | None:
    for turn in conversation_history:
        if turn.get("speaker") != "trainee":
            continue
        text = turn.get("text") or ""
        match = re.search(r"(?:my name is|this is|i'm|i am)\s+([A-Z][a-z]+)", text, re.I)
        if match:
            name = match.group(1)
            if name.lower() not in INVALID_NAME_WORDS:
                return name
    return None


def adapt_customer_line_to_conversation(
    text: str,
    conversation_history: list[dict],
    last_agent: str = "",
) -> str:
    """Only use the agent's name if they introduced themselves — never treat customer as agent."""
    line = (text or "").strip()
    if not line:
        return line
    customer_name = extract_customer_name_from_script(conversation_history) or ""
    customer_first = customer_name.split()[0] if customer_name else ""
    agent_name = extract_agent_name_from_conversation(conversation_history)
    if not agent_name and last_agent:
        match = re.search(r"(?:my name is|this is|i'm|i am)\s+([A-Z][a-z]+)", last_agent, re.I)
        if match and match.group(1).lower() not in INVALID_NAME_WORDS:
            agent_name = match.group(1)
    if agent_name and customer_first and agent_name.lower() == customer_first.lower():
        agent_name = None
    if not agent_name:
        # Only strip Capitalized addressee names — never "for"/"your" (re.I broke thank-you lines).
        line = re.sub(
            r"^(hi|hello),?\s+(?!this\b|my\b)([A-Z][a-z]+)[,.]?\s*",
            r"\1, ",
            line,
        )
        line = re.sub(r"\b[Tt]hanks,?\s+([A-Z][a-z]+)\b", "Thanks", line)
        line = re.sub(r"\b[Tt]hank you,?\s+([A-Z][a-z]+)\b", "Thank you", line)
        line = re.sub(
            r"\b[Aa]ppreciate your help,?\s+([A-Z][a-z]+)\b",
            "appreciate your help",
            line,
        )
        # Strip customer's own name from thank-you lines ("That sounds great, Lisa — thank...")
        if customer_first:
            line = re.sub(
                rf"\b(that sounds great),?\s+{re.escape(customer_first)}\b",
                r"\1",
                line,
                flags=re.I,
            )
        return line.strip()
    directed = re.search(r"^(?:hi|hello|thanks|thank you),?\s+([A-Z][a-z]+)", line, re.I)
    if directed and directed.group(1) != agent_name:
        # Do not rewrite "this is Lisa" style intros — only greeting/thanks address
        if customer_first and directed.group(1).lower() == customer_first.lower():
            if re.search(r"\b(this is|my name is)\b", line, re.I):
                return line.strip()
        line = line.replace(directed.group(1), agent_name, 1)
    if customer_first:
        line = re.sub(
            rf"\b(that sounds great),?\s+{re.escape(customer_first)}\b",
            rf"\1, {agent_name}",
            line,
            flags=re.I,
        )
    return line.strip()


def first_trainee_turn(success_script: list[Any]) -> Any | None:
    for turn in success_script:
        speaker = turn.get("speaker") if isinstance(turn, dict) else turn.speaker
        if speaker == "trainee":
            return turn
    return None


def last_speaker(conversation: list[dict]) -> str | None:
    if not conversation:
        return None
    return conversation[-1].get("speaker")


def conversation_state_prompt_block(
    conversation_history: list[dict],
    scenario_context: dict,
    last_agent: str = "",
) -> str:
    """Summarize call phase so Bedrock answers consistently with conversation memory."""
    if conversation_in_closing_phase(conversation_history):
        return (
            "CALL PHASE: Closing — you already accepted the escalation/summary (e.g. 'sounds good', "
            "'that's all'). Stay satisfied — do NOT say you are still waiting or unresolved. "
            "If they ask if you're happy or if it's resolved, say YES and thank them. "
            "If they say goodbye, reply warmly and end the call."
        )
    last_cust = (last_message_from(conversation_history, "customer") or "").lower()
    if any(w in last_cust for w in ("trying to reconnect", "done the reset", "installing")):
        return (
            "CALL PHASE: You are still completing a troubleshooting step (reset/reconnect/install). "
            "If asked if resolved, say not yet — you are still trying. Never say 'I'm not sure'."
        )
    status = _latest_customer_troubleshooting_status(conversation_history)
    if status is False:
        return (
            "CALL PHASE: Active troubleshooting — you already reported the issue is NOT fixed yet. "
            "If asked whether it is resolved, say not yet clearly (never 'I'm not sure')."
        )
    if status is True:
        return "CALL PHASE: You previously indicated the issue is working/resolved."
    if agent_asks_resolution_check(last_agent):
        resolved = infer_issue_resolved(conversation_history, scenario_context, last_agent)
        if resolved is True:
            return "CALL PHASE: Based on the full call, treat the issue as resolved when answering."
        if resolved is False:
            return "CALL PHASE: Based on the full call, the issue is still not resolved."
    agent_name = extract_agent_name_from_conversation(conversation_history)
    if not agent_name and last_agent:
        match = re.search(r"(?:my name is|this is|i'm|i am)\s+([A-Z][a-z]+)", last_agent, re.I)
        if match and match.group(1).lower() not in INVALID_NAME_WORDS:
            agent_name = match.group(1)
    name_rule = ""
    if agent_name:
        name_rule = f" The agent's name is {agent_name} — use it only because they introduced themselves."
    else:
        name_rule = " Do NOT use the agent's name — they have not introduced themselves yet."
    return (
        f"Remember the ENTIRE conversation above — do not contradict earlier facts.{name_rule} "
        "Follow the scenario timeline beats; respond to what the agent JUST said."
    )


def facts_prompt_block(facts: dict[str, str]) -> str:
    if not facts:
        return "Use only details already mentioned in the conversation. Do not invent account numbers, card digits, or amounts."
    lines = ["Use ONLY these established facts (do not change or invent new numbers):"]
    if facts.get("customer_name"):
        lines.append(f"- Customer name: {facts['customer_name']}")
    if facts.get("card_last4"):
        lines.append(f"- Card last four digits: {facts['card_last4']}")
    if facts.get("email"):
        lines.append(f"- Email: {facts['email']}")
    if facts.get("phone"):
        lines.append(f"- Phone: {facts['phone']}")
    if facts.get("amount"):
        lines.append(f"- Amount: {facts['amount']}")
    if facts.get("account_ref"):
        lines.append(f"- Account/reference: {facts['account_ref']}")
    return "\n".join(lines)


def ensure_customer_intro(
    scenario_context: dict,
    text: str,
    customer_count: int = 0,
    last_agent: str = "",
    conversation_history: list[dict] | None = None,
) -> str:
    history = conversation_history or []
    line = (text or "").strip()

    def _finish_customer_line(result: str) -> str:
        adapted = adapt_customer_line_to_conversation(result, history, last_agent)
        # Keep scripted lines exact so practice matches the preview timeline.
        if customer_count > 0:
            return repair_dangling_text(adapted)
        # First turn: if the script already has a full greeting+issue line, keep it.
        if customer_stated_problem(adapted) or (
            any(w in adapted.lower() for w in ("this is", "my name is"))
            and len(adapted.split()) >= 8
        ):
            return repair_dangling_text(adapted)
        return shorten_speech_text(adapted, "customer")

    if customer_count > 0 and line:
        return _finish_customer_line(line)

    agent_lower = (last_agent or "").lower()
    if any(w in agent_lower for w in ("model", "which", "what is", "account number", "email", "serial")):
        return _finish_customer_line(line)
    persona = (scenario_context.get("customer_persona") or "").strip()
    desc = (scenario_context.get("description") or "").strip()
    script = scenario_context.get("success_script") or []
    facts = extract_scenario_facts(script, persona, desc)

    name = facts.get("customer_name") or _parse_persona_name(persona) or ""

    lower = line.lower()
    has_greeting = any(w in lower for w in ["hi", "hello", "hey", "this is", "my name is", "i'm ", "i am "])
    has_problem = any(
        w in lower
        for w in [
            "problem", "issue", "can't", "cannot", "not working", "offline",
            "activate", "charged", "frustrated", "help", "holding", "stuck", "broken",
            "billing", "renewal", "account", "glitch", "escalat",
        ]
    )
    if line and has_greeting and (has_problem or customer_stated_problem(line)):
        return _finish_customer_line(line)

    problem = desc
    if len(problem) > 180:
        problem = problem[:177].rsplit(" ", 1)[0] + "..."

    if line:
        if is_intro_only_customer(line):
            return _finish_customer_line(build_customer_problem_statement(desc, persona, line))
        if name and name.lower() not in lower:
            if has_greeting:
                if "this is" in lower or "my name" in lower:
                    return _finish_customer_line(line)
                agent_name = _extract_agent_name_from_script(script)
                if (
                    agent_name
                    and name
                    and agent_name.lower() == name.split()[0].lower()
                ):
                    agent_name = None
                if agent_name:
                    rest = re.sub(
                        rf"^(hi|hello|hey)(?:\s+{re.escape(agent_name)})?[,!\s]+",
                        "",
                        line,
                        flags=re.I,
                    ).strip()
                else:
                    rest = re.sub(
                        r"^(hi|hello|hey)(?:\s+[A-Z][a-z]+)?[,!\s]+",
                        "",
                        line,
                        flags=re.I,
                    ).strip()
                if rest:
                    rest = rest[0].upper() + rest[1:]
                greet = f"Hi {agent_name}" if agent_name else "Hi"
                return _finish_customer_line(f"{greet}, this is {name}. {rest}".strip())
            return _finish_customer_line(f"Hi, this is {name}. {line}")
        if not has_problem and problem:
            return _finish_customer_line(f"{line} {problem}".strip())
        return _finish_customer_line(line)

    if name and problem:
        result = f"Hi, this is {name}. {shorten_speech_text(problem, 'customer')}"
    elif problem:
        result = f"Hi. {shorten_speech_text(problem, 'customer')}"
    else:
        result = line or "Hi, I need some help with my account."
    return _finish_customer_line(result)
