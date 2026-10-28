from __future__ import annotations

import os
import re
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from fastapi import Request
from jwt.exceptions import PyJWTError as JWTError
from slowapi.util import get_remote_address
from sqlalchemy import func
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from core.runtime import SessionLocal, logger
from models.domain import RateLimitSetting, User
from utils.input_validation import normalize_email
from utils.jwt_access import decode_access_token
from utils.security_helpers import log_safe

# Defaults only apply to a role with no row in RateLimitSetting - the Super Admin panel's
# "Rate Limiting" tab is the live control surface and always wins (see get_role_limits).
#
# The old authenticated defaults were far too low for how the app actually behaves. A trainee
# opening a course fans out to several endpoints at once, and one tutor paragraph costs a
# /speech/synthesize call plus SPEECH_PREFETCH_AHEAD more, so a single lecture spends requests
# faster than a page of dashboards does. UAT proved the point: a trainee doing nothing unusual
# got "Rate limit exceeded: 100/minute" while their course list was still loading. A limit that
# ordinary use trips is not protecting anything, it is just an outage with a different message.
#
# `anonymous` deliberately stays at 100/minute. It is the pre-login budget, and raising it would
# weaken a control that VAPT work relies on; credential-submitting endpoints are capped tighter
# still in _AUTH_ENDPOINT_LIMITS, and utils/login_throttle.py adds the per-account lockout.
# Authenticated callers are attributable (the key embeds the JWT uid), so a generous ceiling
# there costs far less than a false positive does.
DEFAULT_RATE_LIMITS: Dict[str, str] = {
    "trainee": "1000/minute",
    "trainer": "1000/minute",
    "super_admin": "1000/minute",
    "anonymous": "100/minute",
}

RATE_LIMIT_PATTERN = re.compile(r"^\d+/(second|minute|hour|day)$", re.IGNORECASE)

_PERIOD_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}

_RATE_LIMITS_CACHE: Dict[str, str] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_EXPIRES_AT = 0.0
_CACHE_TTL_SECONDS = 60.0
# One in-flight background reload at a time, so an expired cache under load cannot start a
# thread per request.
_REFRESH_IN_PROGRESS = False

# In-memory sliding window: key -> request timestamps
_REQUEST_TIMESTAMPS: Dict[str, List[float]] = defaultdict(list)
_TIMESTAMPS_LOCK = threading.Lock()

# Short-lived cache for role lookups on legacy JWTs without embedded role
_USER_ROLE_CACHE: Dict[str, Tuple[str, float]] = {}
_USER_ROLE_CACHE_TTL = 300.0

_RATE_LIMIT_SKIP_PREFIXES = (
    "/static/",
    "/temp/",
    "/status",
    "/api/webfonts/",
)

# NOTE: /auth/token and /auth/signup are deliberately NOT exempt. Exempting login
# is what let the VAPT Intruder run push 37 guesses through with a single 429
# (VAPT: Password Bruteforcible). Unauthenticated hits fall under the "anonymous"
# role limit here, and utils/login_throttle.py adds the per-account limit,
# progressive delay and lockout on top.
_RATE_LIMIT_EXEMPT_PATHS = frozenset({
    "/",
    "/notifications",
    "/auth/me",
    "/auth/logout",
})

def validate_rate_limit_string(limit: str) -> bool:
    """
    True for a limit this app can actually enforce, like '200/minute'.

    A zero count is rejected even though the pattern's \\d+ matches it. Nothing here treats 0 as
    "unlimited" - _is_request_allowed refuses as soon as the count is reached, so '0/minute'
    means "deny every request" and would be a total outage for whatever it guards. That is a
    plausible thing for someone to type meaning the opposite, and there are three ways in: an
    AUTH_*_RATE_LIMIT in a task definition, a RateLimitSetting row, and the Super Admin panel's
    Rate Limiting tab - where saving 0 for a role would lock every user of that role out. All
    three route through here, so refusing it once covers them: the env path falls back to its
    safety net, a stored row is ignored in favour of the role default, and the panel returns its
    existing "use format like '100/minute'" error instead of saving.
    """
    stripped = (limit or "").strip()
    if not RATE_LIMIT_PATTERN.match(stripped):
        return False
    return int(stripped.split("/", 1)[0]) > 0


def _auth_limit_from_config(name: str, safety_net: str) -> str:
    """
    Read a credential-endpoint ceiling from configuration, with a last-resort safety net.

    Invalid text is ignored rather than fatal, for the same reason the pool settings behave that
    way: a typo in a task definition must not stop the app booting for every role. The net is a
    real limit, never "unlimited", so a missing key cannot silently remove the control.
    """
    configured = (os.environ.get(name) or "").strip()
    if configured and validate_rate_limit_string(configured):
        return configured
    if configured:
        logger.warning(
            "%s=%s is not a valid rate limit (expected a value like '200/minute'), so the "
            "safety net %s is in force instead.",
            name,
            log_safe(configured),
            safety_net,
        )
    return safety_net


# Per-IP ceiling for credential-submitting endpoints, separate from the "anonymous" browsing
# limit (100/minute) so brute-force pressure on login is capped independently (VAPT: Password
# Bruteforcible).
#
# The key is the socket peer alone (see the middleware below), and TRUST_FORWARDED_FOR_HEADER is
# off, so behind the ALB every caller is attributed to the proxy's address: this ceiling is shared
# by the entire cohort, not held per person. At the previous hardcoded 10/minute a training class
# of 50-70 signing in together exhausted it within seconds and the rest were told "Too many
# requests" - a limit ordinary use trips is an outage with a nicer message. The value is now
# configuration, like the pool sizes, so DevOps can size it to the cohort without a release.
#
# Raising it does not weaken the brute-force control that matters: utils/login_throttle.py caps
# failures PER ACCOUNT at LOGIN_MAX_FAILED_PER_ACCOUNT (5) in a rolling window, with progressive
# delay and lockout on top, and those counters are DB-backed and per account, so they are immune
# to how many users share an address.
#
# These same values are the per-route slowapi ceilings in features/general/router.py, which is
# the OTHER enforcement point on the very same requests: `@limiter_with_db.limit(...)` runs per
# endpoint and its key is the caller (see rate_limit_key_func). Both read from here so a value
# cannot be raised in one place and silently left tight in the other - which is exactly what a
# hardcoded "10/minute" on the handler did while this dict said something else.
AUTH_TOKEN_RATE_LIMIT = _auth_limit_from_config("AUTH_TOKEN_RATE_LIMIT", "200/minute")
AUTH_SSO_RATE_LIMIT = _auth_limit_from_config("AUTH_SSO_RATE_LIMIT", "200/minute")
# Signup is genuinely rare per person but a whole intake can self-register in one sitting, and
# an hour window is the right shape for it. SIGNUP_ALLOWED_EMAIL_DOMAINS is the real gate here.
AUTH_SIGNUP_RATE_LIMIT = _auth_limit_from_config("AUTH_SIGNUP_RATE_LIMIT", "200/hour")
# Read once per login page load, so it must clear the size of a cohort arriving together; it
# discloses nothing and writes nothing.
AUTH_SSO_CONFIG_RATE_LIMIT = _auth_limit_from_config("AUTH_SSO_CONFIG_RATE_LIMIT", "600/minute")
# Authenticated, so it is counted per user rather than per address - this only has to cover one
# person's session, not the cohort's.
AUTH_REFRESH_RATE_LIMIT = _auth_limit_from_config("AUTH_REFRESH_RATE_LIMIT", "60/minute")
# Populates the signup page's process dropdown, so it is anonymous and cohort-shared.
PROCESSES_RATE_LIMIT = _auth_limit_from_config("PROCESSES_RATE_LIMIT", "600/minute")

_AUTH_ENDPOINT_LIMITS = {
    "/auth/token": AUTH_TOKEN_RATE_LIMIT,
    "/auth/signup": AUTH_SIGNUP_RATE_LIMIT,
}


def _parse_limit(limit: str) -> Tuple[int, int]:
    """Return (max_requests, window_seconds) from strings like '100/minute'."""
    count_str, period = limit.strip().lower().split("/", 1)
    return int(count_str), _PERIOD_SECONDS[period]


def _reload_role_limits_now() -> None:
    """Load from the database and replace the cache. Blocks; never raises."""
    global _CACHE_EXPIRES_AT
    loaded = _load_role_limits_from_db()
    with _CACHE_LOCK:
        _RATE_LIMITS_CACHE.clear()
        _RATE_LIMITS_CACHE.update(loaded)
        _CACHE_EXPIRES_AT = time.time() + _CACHE_TTL_SECONDS


def _refresh_role_limits_in_background() -> None:
    """
    Reload the cache off the request path, one refresh at a time.

    Whoever notices the cache has expired must not be the one to pay for the reload. This is
    reached from RoleRateLimitMiddleware.dispatch, which is `async def`, so the query would run
    on the event loop - and a synchronous query there blocks every other request in the worker,
    not just this one. When the pool is saturated that wait is DB_POOL_TIMEOUT (30s), so a
    single expired cache entry could freeze a whole uvicorn worker for half a minute during
    exactly the incident the limits exist to survive. The caller keeps serving the previous
    values while this runs.
    """
    global _REFRESH_IN_PROGRESS
    with _CACHE_LOCK:
        if _REFRESH_IN_PROGRESS:
            return
        _REFRESH_IN_PROGRESS = True

    def _run() -> None:
        global _REFRESH_IN_PROGRESS
        try:
            _reload_role_limits_now()
        finally:
            with _CACHE_LOCK:
                _REFRESH_IN_PROGRESS = False

    threading.Thread(target=_run, name="rate-limits-refresh", daemon=True).start()


def invalidate_rate_limits_cache() -> None:
    """
    Drop the cached limits after the Super Admin panel saves a change.

    The values are reloaded here rather than cleared, so the panel's change is live immediately
    and no request in the gap is limited by DEFAULT_RATE_LIMITS instead of the stored rows - a
    clear would have meant that saving one role's limit briefly reset every other role's. Called
    from a sync endpoint, which runs in a worker thread, so blocking here is safe.
    """
    _reload_role_limits_now()


def _load_role_limits_from_db() -> Dict[str, str]:
    limits = dict(DEFAULT_RATE_LIMITS)
    db = SessionLocal()
    try:
        for row in db.query(RateLimitSetting).all():
            if row.role and row.limit and validate_rate_limit_string(row.limit):
                limits[row.role] = row.limit.strip()
    except Exception as exc:
        logger.error("Failed to load rate limits from database: %s", exc, exc_info=True)
    finally:
        db.close()
    return limits


def get_role_limits() -> Dict[str, str]:
    """
    The current per-role limits. Never queries the database on the calling thread.

    Serve-stale-then-refresh: an expired cache still answers this request with the values it
    already has and reloads behind it. Every request crosses this function from inside async
    middleware, so a query here would run on the event loop - see
    _refresh_role_limits_in_background for what that costs when the pool is saturated.
    """
    now = time.time()
    with _CACHE_LOCK:
        cached = dict(_RATE_LIMITS_CACHE)
        if cached and now < _CACHE_EXPIRES_AT:
            return cached

    _refresh_role_limits_in_background()
    # Falling back to the defaults only happens if warm_rate_limits_cache() could not reach the
    # database at startup, in which case a query here would not have worked either.
    return cached or dict(DEFAULT_RATE_LIMITS)


def warm_rate_limits_cache() -> None:
    """
    Fill the cache at startup, before any request depends on it.

    Deliberately blocking, and deliberately not routed through get_role_limits(): this runs once
    during startup where a query is free to take its time, and doing it here is what keeps the
    request path query-free.
    """
    try:
        _reload_role_limits_now()
    except Exception as exc:
        logger.warning("Could not pre-warm rate limit cache: %s", exc)


def get_limit_for_role(role: Optional[str]) -> str:
    limits = get_role_limits()
    if role and role in limits:
        return limits[role]
    return limits.get("anonymous", DEFAULT_RATE_LIMITS["anonymous"])


def _lookup_role_for_email(email: str) -> Optional[str]:
    """
    Resolve role for legacy tokens that only carry the email subject.

    Keyed and matched on the canonical (lower-cased) address: casing variants of the
    same account must share one cache entry and one role, otherwise a caller could get
    a separate rate-limit identity per casing and multiply their effective quota.
    """
    normalized = normalize_email(email)
    if not normalized:
        return None

    now = time.time()
    cached = _USER_ROLE_CACHE.get(normalized)
    if cached and now < cached[1]:
        return cached[0]

    db = SessionLocal()
    try:
        user = db.query(User).filter(func.lower(User.email) == normalized).first()
        if user and user.role:
            _USER_ROLE_CACHE[normalized] = (user.role, now + _USER_ROLE_CACHE_TTL)
            return user.role
    except Exception as exc:
        # A SQLAlchemy error message embeds the bound parameters, which here includes the
        # caller-supplied email, so the text is user-influenced. exc_info=True is dropped
        # for the same reason: the traceback would reprint those values.
        logger.error("Failed to lookup role for rate limiting: %s", log_safe(exc))
    finally:
        db.close()
    return None


def resolve_request_rate_limit_identity(request: Request) -> Tuple[str, str]:
    role = "anonymous"
    identity = get_remote_address(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            try:
                payload = decode_access_token(token)
                email = payload.get("sub")
                role = payload.get("role") or role
                identity = payload.get("uid") or email or identity
                if not payload.get("role") and email:
                    looked_up_role = _lookup_role_for_email(email)
                    if looked_up_role:
                        role = looked_up_role
            except JWTError:
                pass

    return role or "anonymous", identity


def _is_request_allowed(key: str, limit_str: str) -> bool:
    max_requests, window_seconds = _parse_limit(limit_str)
    now = time.time()
    window_start = now - window_seconds

    with _TIMESTAMPS_LOCK:
        timestamps = [t for t in _REQUEST_TIMESTAMPS[key] if t > window_start]
        if len(timestamps) >= max_requests:
            _REQUEST_TIMESTAMPS[key] = timestamps
            return False
        timestamps.append(now)
        _REQUEST_TIMESTAMPS[key] = timestamps
        return True


def should_skip_rate_limit(request: Request) -> bool:
    path = request.url.path or ""
    if path in _RATE_LIMIT_EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _RATE_LIMIT_SKIP_PREFIXES)


class RoleRateLimitMiddleware(BaseHTTPMiddleware):
    """Lightweight async role-based rate limiting (no slowapi global middleware)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if should_skip_rate_limit(request):
            return await call_next(request)

        role, identity = resolve_request_rate_limit_identity(request)
        path = request.url.path or ""

        # Credential-submitting endpoints get their own tight per-IP ceiling instead
        # of the broad per-role browsing limit (VAPT: Password Bruteforcible).
        auth_limit = _AUTH_ENDPOINT_LIMITS.get(path)
        if auth_limit:
            limit_str = auth_limit
            key = f"auth:{path}:{get_remote_address(request)}"
        else:
            limit_str = get_limit_for_role(role)
            key = f"{role}:{identity}"

        if not _is_request_allowed(key, limit_str):
            # Both values are user-influenced - `path` comes straight off the request line
            # and `key` embeds the JWT `uid`/email for an authenticated caller - so they go
            # through log_safe() (Sonar python:S5145 / CodeQL log injection).
            logger.warning(
                "Rate limit exceeded for %s on %s", log_safe(key), log_safe(path)
            )
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded: {limit_str}"},
                headers={"Retry-After": str(_parse_limit(limit_str)[1])},
            )

        return await call_next(request)


def register_rate_limit_middleware(app) -> None:
    if getattr(app.state, "_rate_limit_middleware_registered", False):
        return
    app.add_middleware(RoleRateLimitMiddleware)
    app.state._rate_limit_middleware_registered = True
