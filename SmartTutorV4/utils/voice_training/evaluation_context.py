"""Contextual (paraphrase-tolerant) evaluation helpers."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from utils.voice_training.schemas import ConversationTurn, SOPActivity
from utils.voice_training.script_turns import trainee_lines_from_script

SOP_PATTERNS: dict[str, list[str]] = {
    "sop-1": [
        "verify", "confirm", "validate", "authentication", "authenticate",
        "full name", "your name", "email", "account number", "order number",
        "last four", "identity", "who am i speaking", "account holder",
        "date of birth", "dob", "birth date", "born", "may i have",
    ],
    "sop-2": [
        "record", "recorded", "recording", "disclosure", "quality purposes",
        "monitored", "training purposes", "this call may", "call may be recorded",
    ],
    "sop-3": [
        "review", "check", "investigate", "diagnostic", "history", "look into",
        "pull up", "troubleshoot", "walk you through", "let me see", "account details",
        "billing history", "system shows", "pending activation", "processing",
        "what that means", "few hours", "estimated",
    ],
    "sop-4": [
        "refund", "credit", "resolve", "fix", "schedule", "technician", "activate",
        "activation", "activated", "reset", "process", "submit", "escalate", "ticket",
        "replacement", "update your", "apply", "manually activ", "hold while",
        "one moment", "checking", "enjoy your services", "all set",
    ],
    "sop-5": [
        "summarize", "summary", "recap", "to summarize", "anything else",
        "is there anything", "before we end", "to confirm", "we've", "next steps",
        "welcome", "glad we could help", "wonderful day",
    ],
}

POLITE_MARKERS = ["please", "thank you", "thanks", "appreciate", "sorry", "apologize", "glad to"]
EMPATHY_MARKERS = [
    "understand", "frustrating", "concern", "hear you", "i'm sorry", "that must be",
    "help you", "i know this", "inconvenience", "absolutely", "of course",
    "appreciate", "urgency", "sorted out",
]
PROFESSIONAL_ANTI = ["yeah", "nah", "gonna", "wanna", "lol", "uh huh", "whatever"]

PROBLEM_MARKERS = (
    "can't", "cannot", "not connect", "not working", "isn't working", "isnt working",
    "issue", "problem", "frustrated", "offline", "broken", "error", "failed",
    "charge", "billing", "activat", "not activating", "isn't activating", "isnt activating",
    "pending", "invalid credentials", "credentials", "sorted out", "urgent",
)

STT_CORRECTIONS: list[tuple[str, str]] = [
    (r"\bbeyond hold wildlife process\b", "please hold while i process"),
    (r"\bwildlife process\b", "while i process"),
    (r"\bthismated\b", "estimated"),
    (r"\bhow can i as you\b", "how can i assist you"),
    (r"\bmay have your\b", "may i have your"),
    (r"\blet me take you to\b", "let me take the next step for"),
    (r"\bmanually activated for you right now\b", "manually activate it for you now"),
    (r"\bwhen I process\b", "while i process"),
    (r"\bJain\b", "Jane"),
    (r"\bjaan\b", "john"),
    (r"\bget the sorted\b", "get this sorted"),
    (r"\btechnician today\b", "issue today"),
]


def dedupe_repeated_speech(text: str) -> str:
    """Remove exact duplicated phrases from speech-to-text (mic restart echo)."""
    t = (text or "").strip()
    if len(t) < 16:
        return t
    half = len(t) // 2
    a, b = t[:half].strip(), t[half:].strip()
    if a and b and a.lower() == b.lower():
        return a
    words = t.split()
    if len(words) >= 8:
        mid = len(words) // 2
        first = " ".join(words[:mid]).lower()
        second = " ".join(words[mid:]).lower()
        if SequenceMatcher(None, first, second).ratio() > 0.88:
            return " ".join(words[:mid])
    return t


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def _is_word_char(ch: str) -> bool:
    """Approx. ``\\w`` for email-token scanning (letters, digits, underscore)."""
    return ch.isalnum() or ch == "_"


def _extract_email_like_tokens(text: str) -> set[str]:
    """
    Extract ``local@domain.tld`` tokens with a linear scan.
    Matches former regex ``[\\w.+-]+@[\\w-]+\\.[a-z]{2,}`` (IGNORECASE).
    """
    if not text or "@" not in text:
        return set()
    found: set[str] = set()
    n = len(text)
    i = 0
    while i < n:
        at = text.find("@", i)
        if at < 0:
            break
        # Local part: [\\w.+-]+ immediately before @
        j = at - 1
        while j >= 0 and (_is_word_char(text[j]) or text[j] in ".+-"):
            j -= 1
        local_start = j + 1
        if local_start >= at:
            i = at + 1
            continue
        # Domain label: [\\w-]+
        k = at + 1
        while k < n and (_is_word_char(text[k]) or text[k] == "-"):
            k += 1
        if k == at + 1:
            i = at + 1
            continue
        if k >= n or text[k] != ".":
            i = at + 1
            continue
        # TLD: [a-z]{2,} with IGNORECASE
        tld_start = k + 1
        t = tld_start
        while t < n and text[t].isalpha():
            t += 1
        if t - tld_start < 2:
            i = at + 1
            continue
        found.add(text[local_start:t])
        i = t
    return found


def _fact_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    tokens.update(re.findall(r"\b\d{4}\b", text))
    tokens.update(re.findall(r"\$[\d,.]+", text))
    tokens.update(_extract_email_like_tokens(text))
    return tokens


def normalize_stt_text(text: str) -> str:
    """Light cleanup for common speech-to-text garbling before scoring."""
    cleaned = dedupe_repeated_speech((text or "").strip())
    if not cleaned:
        return cleaned
    lower = cleaned.lower()
    for pattern, replacement in STT_CORRECTIONS:
        lower = re.sub(pattern, replacement, lower, flags=re.I)
    return lower


def contextual_similarity(actual: str, reference: str) -> float:
    """Paraphrase-tolerant match — exact wording is not required."""
    actual_n = normalize_stt_text(actual)
    reference_n = (reference or "").strip().lower()
    if not actual_n or not reference_n:
        return 0.0
    seq = SequenceMatcher(None, actual_n, reference_n).ratio()
    ta, tb = _tokenize(actual_n), _tokenize(reference_n)
    if not tb:
        return max(seq, 0.45)
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    ref_keywords = {w for w in tb if len(w) > 3}
    keyword_hit = len(ta & ref_keywords) / max(len(ref_keywords), 1)
    overlap_ratio = len(ta & tb) / max(len(tb) * 0.35, 1)
    fa, fr = _fact_tokens(actual_n), _fact_tokens(reference_n)
    fact_hit = len(fa & fr) / max(len(fr), 1) if fr else 1.0
    base = 0.12 * seq + 0.28 * jaccard + 0.22 * keyword_hit + 0.23 * min(1.0, overlap_ratio)
    return min(1.0, base + 0.15 * fact_hit)


def semantic_intent_score(agent_text: str, reference: str, customer_msg: str) -> float:
    """Score whether the agent reply shows the right intent in their own words."""
    lower = normalize_stt_text(agent_text)
    score = 0.0
    if any(m in lower for m in EMPATHY_MARKERS):
        score += 0.22
    if any(m in lower for m in POLITE_MARKERS):
        score += 0.12
    ref_tokens = {w for w in _tokenize(reference) if len(w) > 3}
    agent_tokens = _tokenize(lower)
    if ref_tokens:
        hit = len(agent_tokens & ref_tokens) / max(len(ref_tokens) * 0.35, 1)
        score += 0.28 * min(1.0, hit)
    actions = {
        "check", "verify", "help", "look", "send", "fix", "activate", "review",
        "confirm", "hold", "moment", "account", "email", "name", "sorry", "understand",
        "resolve", "escalate", "refund", "credit", "steps", "information", "birth",
        "processing", "pending", "manually", "greeting", "assist", "calling",
    }
    if any(a in lower for a in actions):
        score += 0.18
    if customer_msg.strip() and addresses_customer_message(agent_text, customer_msg) >= 0.35:
        score += 0.20
    return min(1.0, score)


def addresses_customer_message(agent_text: str, customer_text: str) -> float:
    if not customer_text.strip():
        return 0.5
    agent_n = normalize_stt_text(agent_text)
    customer_n = customer_text.lower()
    ca, cc = _tokenize(agent_n), _tokenize(customer_n)
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "i", "you", "my", "your", "to",
        "and", "or", "it", "that", "this", "for", "on", "with", "have", "be", "can",
        "not", "just", "hi", "hello", "yeah", "please", "soon", "need",
    }
    cc = {w for w in cc if w not in stop and len(w) > 2}
    overlap = len(ca & cc) / len(cc) if cc else 0.0
    base = min(1.0, overlap + (0.2 if any(m in agent_n for m in EMPATHY_MARKERS) else 0))

    verification_followup = any(
        p in agent_n
        for p in (
            "account number", "date of birth", "dob", "verify", "confirm your",
            "may i have", "can i have", "order number", "email", "identify",
            "full name", "born", "birth",
        )
    )
    customer_shared_info = any(
        p in customer_n
        for p in ("account", "number", "born", "birth", "email", "@", "sure", "it's", "smith", "activat")
    )
    if verification_followup and customer_shared_info:
        base = max(base, 0.58)

    if any(p in customer_n for p in PROBLEM_MARKERS) and any(
        p in agent_n for p in ("sorry", "understand", "help", "urgent", "appreciate", "pending", "activat", "process")
    ):
        base = max(base, 0.55)

    if any(p in agent_n for p in ("hold", "moment", "checking", "processing", "one moment", "bear with", "while i")):
        if any(p in customer_n for p in ("thank", "great", "okay", "sure", "yes", "please")):
            base = max(base, 0.52)

    if any(p in agent_n for p in ("activat", "active", "resolved", "all set", "enjoy your")):
        base = max(base, 0.5)

    if "what does that mean" in customer_n or "speed it up" in customer_n or "clarify" in customer_n:
        if any(p in agent_n for p in ("means", "processing", "hours", "manually", "hold", "activate", "few")):
            base = max(base, 0.55)

    return base


def sop_patterns_for_activity(act: SOPActivity) -> list[str]:
    """Patterns for matching SOP steps — derived from each activity's name and description."""
    name = (act.name or "").lower()
    desc = (act.description or "").lower()
    blob = f"{name} {desc}"

    if any(w in blob for w in ("greet", "welcome", "opening")):
        return [
            "how can i help", "how may i help", "thank you for calling", "good morning",
            "this is", "how can i assist", "how may i assist",
        ]
    if any(w in blob for w in ("restart", "reboot", "power cycle", "power-cycle")):
        return [
            "restart", "reboot", "power off", "power on", "unplug", "plug back",
            "router", "printer again", "both devices", "turn off and on",
        ]
    if any(w in blob for w in ("network connection", "check network", "wifi", "wi-fi", "signal", "within range")):
        return [
            "wifi", "wi-fi", "network", "signal", "range", "router", "connect to",
            "ssid", "within range", "next to the router",
        ]
    if "model" in blob and any(w in blob for w in ("printer", "verify", "confirm", "collect")):
        return [
            "model", "serial", "which printer", "what model", "printer model",
            "may i have", "could you please", "provide",
        ]
    if any(w in blob for w in (
        "identify issue", "identify the issue", "problem identification",
        "specific problem", "identifies the", "identifies the specific",
    )):
        return [
            "printer", "connect", "network", "not connect", "issue", "problem",
            "what seems", "understand", "facing", "help you with", "frustrat",
            "sorted", "troubleshoot", "connection",
        ]
    if any(w in blob for w in ("identify customer", "identify caller", "who am i speaking", "who am i")):
        return [
            "may i have", "account number", "your name", "who am i speaking",
            "order number", "email address", "verify your name",
        ]
    if any(w in blob for w in ("verify", "verification", "identity", "authenticate")):
        patterns = list(dict.fromkeys(SOP_PATTERNS["sop-1"] + [
            "date of birth", "dob", "birth", "born",
            "first pet", "maiden", "security question", "what is the name",
            "what's the name", "confirm your identity", "verified your",
            "thank you",
        ]))
        if any(w in blob for w in ("model", "serial", "printer", "network", "equipment", "information")):
            patterns.extend([
                "model", "serial", "printer", "network", "provide", "could you please",
                "may i have", "collect", "details", "name your",
            ])
        return list(dict.fromkeys(patterns))
    if any(w in blob for w in ("record", "disclosure", "consent", "monitor")):
        return SOP_PATTERNS["sop-2"]
    if any(w in blob for w in ("empath", "acknowledge", "apolog", "frustrat", "explain")):
        return [
            "sorry", "understand", "frustrating", "apologize", "help you", "i know",
            "appreciate", "urgency", "pending", "sorted", "next step", "let me",
        ]
    if any(w in blob for w in ("activat", "onboarding", "password", "credentials", "log in", "login")):
        return [
            "activat", "activation", "password", "credentials", "log in", "login",
            "email link", "verification link", "setup", "manually activ", "processing",
            "pending activation",
        ]
    if any(w in blob for w in ("access", "service access")) or (
        "confirm" in blob and any(w in blob for w in ("access", "login", "log in", "service"))
    ):
        return ["activated", "active", "access", "log in", "logged in", "working", "enjoy your", "all set"]
    if any(w in blob for w in ("resolve", "resolution", "fix", "solution", "provide")):
        return list(dict.fromkeys(SOP_PATTERNS["sop-4"] + ["activated", "resolved", "completed", "processing"]))
    if any(w in blob for w in ("troubleshoot", "diagnostic", "investigate", "technical")):
        return list(dict.fromkeys(SOP_PATTERNS["sop-3"] + [
            "restart", "reset", "network", "driver", "router", "update", "reconnect",
            "factory", "troubleshoot", "walk through", "install", "wi-fi", "wifi",
        ]))
    if any(w in blob for w in ("summar", "close", "wrap", "end call", "follow up")):
        return SOP_PATTERNS["sop-5"]
    if any(w in blob for w in ("gather", "information", "describe", "issue", "model", "problem")):
        return [
            "model", "which", "what is", "tell me", "describe", "account", "email",
            "printer", "problem", "issue", "may i have", "serial", "network",
        ]

    if act.id in SOP_PATTERNS:
        return SOP_PATTERNS[act.id]
    match = re.search(r"sop-(\d+)", act.id or "")
    if match:
        idx = ((int(match.group(1)) - 1) % 5) + 1
        return SOP_PATTERNS.get(f"sop-{idx}", [])
    generic = _token_patterns_from_activity(act)
    if generic:
        return generic
    return SOP_PATTERNS["sop-1"]


_SOP_STOPWORDS = frozenset({
    "agent", "customer", "their", "them", "they", "this", "that", "with", "from",
    "your", "have", "will", "should", "must", "step", "steps", "call", "about",
    "inform", "take", "make", "ensure", "using", "during", "through", "into",
})


def _token_patterns_from_activity(act: SOPActivity) -> list[str]:
    """Keyword patterns extracted from custom / AI-generated SOP names and descriptions."""
    blob = f"{(act.name or '')} {(act.description or '')}".lower()
    tokens = re.findall(r"[a-z]{4,}", blob)
    patterns: list[str] = []
    for tok in tokens:
        if tok in _SOP_STOPWORDS:
            continue
        if tok not in patterns:
            patterns.append(tok)
    return patterns[:14]


def _sop_matched_for_activity(
    text: str,
    act: SOPActivity,
    conversation: list[ConversationTurn] | None = None,
) -> bool:
    lower = normalize_stt_text(text)
    if any(p in lower for p in sop_patterns_for_activity(act)):
        return True
    if not conversation:
        return False
    blob = f"{(act.name or '')} {(act.description or '')}".lower()
    customer_blob = " ".join(
        normalize_stt_text(t.text) for t in conversation if t.speaker == "customer"
    )
    if "identif" in blob and "problem" in blob:
        if any(w in customer_blob for w in ("printer", "connect", "network", "not connect", "issue")):
            if any(
                w in lower
                for w in (
                    "printer", "connect", "network", "issue", "problem", "frustrat",
                    "understand", "sorry", "help", "sorted", "model", "serial", "troubleshoot",
                )
            ):
                return True
    if "identif" in blob and "issue" in blob:
        if any(w in customer_blob for w in ("printer", "connect", "network", "not connect", "issue", "billing", "charge")):
            if any(w in lower for w in ("help you with", "issue", "problem", "printer", "connection", "understand", "sorry")):
                return True
    if any(w in blob for w in ("verify", "verification")) and any(
        w in blob for w in ("model", "serial", "printer", "network")
    ):
        if any(w in lower for w in ("model", "serial", "printer", "network", "provide", "may i have", "check if")):
            return True
    if "model" in blob and "printer" in blob:
        if any(w in lower for w in ("model", "serial", "which printer", "what model")):
            return True
    if any(w in blob for w in ("restart", "reboot", "power cycle")):
        if any(w in lower for w in ("restart", "reboot", "router", "printer", "both")):
            return True
    if any(w in blob for w in ("network", "wifi", "wi-fi", "connection")) and any(
        w in blob for w in ("check", "verify", "guide", "within")
    ):
        if any(w in lower for w in ("wifi", "wi-fi", "network", "signal", "range", "router", "within")):
            return True
    if any(w in blob for w in ("investigate", "review", "research")):
        if any(w in lower for w in ("review", "check", "look into", "case", "account", "system", "let me")):
            return True
    if any(w in blob for w in ("apply", "resolution", "resolve", "action")):
        if any(w in lower for w in ("resolve", "policy", "fix", "applied", "next step", "take")):
            return True
    if any(w in blob for w in ("summar", "recap", "wrap")):
        if any(w in lower for w in ("summarize", "summary", "recap", "to summarize", "next steps")):
            return True
    if "disclosure" in blob or "recorded" in blob:
        if any(w in lower for w in ("recorded", "recording", "quality purposes", "may be recorded")):
            return True
    return False


def map_sop_to_script_cues(
    success_script: list[Any],
    sop_activities: list[SOPActivity],
) -> list[dict[str, Any]]:
    """Map each SOP step to the matching trainee script line (timeline-aligned)."""
    from utils.voice_training.script_turns import is_verification_question

    trainee_lines = trainee_lines_from_script(success_script, skip_filler=False)
    if not sop_activities:
        return []
    if not trainee_lines:
        return [
            {"sop_id": act.id, "sop_name": act.name, "script_index": None, "script_line": None}
            for act in sop_activities
        ]

    cues: list[dict[str, Any]] = []
    used_indices: set[int] = set()
    last_assigned = -1

    def _pattern_hits(line: str, patterns: list[str], act: SOPActivity) -> int:
        lower = normalize_stt_text(line)
        hits = sum(1 for p in patterns if p in lower)
        blob = f"{(act.name or '').lower()} {(act.description or '').lower()}"
        is_verify_step = any(w in blob for w in ("verify", "verification", "identity", "authenticate"))
        is_resolve_step = any(w in blob for w in ("resolve", "resolution", "fix", "solution"))
        if is_verify_step and is_verification_question(line):
            hits += 3
        if is_resolve_step and is_verification_question(line):
            hits = max(0, hits - 3)
        if is_verify_step and any(w in lower for w in ("monitor", "recording", "disclosure")):
            hits = max(0, hits - 2)
        return hits

    for step_i, act in enumerate(sop_activities):
        patterns = sop_patterns_for_activity(act)
        best_idx: int | None = None
        best_hits = 0
        preferred = min(step_i, len(trainee_lines) - 1)
        search_window = range(
            max(0, preferred - 2),
            min(len(trainee_lines), preferred + 4),
        )
        for i in search_window:
            if i in used_indices:
                continue
            hits = _pattern_hits(trainee_lines[i], patterns, act)
            if hits > best_hits:
                best_hits = hits
                best_idx = i
        if best_hits == 0:
            for i, line in enumerate(trainee_lines):
                if i in used_indices:
                    continue
                hits = _pattern_hits(line, patterns, act)
                if hits > best_hits:
                    best_hits = hits
                    best_idx = i

        if best_idx is None or best_hits == 0:
            # No line matches this step's keywords. SOP steps and the script both run in call
            # order, so fall forward to the next unclaimed line instead of the first unclaimed
            # one, which pinned late steps to early quotes — "Provide Resolution" was labelled
            # with the opening greeting. With nothing left ahead, emit no cue rather than a
            # wrong one; the UI omits an absent cue.
            forward = [
                i
                for i in range(last_assigned + 1, len(trainee_lines))
                if i not in used_indices
            ]
            if not forward:
                cues.append({
                    "sop_id": act.id,
                    "sop_name": act.name,
                    "script_index": None,
                    "script_line": None,
                })
                continue
            best_idx = forward[0]

        used_indices.add(best_idx)
        last_assigned = max(last_assigned, best_idx)
        cues.append({
            "sop_id": act.id,
            "sop_name": act.name,
            "script_index": best_idx,
            "script_line": trainee_lines[best_idx],
        })
    return cues


def check_sop_contextual(activities: list[SOPActivity], conversation: list[ConversationTurn]) -> list[SOPActivity]:
    trainee_text = " ".join(normalize_stt_text(t.text) for t in conversation if t.speaker == "trainee")
    updated: list[SOPActivity] = []
    for act in activities:
        completed = _sop_matched_for_activity(trainee_text, act, conversation)
        if not completed:
            for turn in conversation:
                if turn.speaker == "trainee" and _sop_matched_for_activity(turn.text, act, conversation):
                    completed = True
                    break
        updated.append(act.model_copy(update={"completed": completed}))
    return updated


CLOSING_AGENT_MARKERS = [
    "summarize", "summary", "recap", "anything else", "before we end", "great day",
    "thank you for calling", "wrap up", "next steps", "all set", "glad we could help",
]


def _is_closing_reference(text: str) -> bool:
    lower = (text or "").lower()
    return any(m in lower for m in CLOSING_AGENT_MARKERS)


def _best_reference_line(actual: str, reference_lines: list[str], trainee_idx: int) -> str:
    """Pick the script line that best matches what the agent said (position-aware)."""
    if not reference_lines:
        return ""
    capped = min(trainee_idx, len(reference_lines) - 1)
    best_ref = reference_lines[capped]
    best_score = -1.0
    search_end = min(len(reference_lines), trainee_idx + 4)
    search_start = max(0, trainee_idx - 2)
    for i in range(search_start, search_end):
        score = contextual_similarity(actual, reference_lines[i])
        if i == capped:
            score += 0.08
        elif abs(i - capped) <= 1:
            score += 0.04
        if score > best_score:
            best_score = score
            best_ref = reference_lines[i]
    if best_score < 0.38:
        for ref in reference_lines:
            score = contextual_similarity(actual, ref)
            if score > best_score:
                best_score = score
                best_ref = ref
    return best_ref


def analyze_trainee_turns(
    conversation: list[ConversationTurn],
    success_script: list[Any],
) -> list[dict[str, Any]]:
    reference_lines = trainee_lines_from_script(success_script)
    analysis: list[dict[str, Any]] = []
    trainee_idx = 0

    for j, turn in enumerate(conversation):
        if turn.speaker != "trainee":
            continue
        post_script = trainee_idx >= len(reference_lines)
        if post_script:
            ref = reference_lines[-1] if reference_lines else ""
        else:
            ref = _best_reference_line(turn.text, reference_lines, trainee_idx)
        customer_msg = _customer_before_index(conversation, j)
        cust_lower = customer_msg.lower()
        customer_has_problem = any(w in cust_lower for w in PROBLEM_MARKERS)
        empathy_bonus = 0.0
        politeness_bonus = 0.0
        if customer_has_problem:
            if any(m in normalize_stt_text(turn.text) for m in EMPATHY_MARKERS) or "sorry" in turn.text.lower():
                empathy_bonus = 15.0
            if any(m in turn.text.lower() for m in POLITE_MARKERS):
                politeness_bonus = 8.0

        context_pct = round(contextual_similarity(turn.text, ref) * 100, 1) if ref else 55.0
        response_pct = round(addresses_customer_message(turn.text, customer_msg) * 100, 1)
        intent_pct = round(semantic_intent_score(turn.text, ref, customer_msg) * 100, 1)
        intent_pct = min(100.0, intent_pct + empathy_bonus * 0.35 + politeness_bonus * 0.2)

        if post_script:
            closing_bonus = 12.0 if _is_closing_reference(turn.text) or response_pct >= 50 else 0.0
            context_pct = min(100.0, context_pct * 0.5 + response_pct * 0.3 + closing_bonus)
            intent_pct = min(100.0, intent_pct + closing_bonus * 0.5)

        combined = round((context_pct * 0.25 + response_pct * 0.45 + intent_pct * 0.30), 1)
        passed = combined >= 38 or (response_pct >= 42 and intent_pct >= 35)

        if post_script:
            if combined >= 52:
                feedback = "Good closing — you wrapped up the call appropriately in your own words."
            elif combined >= 38:
                feedback = "Acceptable wrap-up — summarize and confirm next steps when ending the call."
            else:
                feedback = "Closing needs work — address the customer's last message and summarize before ending."
        elif combined >= 72:
            feedback = "Strong — you addressed the customer with the right intent in your own words."
        elif combined >= 52:
            feedback = "Good — meaning and context match; exact script wording not required."
        elif combined >= 38:
            feedback = "Acceptable — you responded to the customer; try clearer next steps."
        else:
            feedback = "Needs work — address what the customer just said and follow the SOP step."

        analysis.append(
            {
                "turn_index": trainee_idx + 1,
                "trainee_text": turn.text,
                "reference_text": ref,
                "customer_context": customer_msg[:200] if customer_msg else "",
                "context_match_pct": context_pct,
                "response_relevance_pct": response_pct,
                "intent_pct": intent_pct,
                "combined_pct": combined,
                "passed": passed,
                "post_script": post_script,
                "feedback": feedback,
            }
        )
        trainee_idx += 1
    return analysis


def _customer_before_index(conversation: list[ConversationTurn], agent_index: int) -> str:
    for k in range(agent_index - 1, -1, -1):
        if conversation[k].speaker == "customer":
            return conversation[k].text
    return ""


def score_contextual_empathy(conversation: list[ConversationTurn]) -> float:
    """Score empathy when customer has stated a problem — varied, not repetitive filler."""
    problem_seen = False
    hits = 0
    checked = 0
    understand_streak = 0
    max_understand_streak = 0
    for turn in conversation:
        if turn.speaker == "customer":
            if any(m in turn.text.lower() for m in PROBLEM_MARKERS):
                problem_seen = True
            continue
        if turn.speaker != "trainee" or not problem_seen:
            continue
        checked += 1
        lower = normalize_stt_text(turn.text)
        if lower.startswith("i understand") or lower.startswith("i completely understand"):
            understand_streak += 1
            max_understand_streak = max(max_understand_streak, understand_streak)
        else:
            understand_streak = 0
        if any(m in lower for m in EMPATHY_MARKERS) or any(w in lower for w in ("sorry", "appreciate", "hear your")):
            hits += 1
        if checked >= 6:
            break
    if not checked:
        trainee_turns = [t for t in conversation if t.speaker == "trainee"]
        if not trainee_turns:
            return 1.0
        hits = sum(
            1
            for t in trainee_turns[:6]
            if any(m in normalize_stt_text(t.text) for m in EMPATHY_MARKERS)
            or any(w in t.text.lower() for w in ("sorry", "appreciate", "hear your", "help you"))
        )
        rate = hits / len(trainee_turns[:6])
        return max(1.0, min(5.0, 1.0 + rate * 3.0))
    rate = hits / checked
    base = min(5.0, 2.0 + rate * 3.0)
    if max_understand_streak >= 2:
        base = max(1.0, base - (max_understand_streak - 1) * 0.75)
    return base


def score_contextual_politeness(conversation: list[ConversationTurn]) -> float:
    if not conversation:
        return 1.0
    trainee_turns = [t for t in conversation if t.speaker == "trainee"]
    if not trainee_turns:
        return 1.0
    hits = sum(
        1 for t in trainee_turns
        if any(m in t.text.lower() for m in POLITE_MARKERS)
    )
    rate = hits / len(trainee_turns)
    unprofessional = sum(1 for t in trainee_turns for m in PROFESSIONAL_ANTI if m in t.text.lower())
    base = min(5.0, 2.0 + rate * 3.0)
    return max(1.0, base - unprofessional * 0.5)


def score_from_turn_analysis(turn_analysis: list[dict[str, Any]]) -> dict[str, float]:
    if not turn_analysis:
        return {"intent": 1.0, "dialogue": 1.0, "avg_context": 0.0, "avg_combined": 0.0}
    avg_combined = sum(t["combined_pct"] for t in turn_analysis) / len(turn_analysis)
    avg_context = sum(t["context_match_pct"] for t in turn_analysis) / len(turn_analysis)
    pass_rate = sum(1 for t in turn_analysis if t["passed"]) / len(turn_analysis)
    intent = min(5.0, 1.5 + (avg_combined / 100) * 3.5)
    dialogue = min(5.0, 1.5 + pass_rate * 3.5)
    return {
        "intent": round(intent, 1),
        "dialogue": round(dialogue, 1),
        "avg_context": round(avg_context, 1),
        "avg_combined": round(avg_combined, 1),
    }


def infer_scenario_resolved(
    scenario,
    trainee_text: str,
    conversation: list,
) -> bool:
    """Detect issue resolution from scenario category/description — not activation-only."""
    desc = (scenario.description or "").lower()
    cat = getattr(scenario.category, "value", str(scenario.category or "")).lower()
    trainee_lower = (trainee_text or "").lower()
    customer_tail = " ".join(
        t.text.lower() for t in conversation[-6:] if getattr(t, "speaker", None) == "customer"
    )

    marker_groups: list[tuple[str, ...]] = []
    if "activat" in cat or "activat" in desc:
        marker_groups.append(
            ("activated", "now active", "all set", "enjoy your services", "account is active", "activation complete")
        )
    if any(w in cat or w in desc for w in ("billing", "charge", "refund", "invoice", "dispute")):
        marker_groups.append(
            ("refund", "credit applied", "adjustment", "corrected", "billing concern", "documented the resolution")
        )
    if any(w in cat or w in desc for w in ("technical", "printer", "connect", "network", "outage", "wifi")):
        marker_groups.append(
            ("working now", "connected", "it's working", "fixed", "troubleshoot", "connection issue")
        )
    if "escalat" in cat or "escalat" in desc:
        marker_groups.append(
            ("escalat", "specialist will contact", "team will review", "technician will contact", "follow up")
        )
    marker_groups.append(
        ("resolved", "all set", "issue is fixed", "problem solved", "taken care of", "to summarize")
    )

    for markers in marker_groups:
        if any(m in trainee_lower for m in markers):
            return True

    if any(w in customer_tail for w in ("thank you for resolving", "sounds good", "appreciate your help")):
        if any(w in trainee_lower for w in ("summarize", "to summarize", "documented", "resolved", "adjustment")):
            return True
    return False
