"""
Centralized request-input validation (VAPT: Improper Input Validation).

Every helper is total: it canonicalizes the value first, then applies
type / length / range / allow-list checks, and raises ``ValueError`` on rejection
so Pydantic surfaces a 422 instead of persisting the raw input.

Design notes
------------
* Canonicalization uses Unicode NFKC so a full-width or compatibility character
  cannot smuggle a blocked character (``<``, ``/``, ...) past the checks.
* Control characters are stripped, which also closes CR/LF log-injection and NUL
  truncation on every field routed through here.
* Screening uses set membership and flat character classes only — no nested
  quantifiers, so there is no catastrophic backtracking (Sonar python:S2631).
* SQL safety comes from SQLAlchemy parameter binding across the app; these
  helpers add the length/charset gate in front of it.
* Angle brackets are rejected on names/labels because no legitimate value needs
  them; long-form content fields keep them (module markdown depends on them) and
  are length-capped instead, with escaping applied at render time.
"""
from __future__ import annotations

import os
import unicodedata
from typing import Any, Iterable, Optional, Sequence

# ── Length ceilings ───────────────────────────────────────────────────────────
MAX_NAME_LENGTH = 120
MAX_TITLE_LENGTH = 300
MAX_DESCRIPTION_LENGTH = 1000
MAX_REASON_LENGTH = 2000
MAX_FILENAME_LENGTH = 255
MAX_IDENTIFIER_LENGTH = 64
# Chat / tutor session keys are client-generated COMPOSITE strings, not a single UUID. The
# tutor builds `tutor-<owner uuid>-<course name>-m<n>-<viewer uuid>` so a module's raise-hand
# history stays tied to one module for one viewer, which is already 83 characters before the
# course name is appended - always over MAX_IDENTIFIER_LENGTH. Validating those keys with the
# 64-character ceiling rejected EVERY typed raise-hand question with a 422 before the endpoint
# body ran. The ceiling is widened here rather than by shortening the generated key, because a
# new key format would orphan the history of every session already stored under the long form.
# The identifier character allow-list is unchanged - only the length ceiling differs.
MAX_SESSION_ID_LENGTH = 256
MAX_EMAIL_LENGTH = 254
MAX_QUERY_LENGTH = 4000
MAX_CONTENT_LENGTH = 200_000
MAX_PASSWORD_LENGTH = 128


def _env_int(name: str, default: int, *, floor: int) -> int:
    """Read a positive int from the environment, never dropping below ``floor``."""
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        value = default
    # A misconfigured environment must not be able to weaken the policy below the
    # floor the VAPT remediation asks for.
    return max(floor, value)


# VAPT #8 (Weak Password Accepted): the POC registered `12345678` - exactly the old
# 8-character minimum - so the gate passed a trivially guessable secret. The tracker
# asks for "a strong policy (12+ characters)". Configurable upward only: the floor
# keeps a stray environment value from re-opening the finding.
MIN_PASSWORD_LENGTH = _env_int("MIN_PASSWORD_LENGTH", 12, floor=12)
# Distinct character classes required (lower, upper, digit, symbol). 3 of 4 keeps a
# long passphrase usable while still ruling out single-class strings like
# "123456789012" or "aaaaaaaaaaaa", which meet 12 characters but carry no entropy.
PASSWORD_MIN_CHARACTER_CLASSES = _env_int(
    "PASSWORD_MIN_CHARACTER_CLASSES", 3, floor=1
)
MAX_ID_LIST_ITEMS = 500
MAX_ANSWER_ITEMS = 500

# Known-breached / trivially guessable passwords, stored lower-case and compared
# case-insensitively (VAPT #8: "weak or breached passwords"). Kept deliberately small
# and in-process: a full HaveIBeenPwned range lookup would put a network call on the
# signup path, and shipping a multi-megabyte corpus would bloat the image. The length
# and character-class gates above do the heavy lifting - this list exists to stop the
# handful of 12+ character strings that pass those gates yet top every wordlist.
# `12345678` (the POC8 payload) is here explicitly and is also caught by the length
# rule; it is listed so the intent survives a config change.
_BREACHED_PASSWORDS = frozenset(
    {
        "12345678", "123456789", "1234567890", "123456789012", "1234567890123",
        "administrator", "qwertyuiop", "abcdefghijkl", "aaaaaaaaaaaa",
        "trustno1", "sunshine", "princess", "football", "baseball", "superman",
    }
)

# Base words that dominate every cracking wordlist. Matching on the BASE rather than on
# whole strings is what makes this robust: an exact-string list has to enumerate
# `Welcome@123`, `Welcome@1234`, `Welcome@123456`, `Welcome#2025`, ... and will always
# have a hole. A base-word match catches the entire family at once, which is exactly the
# "attackers can guess" shape the finding describes - the digits and symbols people
# append are the first thing a rules-based cracker (hashcat `best64`) tries.
_COMMON_BASE_WORDS = frozenset(
    {
        "password", "passwd", "pass", "pwd", "welcome", "admin", "administrator",
        "root", "guest", "test", "testing", "demo", "sample", "temp", "temporary",
        "default", "changeme", "letmein", "login", "signin", "secret", "qwerty",
        "asdf", "asdfgh", "zxcvbn", "iloveyou", "monkey", "dragon", "master",
        "shadow", "sunshine", "princess", "football", "baseball", "superman",
        "trustno", "abcd", "abcde", "abcdef", "hello", "hallo", "freedom",
        "whatever", "starwars", "computer", "internet", "server", "database",
        "company", "corp", "office", "india", "hexaware", "smarttutor",
        "smart", "tutor", "velocity", "training", "trainer", "trainee", "student",
        "january", "february", "march", "april", "june", "july", "august",
        "september", "october", "november", "december", "monday", "tuesday",
        "wednesday", "thursday", "friday", "saturday", "sunday",
        "summer", "winter", "spring", "autumn",
    }
)

# `a`->`4` style substitutions, so `P@ssw0rd@123` folds onto the base word `password`.
# Applied only when testing against the base-word list - the stored secret is never
# altered.
_LEET_FOLD = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s",
                            "7": "t", "8": "b", "9": "g", "$": "s", "@": "a",
                            "!": "i", "+": "t", "(": "c"})

_CONTROL_CHARS = frozenset(chr(code) for code in range(32)) | {chr(127)}
_ALLOWED_WHITESPACE = frozenset({"\n", "\t"})
_HTML_UNSAFE_CHARS = frozenset("<>")
_PATH_UNSAFE_CHARS = frozenset("/\\")
_IDENTIFIER_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _require_scalar(value: Any, field: str) -> str:
    """Reject non-scalar payloads (dicts/lists smuggled into a string field)."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} must be text.")
    return str(value)


def _canonicalize(value: Any, field: str, *, allow_newlines: bool) -> str:
    """NFKC-normalize, drop control characters, and trim outer whitespace."""
    text = unicodedata.normalize("NFKC", _require_scalar(value, field))
    if allow_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept = [
        ch
        for ch in text
        if ch not in _CONTROL_CHARS or (allow_newlines and ch in _ALLOWED_WHITESPACE)
    ]
    return "".join(kept).strip()


def _reject_html_chars(text: str, field: str) -> None:
    if any(ch in _HTML_UNSAFE_CHARS for ch in text):
        raise ValueError(f"{field} must not contain '<' or '>'.")


def _check_length(text: str, field: str, *, min_length: int, max_length: int) -> None:
    if len(text) < min_length:
        raise ValueError(f"{field} must be at least {min_length} character(s) long.")
    if len(text) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters long.")


def normalize_email(value: Any) -> str:
    """
    Canonical form used for every email comparison and for storage.

    Email addresses are case-insensitive in practice, but ``EmailStr`` only lower-cases
    the DOMAIN - ``Phani@st.com`` stays ``Phani@st.com``. Because the user lookup is an
    exact ``User.email == email`` match, signing up as ``Phani@st.com`` and then signing
    in as ``phani@st.com`` would fail to find the account (and, worse, the unique index
    would happily accept BOTH as two separate users). Normalizing on the way in and on
    every lookup makes the two directions interchangeable.

    Returns "" for anything unusable (None, wrong type, blank) and leaves the decision
    about whether that is an error to the caller - callers already raise their own
    "Email is required" / 401. NFKC first so full-width lookalikes cannot be used to
    register a visually identical second account.

    No regex, so there is no backtracking risk (Sonar python:S2631).
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return ""
    # NFKC folds full-width forms (e.g. "Ｐhani＠st.com") onto their ASCII equivalents.
    text = unicodedata.normalize("NFKC", value)
    text = "".join(ch for ch in text if ch not in _CONTROL_CHARS)
    return text.strip().lower()


# ── Registration email domain policy (VAPT #12: Temporary email accepted) ─────
# The POC registered `cisin77379@kingcq.com` - a throwaway inbox - and got 201. The
# tracker asks to "detect and block disposable email domains using an updated denylist or
# email validation service during registration".
#
# An ALLOW-LIST is used as the primary control rather than a denylist, because a denylist
# of disposable providers is unwinnable: new domains appear daily (kingcq.com is one of
# thousands), so a list that is current today is stale next week. This is a corporate
# training platform whose users all hold company addresses, so naming the two domains that
# may register closes the finding completely - every disposable provider, present and
# future, is outside the list by construction.
#
# Applies ONLY where a new account is created (self-signup and the super-admin Create User
# panel). Sign-in is deliberately untouched, so accounts that already exist on any other
# domain keep working and are never locked out - see the note on `safe_password`.
_DEFAULT_REGISTRATION_EMAIL_DOMAINS = ("testenvhexa.com", "hexaware.com")

# Second gate, defence in depth. Under the default allow-list this is unreachable: nothing
# here could match `testenvhexa.com`/`hexaware.com`. It becomes the active control only if
# an operator widens SIGNUP_ALLOWED_EMAIL_DOMAINS, so widening the allow-list cannot
# silently re-open the finding for the best-known throwaway providers. Deliberately not
# exhaustive - the allow-list is what actually closes #12.
_DISPOSABLE_EMAIL_DOMAINS = frozenset(
    {
        "kingcq.com",  # the POC12 payload
        "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com",
        "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org",
        "throwawaymail.com", "yopmail.com", "yopmail.net", "getnada.com", "nada.email",
        "dispostable.com", "trashmail.com", "trashmail.net", "maildrop.cc",
        "mailnesia.com", "mytemp.email", "fakeinbox.com", "spamgourmet.com",
        "mohmal.com", "emailondeck.com", "tempinbox.com", "discard.email",
        "mailcatch.com", "inboxbear.com", "tempr.email", "moakt.com", "linshiyouxiang.net",
        "burnermail.io", "anonaddy.com", "simplelogin.io", "mail.tm", "minuteinbox.com",
    }
)


def _parse_domain_list(raw: Any) -> tuple[str, ...]:
    """
    Split a comma-separated domain list into canonical, de-duplicated entries.

    Tolerant of how an operator actually types a secret-store value: surrounding spaces,
    an accidental leading ``@`` (``@hexaware.com``), mixed case, and a trailing comma are
    all accepted. Entries containing a ``/`` or ``@`` after that cleanup are dropped as
    malformed rather than being stored, so a pasted URL or full address cannot become an
    allow-list entry that matches nothing (or, worse, something unintended).

    Plain string scanning, no regex, so there is no backtracking risk (Sonar python:S2631).
    """
    if not isinstance(raw, str):
        return ()
    entries: list[str] = []
    for chunk in raw.split(","):
        domain = normalize_email(chunk).lstrip("@").strip(".")
        if not domain or "@" in domain or "/" in domain or "." not in domain:
            continue
        if domain not in entries:
            entries.append(domain)
    return tuple(entries)


def _resolve_registration_email_domains() -> tuple[str, ...]:
    """
    Domains permitted to register, from ``SIGNUP_ALLOWED_EMAIL_DOMAINS``.

    A blank, missing or entirely malformed value falls back to the built-in default
    instead of meaning "allow every domain" - the failure mode of a config typo has to be
    the SECURE one, or a stray edit in the secret store would silently re-open #12.
    """
    configured = _parse_domain_list(os.environ.get("SIGNUP_ALLOWED_EMAIL_DOMAINS", ""))
    return configured or _DEFAULT_REGISTRATION_EMAIL_DOMAINS


REGISTRATION_EMAIL_DOMAINS = _resolve_registration_email_domains()


def registration_email_domains() -> tuple[str, ...]:
    """
    The allow-list as it stands **right now**, re-read from the environment.

    ``REGISTRATION_EMAIL_DOMAINS`` is resolved once at import, which is what the
    validators use and is correct for a process whose environment is fixed at start-up.
    This accessor exists for the places that must reflect a *changed* value without a
    code edit - notably the domain list handed to the browser - so that adding a domain
    is only ever a ``SIGNUP_ALLOWED_EMAIL_DOMAINS`` change plus a restart, never a
    change to ``static/script.js``.

    Falls back to the module-level tuple the same way, so it can never return an empty
    list and thereby allow everything.
    """
    return _resolve_registration_email_domains()


def email_domain(value: Any) -> str:
    """
    Canonical domain part of an address, or "" when there isn't exactly one ``@``.

    More than one ``@`` is refused outright rather than resolved by picking the last one.
    An unquoted local part may not contain ``@`` at all, so ``a@b@hexaware.com`` is simply
    invalid - and treating it as valid would create a **parser differential**: this
    function would read the domain as ``hexaware.com`` and allow the registration, while
    any downstream consumer that splits on the FIRST ``@`` (a mail relay, a log parser, an
    SSO mapper) would read it as ``b@hexaware.com``. Requiring exactly one separator means
    every parser agrees, so the allow-list cannot be side-stepped by an address whose
    domain depends on which end you read from.

    The domain must also carry a dot; a bare label like ``a@hexaware`` is not a
    registerable address and would otherwise reach the set test as ``"hexaware"``.
    """
    normalized = normalize_email(value)
    if normalized.count("@") != 1:
        return ""
    local, separator, domain = normalized.partition("@")
    if not separator or not local or not domain or "." not in domain:
        return ""
    return domain


def is_disposable_email_domain(value: Any) -> bool:
    """True when the address's domain is a known throwaway provider."""
    return email_domain(value) in _DISPOSABLE_EMAIL_DOMAINS


def safe_registration_email(
    value: Any,
    *,
    field: str = "Email",
    allowed_domains: Optional[Sequence[str]] = None,
) -> str:
    """
    Gate for the email of a *newly created* account (VAPT #12).

    Returns the canonical (NFKC, lower-cased) address so the caller stores exactly what
    was validated. Raises ``ValueError`` - surfaced by Pydantic as a 422 - when the
    domain is not on the allow-list.

    The domain comparison is **exact equality against a set**, never ``endswith``. That
    distinction is the whole control: ``"x-hexaware.com".endswith("hexaware.com")`` is
    ``True``, so a suffix test would accept an attacker-registered look-alike domain and
    leave the finding open. Subdomains are likewise not accepted implicitly - only the
    listed domains match - so nothing outside the company's own DNS can qualify.

    Deliberately NOT applied to sign-in, and not applied to ``UserBase`` itself: that
    model is also used to serialize EXISTING users (``UserInDB``), and validating there
    would make every response for a pre-existing account on another domain fail. This is
    the same "new values only" split used for the password policy.
    """
    # The live value, not the import-time constant, so the validator and the list handed
    # to the browser can never disagree about what is allowed.
    allowed = (tuple(allowed_domains) if allowed_domains is not None
               else registration_email_domains())
    normalized = normalize_email(value)
    if not normalized:
        raise ValueError(f"{field} is required.")
    # Length is checked on the canonical form, before the domain test, so an
    # absurdly long value is refused cheaply.
    _check_length(normalized, field, min_length=3, max_length=MAX_EMAIL_LENGTH)

    domain = email_domain(normalized)
    if not domain:
        raise ValueError(f"{field} must be a valid address.")

    # Set membership, so a domain is accepted only if it IS one of the allowed values.
    if domain not in set(allowed):
        # The rejection names the permitted domains (the user needs to know what to do)
        # but never echoes the submitted address back into the response body.
        raise ValueError(
            "Registration is limited to company email addresses. "
            f"Please use an address ending in: {', '.join('@' + d for d in allowed)}."
        )

    # Unreachable under the default allow-list; the active control if it is widened.
    if domain in _DISPOSABLE_EMAIL_DOMAINS:
        raise ValueError(
            "Disposable or temporary email addresses are not accepted. "
            "Please register with your company email address."
        )
    return normalized


def safe_name(
    value: Any,
    *,
    field: str = "Value",
    max_length: int = MAX_NAME_LENGTH,
    min_length: int = 1,
) -> str:
    """Single-line label (batch name, process name, title). Angle brackets rejected."""
    text = _canonicalize(value, field, allow_newlines=False)
    _check_length(text, field, min_length=min_length, max_length=max_length)
    _reject_html_chars(text, field)
    return text


def safe_optional_name(
    value: Any,
    *,
    field: str = "Value",
    max_length: int = MAX_NAME_LENGTH,
) -> Optional[str]:
    """``safe_name`` that preserves ``None`` (PATCH-style optional fields)."""
    if value is None:
        return None
    return safe_name(value, field=field, max_length=max_length)


def safe_description(
    value: Any,
    *,
    field: str = "Description",
    max_length: int = MAX_DESCRIPTION_LENGTH,
) -> str:
    """Multi-line prose that never needs markup (descriptions, reasons, remarks)."""
    text = _canonicalize(value, field, allow_newlines=True)
    _check_length(text, field, min_length=0, max_length=max_length)
    _reject_html_chars(text, field)
    return text


def safe_optional_description(
    value: Any,
    *,
    field: str = "Description",
    max_length: int = MAX_DESCRIPTION_LENGTH,
) -> Optional[str]:
    if value is None:
        return None
    return safe_description(value, field=field, max_length=max_length)


def safe_required_description(
    value: Any,
    *,
    field: str = "Description",
    max_length: int = MAX_REASON_LENGTH,
) -> str:
    """``safe_description`` that additionally refuses an empty result."""
    text = safe_description(value, field=field, max_length=max_length)
    if not text:
        raise ValueError(f"{field} is required.")
    return text


def safe_free_text(
    value: Any,
    *,
    field: str = "Content",
    max_length: int = MAX_CONTENT_LENGTH,
    min_length: int = 0,
) -> str:
    """
    Long-form content (module markdown, chat query, generated question text).

    Markup characters are preserved because module content legitimately carries
    markdown/HTML; safety comes from the length/control-character gate here plus
    context-aware escaping at every render site.
    """
    text = _canonicalize(value, field, allow_newlines=True)
    _check_length(text, field, min_length=min_length, max_length=max_length)
    return text


def safe_optional_free_text(
    value: Any,
    *,
    field: str = "Content",
    max_length: int = MAX_CONTENT_LENGTH,
) -> Optional[str]:
    if value is None:
        return None
    return safe_free_text(value, field=field, max_length=max_length)


def safe_filename(
    value: Any,
    *,
    field: str = "File name",
    max_length: int = MAX_FILENAME_LENGTH,
) -> str:
    """Document/report name used to build S3 keys — blocks path traversal."""
    text = _canonicalize(value, field, allow_newlines=False)
    _check_length(text, field, min_length=1, max_length=max_length)
    _reject_html_chars(text, field)
    if any(ch in _PATH_UNSAFE_CHARS for ch in text):
        raise ValueError(f"{field} must not contain path separators.")
    if ".." in text or text in {".", ".."}:
        raise ValueError(f"{field} must not contain relative path segments.")
    return text


def safe_optional_filename(
    value: Any,
    *,
    field: str = "File name",
    max_length: int = MAX_FILENAME_LENGTH,
) -> Optional[str]:
    if value is None:
        return None
    return safe_filename(value, field=field, max_length=max_length)


def safe_identifier(
    value: Any,
    *,
    field: str = "Identifier",
    max_length: int = MAX_IDENTIFIER_LENGTH,
) -> str:
    """UUID-style key allow-list: letters, digits, underscore, hyphen only."""
    text = _canonicalize(value, field, allow_newlines=False)
    _check_length(text, field, min_length=1, max_length=max_length)
    if any(ch not in _IDENTIFIER_ALLOWED_CHARS for ch in text):
        raise ValueError(f"{field} contains unsupported characters.")
    return text


def safe_optional_identifier(
    value: Any,
    *,
    field: str = "Identifier",
    max_length: int = MAX_IDENTIFIER_LENGTH,
) -> Optional[str]:
    if value is None:
        return None
    text = _canonicalize(value, field, allow_newlines=False)
    if not text:
        return None
    return safe_identifier(text, field=field, max_length=max_length)


def safe_session_id(value: Any, *, field: str = "Session ID") -> str:
    """
    Chat / tutor session key: identifier charset, composite-length ceiling.

    Same allow-list as ``safe_identifier`` (letters, digits, underscore, hyphen), so a
    session key still cannot carry a path separator, quote or angle bracket. Only the
    length limit differs - see ``MAX_SESSION_ID_LENGTH`` for why a chat session key is
    legitimately longer than a bare UUID.

    Used by every entry point that accepts a chat session key from the client, so the
    typed and spoken raise-hand paths cannot disagree about what is acceptable.
    """
    return safe_identifier(value, field=field, max_length=MAX_SESSION_ID_LENGTH)


def safe_identifier_list(
    values: Any,
    *,
    field: str = "Identifiers",
    max_items: int = MAX_ID_LIST_ITEMS,
) -> list[str]:
    """
    Validate a list of identifiers and drop duplicates, preserving order.

    De-duplicating here keeps ``len(rows) != len(ids)`` ownership checks in the
    routers correct when a client repeats an id.
    """
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError(f"{field} must be a list.")
    items = list(values)
    if len(items) > max_items:
        raise ValueError(f"{field} must contain at most {max_items} entries.")
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        identifier = safe_identifier(item, field=field)
        if identifier not in seen:
            seen.add(identifier)
            cleaned.append(identifier)
    return cleaned


def safe_optional_identifier_list(
    values: Any,
    *,
    field: str = "Identifiers",
    max_items: int = MAX_ID_LIST_ITEMS,
) -> Optional[list[str]]:
    if values is None:
        return None
    return safe_identifier_list(values, field=field, max_items=max_items)


def safe_choice(
    value: Any,
    allowed: Sequence[str],
    *,
    field: str = "Value",
    lower: bool = True,
) -> str:
    """Allow-list a closed set of values (roles, publication types, actions)."""
    text = _canonicalize(value, field, allow_newlines=False)
    if lower:
        text = text.lower()
    if text not in set(allowed):
        raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}.")
    return text


def safe_optional_choice(
    value: Any,
    allowed: Sequence[str],
    *,
    field: str = "Value",
    lower: bool = True,
) -> Optional[str]:
    if value is None:
        return None
    return safe_choice(value, allowed, field=field, lower=lower)


def safe_int(
    value: Any,
    *,
    field: str = "Value",
    minimum: int,
    maximum: int,
) -> int:
    """Range-check an integer, rejecting bools and non-integral floats."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if value != int(value):
            raise ValueError(f"{field} must be a whole number.")
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(_canonicalize(value, field, allow_newlines=False))
        except ValueError as exc:
            raise ValueError(f"{field} must be a number.") from exc
    else:
        raise ValueError(f"{field} must be a number.")
    if number < minimum or number > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}.")
    return number


def safe_optional_int(
    value: Any,
    *,
    field: str = "Value",
    minimum: int,
    maximum: int,
) -> Optional[int]:
    if value is None:
        return None
    return safe_int(value, field=field, minimum=minimum, maximum=maximum)


def _character_classes(raw: str) -> int:
    """Count how many of lower / upper / digit / symbol appear in ``raw``."""
    has_lower = has_upper = has_digit = has_symbol = False
    for ch in raw:
        if ch.islower():
            has_lower = True
        elif ch.isupper():
            has_upper = True
        elif ch.isdigit():
            has_digit = True
        else:
            has_symbol = True
    return sum((has_lower, has_upper, has_digit, has_symbol))


def _is_sequential(raw: str) -> bool:
    """
    True when the whole string is one run of consecutive code points.

    Catches ``123456789012`` and ``abcdefghijkl`` - long enough to pass the length
    and class gates, but the first thing any wordlist tries. A single pass over the
    string, so there is no backtracking risk (Sonar python:S2631).
    """
    if len(raw) < 4:
        return False
    ascending = descending = True
    for prev, cur in zip(raw, raw[1:]):
        delta = ord(cur) - ord(prev)
        if delta != 1:
            ascending = False
        if delta != -1:
            descending = False
        if not ascending and not descending:
            return False
    return ascending or descending


def _is_common_word_with_suffix(raw: str) -> bool:
    """
    True when the value is a wordlist base word plus decoration.

    ``Welcome@123456`` -> drop the trailing ``@123456`` -> ``welcome``, a base word, so
    it is refused. This is the standard hashcat rule-based attack (a wordlist entry plus
    appended digits/symbols and simple leet substitution), which is precisely the
    "attackers can guess" case the finding describes.

    Order matters: the leading/trailing decoration is removed BEFORE leet-folding.
    Folding first would turn the appended digits into letters (``welcome@123`` ->
    ``welcomeai2e``) and nothing would ever match. Digits *inside* the word are still
    folded, so ``Adm1n@123`` and ``P@ssw0rd2025`` both reduce to a base word.

    Implemented with index scanning and str.translate - no regex, so there is no nested
    quantifier and no backtracking risk (Sonar python:S2631). Runs in one pass over the
    string.

    A value is only rejected when the *entire* remaining letter content is one base word.
    A password that merely CONTAINS a common word among other letters
    (``BlueAdminHouse7!``) still passes, so this does not become a blanket deny on
    ordinary passphrases.
    """
    # Trim non-letters from both ends: the appended "@123" / "2025!" and any prefix.
    start, end = 0, len(raw)
    while start < end and not raw[start].isalpha():
        start += 1
    while end > start and not raw[end - 1].isalpha():
        end -= 1
    stem = raw[start:end]
    if not stem:
        # No letters at all (e.g. "12345678!@#$"): the character-class and variety
        # gates already cover this, so there is nothing to compare against a word list.
        return False
    folded = stem.lower().translate(_LEET_FOLD)
    letters = "".join(ch for ch in folded if ch.isalpha())
    return letters in _COMMON_BASE_WORDS


def safe_password(value: Any, *, field: str = "Password") -> str:
    """
    Strength gate for a *new* password (VAPT #8: Weak Password Accepted).

    The POC registered ``12345678`` and got 200 OK. Enforced here rather than in the
    route so every creation path shares one policy: ``UserCreate`` (self-signup) and
    ``AdminUserCreate`` (super-admin panel) both route their ``password`` field
    through this helper.

    Deliberately NOT applied on sign-in. ``verify_password`` must keep working for
    accounts created under the old 8-character rule, and re-checking strength at
    login would turn the form into an oracle that reveals a stored password's shape.

    The value is never trimmed, canonicalized or charset-filtered - doing so would
    silently alter the user's secret. Only whole-string properties are inspected.

    Note on the 128-character ceiling: bcrypt hashes only the first **72 bytes**, so two
    passwords sharing a 72-byte prefix hash identically. The cap is deliberately left at
    128 characters rather than 72 - it bounds the work per request (a megabyte-long
    password would otherwise burn CPU in the KDF) while still accepting a long passphrase,
    and 72 bytes is already far more entropy than this policy asks for. Lowering it to 72
    would reject strong passphrases for no security gain.
    """
    raw = _require_scalar(value, field)
    if any(ch in _CONTROL_CHARS for ch in raw):
        raise ValueError(f"{field} must not contain control characters.")
    if len(raw) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"{field} must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if len(raw) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"{field} must be at most {MAX_PASSWORD_LENGTH} characters long.")
    if _character_classes(raw) < PASSWORD_MIN_CHARACTER_CLASSES:
        raise ValueError(
            f"{field} must combine at least {PASSWORD_MIN_CHARACTER_CLASSES} of: "
            "lowercase letters, uppercase letters, digits, symbols."
        )
    if len(set(raw)) < 5:
        raise ValueError(f"{field} must not repeat the same few characters.")
    if _is_sequential(raw):
        raise ValueError(f"{field} must not be a sequence of consecutive characters.")
    # Breached/common check. Compared case-insensitively so `Password@123` cannot slip
    # past an entry stored in lower case, and against base words so the whole
    # `Welcome@123` / `Welcome@123456` / `P@ssw0rd2025` family is covered rather than
    # only the exact strings someone remembered to enumerate.
    if raw.lower() in _BREACHED_PASSWORDS or _is_common_word_with_suffix(raw):
        raise ValueError(
            f"{field} appears in known breach and common-password lists. "
            "Please choose something less predictable."
        )
    return raw


def safe_answer_map(
    value: Any,
    *,
    field: str = "Answers",
    max_items: int = MAX_ANSWER_ITEMS,
    max_answer_length: int = MAX_QUERY_LENGTH,
) -> dict[str, str]:
    """Validate an assessment ``{question_id: answer}`` submission map."""
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object.")
    if len(value) > max_items:
        raise ValueError(f"{field} must contain at most {max_items} entries.")
    cleaned: dict[str, str] = {}
    for key, answer in value.items():
        question_id = safe_identifier(key, field=f"{field} key")
        cleaned[question_id] = safe_free_text(
            "" if answer is None else answer,
            field=f"{field} value",
            max_length=max_answer_length,
        )
    return cleaned


def coerce_safe_text(
    value: Any,
    *,
    max_length: int,
    allow_newlines: bool = True,
    fallback: str = "",
) -> str:
    """
    Canonicalize and truncate instead of raising.

    For models that carry BOTH request input and internally generated content
    (LLM scenario scripts, stored sessions being rehydrated). Rejecting there
    would turn model output into a 500, so the untrusted-input controls are
    applied by clamping: NFKC-normalize, drop control characters, hard-truncate.
    """
    try:
        text = _canonicalize(value, "Value", allow_newlines=allow_newlines)
    except ValueError:
        return fallback
    if not text:
        return fallback
    return text[:max_length]


def normalize_voice_transcript(value: Any) -> str:
    """
    Clamp a speech-to-text transcript down to a safe single-line query string.

    Applies to BOTH sources of a spoken chat question - the AWS Transcribe result and the
    browser Web Speech API fallback the client posts alongside the audio. The browser value
    is fully untrusted (anyone can post any string to the form field), so it gets the same
    treatment as typed chat input rather than being trusted because it came from our own JS.

    Clamps instead of raising: a garbled transcript should ask the user to repeat themselves,
    not return a 500 from the middle of a streaming response. Newlines are folded to spaces
    because this becomes one chat message, and the result is capped at MAX_QUERY_LENGTH so an
    oversized post cannot inflate the downstream LLM prompt.
    """
    # Fold newlines to spaces BEFORE canonicalizing: allow_newlines=False drops them as
    # control characters, which would run "line one\nline two" together into one word.
    if isinstance(value, str):
        value = value.replace("\r", " ").replace("\n", " ")
    text = coerce_safe_text(value, max_length=MAX_QUERY_LENGTH, allow_newlines=False)
    return " ".join(text.split())


# ── Closed value sets shared by schemas and routers ──────────────────────────
SIGNUP_ROLES = ("trainee", "trainer")
ALL_ROLES = ("super_admin", "trainer", "trainee")
PUBLICATION_TYPES = ("all_process", "batch", "multi_batch")
ASSESSMENT_SCOPES = ("end_of_course", "end_of_module")
QUESTION_TYPES = ("mcq", "subjective", "mixed")
QUESTION_REVIEW_ACTIONS = ("approve", "reject", "restore", "edit")

__all__ = [name for name in globals() if not name.startswith("_")]
