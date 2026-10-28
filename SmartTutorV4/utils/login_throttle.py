"""
Login brute-force throttling (VAPT: Password Bruteforcible).

The VAPT POC ran 37 Burp Intruder payloads at ``POST /auth/token`` and only one
was answered with 429 before a valid password returned 200 + access_token. Two
compounding gaps caused that:

  1. ``/auth/token`` sat in ``_RATE_LIMIT_EXEMPT_PATHS``, so the role-based
     middleware skipped login entirely.
  2. The only remaining guard was a slowapi ``10/minute`` decorator backed by
     process memory, while the container runs ``uvicorn --workers 3`` — three
     independent counters, so the effective ceiling was ~30/min per IP and there
     was no per-account limit at all.

This module implements the remediation the tracker asks for:

  * per-IP **and** per-account rate limiting (counted independently)
  * progressive delays that grow with the number of recent failures
  * a lockout window once the threshold is crossed
  * generic error messages — callers must not reveal whether the email exists

Counters live in the ``login_attempts`` table so every worker shares one tally.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from core.runtime import (
    LOGIN_FAILURE_WINDOW_MINUTES,
    LOGIN_LOCKOUT_MINUTES,
    LOGIN_MAX_FAILED_PER_ACCOUNT,
    LOGIN_MAX_FAILED_PER_IP,
    LOGIN_PROGRESSIVE_DELAY_MAX_SECONDS,
    LOGIN_PROGRESSIVE_DELAY_SECONDS,
    TRUST_FORWARDED_FOR_HEADER,
    logger,
)
from models.domain import LoginAttempt
from utils.security_helpers import log_safe

SCOPE_ACCOUNT = "account"
SCOPE_IP = "ip"

# Single message for every failure mode. Never say "no such user" or "account
# locked for <email>" - that turns the login form into an account oracle.
GENERIC_LOGIN_ERROR = "Incorrect email or password"
GENERIC_LOCKOUT_ERROR = (
    "Too many failed sign-in attempts. Please wait a few minutes and try again."
)


def _now() -> datetime:
    """
    Current UTC instant as a **naive** datetime.

    ``LoginAttempt.attempted_at`` is a plain SQLAlchemy ``DateTime``, which on
    PostgreSQL is ``TIMESTAMP WITHOUT TIME ZONE``. Handing psycopg2 a tz-aware value
    for that column makes the server apply its own ``TimeZone`` setting during the
    comparison, so on an RDS instance not set to UTC the rolling window would shift
    by the offset - silently widening or shrinking the lockout. Storing and comparing
    naive UTC on both sides keeps the window exact whatever the server is set to.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_account(email: Optional[str]) -> str:
    """Accounts are keyed case-insensitively so casing cannot reset the counter."""
    return (email or "").strip().lower()


def _window_start(minutes: int) -> datetime:
    return _now() - timedelta(minutes=max(1, minutes))


def count_recent_failures(db: Session, scope: str, identifier: str) -> int:
    """Failed attempts for this scope/identifier inside the rolling window."""
    if not identifier:
        return 0
    try:
        return (
            db.query(LoginAttempt)
            .filter(
                LoginAttempt.scope == scope,
                LoginAttempt.identifier == identifier,
                LoginAttempt.attempted_at >= _window_start(LOGIN_FAILURE_WINDOW_MINUTES),
            )
            .count()
        )
    except Exception as exc:
        # Never let a bookkeeping failure block sign-in for legitimate users.
        logger.error("Login throttle count failed: %s", log_safe(exc))
        return 0


def _limit_for(scope: str) -> int:
    if scope == SCOPE_ACCOUNT:
        return max(1, LOGIN_MAX_FAILED_PER_ACCOUNT)
    return max(1, LOGIN_MAX_FAILED_PER_IP)


def is_locked_out(db: Session, account: str, ip_address: str) -> Tuple[bool, int]:
    """
    Return (locked, retry_after_seconds).

    Locked when either the per-account or the per-IP failure count has reached its
    limit within the window. Checked *before* any password verification so a
    locked account costs the attacker a bcrypt-free 429.
    """
    for scope, identifier in ((SCOPE_ACCOUNT, account), (SCOPE_IP, ip_address)):
        if not identifier:
            continue
        if count_recent_failures(db, scope, identifier) >= _limit_for(scope):
            return True, max(1, LOGIN_LOCKOUT_MINUTES) * 60
    return False, 0


def progressive_delay_seconds(failure_count: int) -> float:
    """Delay grows with recent failures, capped so a worker is never tied up long."""
    if failure_count <= 0:
        return 0.0
    delay = failure_count * max(0.0, LOGIN_PROGRESSIVE_DELAY_SECONDS)
    return min(delay, max(0.0, LOGIN_PROGRESSIVE_DELAY_MAX_SECONDS))


def apply_progressive_delay(db: Session, account: str, ip_address: str) -> float:
    """
    Sleep proportionally to the failures already on record, then return the delay.

    Applied before the credential check on a request that already has failures
    behind it, so scripted guessing pays a growing cost per try.
    """
    recent = max(
        count_recent_failures(db, SCOPE_ACCOUNT, account),
        count_recent_failures(db, SCOPE_IP, ip_address),
    )
    delay = progressive_delay_seconds(recent)
    if delay > 0:
        time.sleep(delay)
    return delay


def record_failed_attempt(db: Session, account: str, ip_address: str) -> None:
    """Append one failure row per scope. Best-effort: never masks the 401."""
    try:
        for scope, identifier in ((SCOPE_ACCOUNT, account), (SCOPE_IP, ip_address)):
            if identifier:
                db.add(LoginAttempt(scope=scope, identifier=identifier))
        db.commit()
    except Exception as exc:
        logger.error("Could not record failed login attempt: %s", log_safe(exc))
        try:
            db.rollback()
        except Exception:
            logger.debug("Rollback after failed-attempt record also failed")


def clear_failed_attempts(db: Session, account: str, ip_address: str) -> None:
    """
    Drop this account's failure rows after a successful sign-in.

    The IP rows are deliberately kept: a shared NAT address must not have its
    budget reset by one attacker-controlled successful login elsewhere.
    """
    try:
        if account:
            db.query(LoginAttempt).filter(
                LoginAttempt.scope == SCOPE_ACCOUNT,
                LoginAttempt.identifier == account,
            ).delete(synchronize_session=False)
            db.commit()
    except Exception as exc:
        logger.error("Could not clear login attempts: %s", log_safe(exc))
        try:
            db.rollback()
        except Exception:
            logger.debug("Rollback after clearing attempts also failed")


def purge_expired_attempts(db: Session) -> int:
    """Housekeeping: drop rows older than the window so the table stays small."""
    try:
        deleted = (
            db.query(LoginAttempt)
            .filter(LoginAttempt.attempted_at < _window_start(LOGIN_FAILURE_WINDOW_MINUTES))
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(deleted or 0)
    except Exception as exc:
        logger.error("Could not purge login attempts: %s", log_safe(exc))
        try:
            db.rollback()
        except Exception:
            logger.debug("Rollback after purge also failed")
        return 0


def _socket_peer(request) -> str:
    try:
        return (request.client.host if request.client else "") or "unknown"
    except Exception:
        return "unknown"


def client_ip(request) -> str:
    """
    Rate-limit key for the caller's address.

    Deliberately based on the **socket peer**, not ``X-Forwarded-For``: that header
    is attacker-supplied, so keying on it would let an attacker rotate a fake value
    per request and reset their own per-IP budget - the very bypass this finding is
    about. Behind the load balancer every request already arrives from the proxy, so
    the peer is stable.

    ``X-Forwarded-For`` is still appended when ``TRUST_FORWARDED_FOR_HEADER`` is
    enabled (set it only behind a proxy that appends the true client address), which
    narrows the key from the shared proxy address to the individual client. Even then
    the value is validated as an IP address and the socket peer stays in the key, so a
    forged header cannot escape the proxy's own budget.

    The **last** entry is taken, not the first. A proxy *appends* the address it saw,
    so on AWS ALB the header arrives as ``<anything the client sent>, <real client>``
    and only the rightmost element was written by the proxy. Reading the leftmost
    element would hand the key straight back to the attacker, who could then rotate it
    per request - the same bypass keying on the header at all was meant to prevent.
    This mirrors how uvicorn's own ``ProxyHeadersMiddleware`` scans the list in
    reverse for the first host it did not put there.
    """
    peer = _socket_peer(request)
    if not TRUST_FORWARDED_FOR_HEADER:
        return peer[:64]

    forwarded = ""
    try:
        hops = [h.strip() for h in (request.headers.get("X-Forwarded-For") or "").split(",")]
        forwarded = next((h for h in reversed(hops) if h), "")
    except Exception:
        forwarded = ""
    # Only a syntactically valid IP is accepted, which also drops CR/LF and any
    # other injected text before the value can reach a log or a DB row.
    try:
        forwarded = str(ip_address(forwarded)) if forwarded else ""
    except ValueError:
        forwarded = ""
    if forwarded:
        return f"{peer}|{forwarded}"[:64]
    return peer[:64]
