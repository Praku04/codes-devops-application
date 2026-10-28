from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import boto3
import fitz  # PyMuPDF
import numpy as np
from agno.agent import Agent
from agno.models.aws import AwsBedrock
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ClientError as BedrockClientError
from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import jwt
from jwt.exceptions import PyJWTError as JWTError
from langchain_aws import BedrockEmbeddings
from langchain_pinecone import Pinecone
from passlib.context import CryptContext
from pinecone import NotFoundException, Pinecone as PineconeClient, ServerlessSpec
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError
import speech_recognition as sr
from pydub import AudioSegment
from sklearn.metrics.pairwise import cosine_similarity
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    desc,
    event,
    func,
    or_,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker
from sqlalchemy.pool import NullPool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from werkzeug.utils import secure_filename

# Only first-party import in this module, and deliberately so: `core.secrets_manager`
# imports nothing from this project (stdlib + boto3 only), so it cannot create a cycle
# and it is safe to resolve configuration from it before anything else runs.
from .secrets_manager import load_secrets_into_environ

try:
    from docx import Document

    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    from pptx import Presentation

    PPTX_AVAILABLE = True
except ImportError:
    PPTX_AVAILABLE = False

try:
    import comtypes.client

    COMTYPES_AVAILABLE = True
except ImportError:
    COMTYPES_AVAILABLE = False

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

load_dotenv()

LOG_FILE_NAME = "app.log"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(module)s.%(funcName)s:%(lineno)d - %(message)s"


def setup_logging():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
    formatter = logging.Formatter(LOG_FORMAT)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    file_handler = RotatingFileHandler(
        LOG_FILE_NAME,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


setup_logging()
logger = logging.getLogger(__name__)

# Configuration comes from AWS Secrets Manager, not from the ECS task definition's
# `environment` array. This is deliberately the first statement after logging is wired
# and `.env` is read, and before any module-level setting is looked at (the earliest one
# is CSP_REPORT_ONLY further down this file). Because `main.py` imports this module
# first and this module has no other first-party imports, every environment lookup in
# the process - in every module - sees the resolved secret. It is a no-op locally unless
# AWS_SECRETS_MANAGER_SECRET_ID is set, so `.env` still drives a developer machine.
load_secrets_into_environ()


def _env_first(*keys: str) -> Optional[str]:
    for key in keys:
        value = os.environ.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _parse_allowed_origins() -> list[str]:
    """
    Build the CORS origin allow-list (VAPT: Cross Origin Resource Sharing).

    ``allow_origins=["*"]`` combined with ``allow_credentials=True`` makes Starlette
    echo the caller's ``Origin`` back verbatim, so any site could read authenticated
    responses. Origins are therefore read from ``CORS_ALLOWED_ORIGINS`` (comma
    separated) and screened here.

    The UI is served by this same app and calls the API with
    ``window.location.origin``, so same-origin traffic needs no entry at all; the
    list stays empty unless a separately hosted front end is explicitly configured.

    Each entry must be a bare scheme://host[:port] origin. Wildcards, paths,
    credentials and anything non-HTTP(S) are dropped, so a stray ``*`` in the
    environment cannot silently reopen this finding.
    """
    raw = _env_first("CORS_ALLOWED_ORIGINS", "ALLOWED_ORIGINS")
    if not raw:
        return []

    allowed: list[str] = []
    for candidate in raw.split(","):
        origin = candidate.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            logger.warning("CORS: ignoring wildcard origin '*' (credentialed CORS forbids it).")
            continue
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            logger.warning("CORS: ignoring malformed origin entry (expected scheme://host[:port]).")
            continue
        # An origin is scheme + host + port only. Reject anything carrying a path,
        # query, fragment or userinfo, which would never match a browser Origin header.
        if parsed.path or parsed.params or parsed.query or parsed.fragment or "@" in parsed.netloc:
            logger.warning("CORS: ignoring origin entry with a path/credentials component.")
            continue
        normalized = f"{parsed.scheme}://{parsed.netloc}"
        if normalized not in allowed:
            allowed.append(normalized)

    if allowed:
        logger.info("CORS: %s cross-origin entr(y/ies) configured.", len(allowed))
    return allowed


def _build_database_connection_url() -> str:
    """Build PostgreSQL SQLAlchemy URL from username, password, DB_HOST, DB_PORT, DB_NAME."""
    username = _env_first("username", "DB_USERNAME", "POSTGRES_USER")
    password = _env_first("password", "DB_PASSWORD", "POSTGRES_PASSWORD")
    host = _env_first("DB_HOST", "DATABASE_HOST", "POSTGRES_HOST")
    port = _env_first("DB_PORT", "DATABASE_PORT", "POSTGRES_PORT") or "5432"
    db_name = _env_first("DB_NAME", "DATABASE_NAME", "POSTGRES_DB") or "postgres"

    if not username or password is None:
        raise ValueError(
            "Database credentials missing: set username, password, and DB_HOST in the environment."
        )
    if not host:
        raise ValueError(
            "DB_HOST must be set when using username/password database credentials."
        )

    user_enc = quote_plus(username)
    password_enc = quote_plus(password)
    return f"postgresql://{user_enc}:{password_enc}@{host}:{port}/{db_name}"


class Base(DeclarativeBase):
    pass


app = FastAPI(
    title="Smart Tutor V4 API's",
    description=(
        "The Smart Tutor V4 API's provides a unified agentic framework that automates "
        "module generation while managing distinct workflows for Super Admins "
        "(system oversight), Trainers (curriculum design), and Trainees "
        "(personalized AI tutoring)"
    ),
)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Fields whose rejected value must never be echoed back (VAPT: Weak Password Accepted).
# Pydantic's default 422 body carries an `input` key holding the value that failed, so a
# password refused by the strength gate would be reflected straight back in the response
# and into any client-side log or toast. The rule message is kept - the user needs to know
# WHY it was refused - but the submitted secret is dropped.
_SENSITIVE_VALIDATION_FIELDS = frozenset(
    {"password", "new_password", "current_password", "confirm_password", "token",
     "access_token", "id_token", "secret", "api_key"}
)


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Return the standard 422 shape with sensitive submitted values removed.

    `url` is dropped as well: it is only a link to the Pydantic docs and adds noise to
    the message the front end surfaces. Nothing here is logged, so a rejected password
    never reaches the log file either (Sonar python:S5145 / CodeQL log injection).
    """
    cleaned: List[Dict[str, Any]] = []
    for error in exc.errors():
        item = {k: v for k, v in error.items() if k != "url"}
        location = item.get("loc") or ()
        is_sensitive = any(
            isinstance(part, str) and part.lower() in _SENSITIVE_VALIDATION_FIELDS
            for part in location
        )
        if is_sensitive:
            item.pop("input", None)

        # `ctx["error"]` is the ValueError OBJECT, not a string. Left alone,
        # jsonable_encoder serializes it to `{}` (an exception carries no useful
        # __dict__), silently dropping the message that explains the rule - so stringify
        # it for every field, sensitive or not.
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {
                key: (str(value) if isinstance(value, BaseException) else value)
                # For a sensitive field keep only `error`: other ctx keys can carry the
                # rejected value itself, depending on which validator raised.
                for key, value in ctx.items()
                if not is_sensitive or key == "error"
            }

        # `msg`/`ctx["error"]` are our own validator strings from
        # utils/input_validation.py, never caller-supplied text, so they are safe to
        # return verbatim - and they are what tells the user which rule failed.
        cleaned.append(jsonable_encoder(item))
    return JSONResponse(status_code=422, content={"detail": cleaned})


app.add_exception_handler(RequestValidationError, _validation_exception_handler)

TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)
app.mount("/temp", StaticFiles(directory=TEMP_DIR), name="temp")

# CORS is restricted to an explicit allow-list (VAPT: Cross Origin Resource Sharing).
# Credentials stay disabled: auth is a Bearer token that the front end attaches per
# request, and no endpoint sets a cookie, so credentialed CORS is not needed. Keeping
# it off also means a reflected Origin could never expose an authenticated response.
# Methods and headers are enumerated rather than "*" so a new verb or header has to be
# added deliberately. DELETE/PUT/PATCH are deliberately absent: deletes and updates are
# exposed as POST `<path>/delete` and `<path>/update` routes instead
# (VAPT: HTTP DELETE Method Enabled), so no route serves those verbs.
CORS_ALLOWED_ORIGINS = _parse_allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    expose_headers=["Content-Disposition"],
    max_age=600,
)


class LargeUploadMiddleware(BaseHTTPMiddleware):
    """Remove body size limits for video upload endpoint."""

    async def dispatch(self, request: StarletteRequest, call_next):
        if request.url.path == "/video-analysis/upload":
            request.scope["_body_size_limit"] = None
        return await call_next(request)


app.add_middleware(LargeUploadMiddleware)


# ── Security response headers (VAPT: Missing security headers) ───────────────
# The origin previously answered with only content-length/content-type/date/server, so
# every protection below was absent. They are built once at import and applied by
# SecurityHeadersMiddleware to every response, including error responses and static files.

# Paths whose responses are allowed to stay cacheable. Everything else gets no-store.
# Blanket no-store would defeat the deliberate `public, max-age=86400` on the Font Awesome
# webfonts and force the ~1 MB of JS to be re-downloaded on every navigation, for no
# security gain: these are public, unauthenticated, non-personalised assets.
_CACHEABLE_PATH_PREFIXES = ("/static/", "/temp/", "/api/webfonts/")

# The app's real script/style/connect origins, enumerated rather than wildcarded.
#   cdnjs.cloudflare.com  - showdown + jsdiff, loaded in templates/index.html with SRI
#                           hashes and crossorigin="anonymous". Also needed on
#                           connect-src: DevTools/sourceMappingURL fetches the
#                           non-min sibling (showdown.js) as a connect, not a script.
#   cdn.sheetjs.com       - XLSX, injected on demand by the batch-analytics export.
#   cdn.jsdelivr.net      - Swagger UI assets on /docs.
#   fonts.googleapis.com  - the Manrope/Reddit Mono stylesheet (a <link>, so style-src).
#   fonts.gstatic.com     - the woff2 files that stylesheet references (font-src).
#   login.microsoftonline.com - Entra ID, contacted by MSAL for the SSO redirect flow.
#   *.okta.com / oktapreview / okta-emea - Okta token endpoint when SSO_PROVIDER=okta
#                           (origin taken from OKTA_ISSUER only; same host allow-list as
#                           features/general/sso_helper.py — kept local to avoid importing
#                           the features package from runtime and creating a cycle).
_OKTA_CSP_HOST_SUFFIXES = (".okta.com", ".oktapreview.com", ".okta-emea.com")


def _okta_issuer_origin_for_csp() -> str:
    """Return https://{okta-host} when OKTA_ISSUER is a safe Okta URL; else empty."""
    raw = (os.environ.get("OKTA_ISSUER") or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    host = (parsed.hostname or "").lower()
    if not host or ".." in host:
        return ""
    if parsed.port not in (None, 443):
        return ""
    if not any(host == suffix[1:] or host.endswith(suffix) for suffix in _OKTA_CSP_HOST_SUFFIXES):
        return ""
    return f"https://{host}"


def _sso_csp_connect_hosts() -> str:
    hosts = ["https://login.microsoftonline.com"]
    okta_origin = _okta_issuer_origin_for_csp()
    if okta_origin:
        hosts.append(okta_origin)
    return " ".join(hosts)


_CSP_SCRIPT_HOSTS = "https://cdnjs.cloudflare.com https://cdn.sheetjs.com https://cdn.jsdelivr.net"
_CSP_CONNECT_HOSTS = f"{_sso_csp_connect_hosts()} {_CSP_SCRIPT_HOSTS}"

# `'unsafe-inline'` is required, not laziness. templates/index.html carries two inline
# <script> blocks (the session-restore bootstrap and the auth canvas), static/script.js
# builds markup containing ~150 inline `onclick=`/`onchange=` handlers, and the template
# uses 71 inline `style=` attributes plus a <style> block. A nonce cannot cover event-handler
# attributes at all, so dropping 'unsafe-inline' would take the UI down without a full
# rewrite of the rendering layer. The directives that do the load-bearing work here are
# `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'` and `form-action 'self'`,
# none of which are weakened by 'unsafe-inline'.
_DEFAULT_CSP_DIRECTIVES = (
    "default-src 'self'",
    f"script-src 'self' 'unsafe-inline' {_CSP_SCRIPT_HOSTS}",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net",
    # data: covers the inline SVG favicon and the CSS background patterns; blob: covers
    # images fetched with an Authorization header and handed to createObjectURL.
    "img-src 'self' data: blob:",
    "font-src 'self' https://fonts.gstatic.com data:",
    f"connect-src 'self' {_CSP_CONNECT_HOSTS}",
    # Polly audio and recorded answers are played back from object URLs.
    "media-src 'self' blob: data:",
    # blob: is the PDF preview iframe; the tutor widget iframes are srcdoc + sandbox.
    "frame-src 'self' blob:",
    "child-src 'self' blob:",
    "worker-src 'self' blob:",
    # Refuse to be framed at all (clickjacking). Paired with X-Frame-Options for old agents.
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "upgrade-insecure-requests",
    # Obsolete in current Chrome (superseded by upgrade-insecure-requests) and ignored
    # there, but still honoured by older engines and expected by the scanner.
    "block-all-mixed-content",
)

# Full override, for tightening or loosening without a code change.
CONTENT_SECURITY_POLICY = (
    os.environ.get("CONTENT_SECURITY_POLICY", "").strip()
    or "; ".join(_DEFAULT_CSP_DIRECTIVES)
)

# Report-only ships the same policy as `Content-Security-Policy-Report-Only`: browsers log
# violations and block nothing. Useful for one deploy when adding a new third-party asset.
# Default OFF - the policy must actually enforce to close this finding.
CSP_REPORT_ONLY = os.environ.get(
    "CSP_REPORT_ONLY", "false"
).strip().lower() in {"1", "true", "yes", "on"}
_CSP_HEADER_NAME = (
    "Content-Security-Policy-Report-Only" if CSP_REPORT_ONLY else "Content-Security-Policy"
)


def _hsts_max_age() -> int:
    """
    Seconds for Strict-Transport-Security. 0 disables the header entirely.

    Kept configurable because HSTS is the one header here that can lock a domain out:
    once a browser has cached it, that host is HTTPS-only for the whole duration and the
    user cannot click through. 1 year is the standard value and what the finding asks for.
    """
    raw = os.environ.get("HSTS_MAX_AGE_SECONDS", "31536000").strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning("HSTS_MAX_AGE_SECONDS is not an integer; using the 31536000 default.")
        return 31536000
    return max(value, 0)


HSTS_MAX_AGE_SECONDS = _hsts_max_age()

_STATIC_SECURITY_HEADERS: Dict[str, str] = {
    # Honour the declared Content-Type instead of sniffing the bytes. Also the last link in
    # the upload chain (VAPT: Double Extension File Upload): even if a deceptive name
    # reached storage, the browser must not re-interpret it as something executable.
    "X-Content-Type-Options": "nosniff",
    # Legacy clickjacking control. Modern browsers use CSP frame-ancestors above; this is
    # for agents that predate it.
    "X-Frame-Options": "DENY",
    # Retired in Chrome/Firefox (the auditor it toggled was removed) and superseded by CSP,
    # but still read by older engines and expected by the scanner.
    "X-XSS-Protection": "1; mode=block",
    # Send the full URL same-origin, bare origin cross-origin over HTTPS, nothing on a
    # downgrade - so a document id or filename in a path never leaks to a third party.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Everything the app does not use is denied outright. `microphone=(self)` is a
    # DELIBERATE departure from the finding's `microphone=()`: Voice Training and the chat
    # recorder both call navigator.mediaDevices.getUserMedia(), so `()` would disable the
    # feature the product exists for. `(self)` still blocks any embedded third-party frame
    # from reaching the microphone, which is the actual risk.
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
        "microphone=(self), payment=(), usb=()"
    ),
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Attach the protective response headers (VAPT: Missing security headers).

    `setdefault` throughout, so a route that has already made a deliberate choice - the
    webfont handler's `public, max-age=86400`, for instance - keeps it.
    """

    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        headers = response.headers

        for name, value in _STATIC_SECURITY_HEADERS.items():
            headers.setdefault(name, value)

        headers.setdefault(_CSP_HEADER_NAME, CONTENT_SECURITY_POLICY)

        if HSTS_MAX_AGE_SECONDS:
            # Sent unconditionally. TLS terminates at the ALB, so the request reaching
            # uvicorn looks like plain HTTP and `request.url.scheme` cannot be used to
            # decide - and a browser ignores HSTS received over HTTP anyway, so there is
            # no way for this to mis-fire on a local http:// run.
            headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={HSTS_MAX_AGE_SECONDS}; includeSubDomains",
            )

        path = request.url.path or ""
        if not path.startswith(_CACHEABLE_PATH_PREFIXES):
            # API responses carry course content, assessment answers and user records, and
            # the SPA shell is per-session. None of it may sit in a shared proxy cache or
            # on disk for the next user of the machine.
            headers.setdefault("Cache-Control", "no-store, max-age=0, must-revalidate")
            headers.setdefault("Pragma", "no-cache")
            headers.setdefault("Expires", "0")

        return response


app.add_middleware(SecurityHeadersMiddleware)

def _env_int_or_default(name: str, default: int, *, minimum: int | None = None) -> int:
    """
    Parse an integer env var safely.

    Blank / whitespace / non-integer values (common when a secret-store entry exists but
    is left empty) fall back to ``default`` instead of raising at import and taking the
    process down. Optional ``minimum`` clamps the result (e.g. floor of 1 for timeouts).
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("%s is not an integer; using default %s.", name, default)
            value = default
    if minimum is not None:
        return max(value, minimum)
    return value


# Pool settings whose value had to come from the built-in safety net because the environment
# did not supply one. Reported by name at startup: see the pool block further down for why the
# net exists at all, and why it must never be mistaken for the configuration to deploy.
_pool_settings_missing_from_config: List[str] = []


def _pool_setting(name: str, safety_net: int, *, minimum: int) -> int:
    """
    Read one DevOps-owned pool setting, remembering whether configuration actually supplied it.

    Deliberately separate from _env_int_or_default: this app must not present a number baked
    into its own source as if it were the tuned value for a particular database instance. The
    only difference in behaviour is the record kept here, so a missing key can be named out
    loud at startup rather than silently becoming the thing the pool is sized by.
    """
    if not (os.environ.get(name) or "").strip():
        _pool_settings_missing_from_config.append(name)
    return _env_int_or_default(name, safety_net, minimum=minimum)


def _env_float_or_default(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        value = default
    else:
        try:
            value = float(raw)
        except ValueError:
            logger.warning("%s is not a number; using default %s.", name, default)
            value = default
    return max(value, minimum)


JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
# Pinned signing algorithm for app access tokens (VAPT: Improper Token Validation).
# Do not expand this list — rejects alg=none / algorithm confusion.
JWT_ALGORITHM = "HS256"
JWT_ISSUER = os.environ.get("JWT_ISSUER", "smarttutor-ai").strip() or "smarttutor-ai"
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "smarttutor-ai-api").strip() or "smarttutor-ai-api"
JWT_LEEWAY_SECONDS = _env_int_or_default("JWT_LEEWAY_SECONDS", 60, minimum=0)
# Short-lived access tokens (VAPT: Improper Token Expiration). Override via env if needed.
ACCESS_TOKEN_EXPIRE_MINUTES = _env_int_or_default("ACCESS_TOKEN_EXPIRE_MINUTES", 30, minimum=1)
# Idle timeout: revoke session after no API activity (minutes).
# Default 120 matches CLIENT_IDLE / credential docs (2 hours of true idleness).
SESSION_IDLE_TIMEOUT_MINUTES = _env_int_or_default("SESSION_IDLE_TIMEOUT_MINUTES", 120, minimum=1)
# Absolute timeout: max session lifetime from login (minutes). Default 8 hours.
SESSION_ABSOLUTE_TIMEOUT_MINUTES = _env_int_or_default(
    "SESSION_ABSOLUTE_TIMEOUT_MINUTES", 480, minimum=1
)
# How often a request may refresh UserSession.last_activity (seconds). See
# validate_and_touch_session in services/auth_platform.py: the idle window above is measured in
# minutes, so stamping the row on EVERY authenticated request bought no accuracy and cost a
# write plus a commit each time. Worse, all of one caller's in-flight requests update the SAME
# row, so they queued behind each other on a row lock while holding a pooled DB connection -
# three concurrent /speech/synthesize calls from one trainee serialized on bookkeeping.
# At 60s the stamp is at most a minute stale, so a session can expire at most a minute early out
# of SESSION_IDLE_TIMEOUT_MINUTES, and it stays well inside the Live Activity panel's
# "Online Now (< 2 min)" bucket. Set to 0 to restore a write on every request.
SESSION_ACTIVITY_WRITE_INTERVAL_SECONDS = _env_int_or_default(
    "SESSION_ACTIVITY_WRITE_INTERVAL_SECONDS", 60, minimum=0
)
# Sliding-window renewal (VAPT: Improper Token Expiration - remediation follow-up).
#
# The access token stays short-lived, so a leaked token still dies in
# ACCESS_TOKEN_EXPIRE_MINUTES. What changes is that an *active* user can trade a still-valid
# token for a fresh one at /auth/refresh, instead of being thrown out mid-task at a fixed
# wall-clock deadline. The security ceiling is unchanged and enforced server-side:
#   - the DB session's idle timer (SESSION_IDLE_TIMEOUT_MINUTES) ends genuinely idle sessions
#   - the DB session's absolute timer (SESSION_ABSOLUTE_TIMEOUT_MINUTES) caps total lifetime
#     from login_time and is NOT extended by refreshing, so this cannot become an endless
#     session no matter how many times the client renews
# Set to 0 to disable renewal entirely and go back to a hard expiry at token age.
# Blank secret-store values must keep the secure default (enabled), not disable renewal.
SESSION_REFRESH_ENABLED = (
    os.environ.get("SESSION_REFRESH_ENABLED") or "true"
).strip().lower() not in ("0", "false", "no", "off")
# How long before `exp` the browser is allowed to renew, and how often it checks.
# The client refreshes at (token lifetime - this), so it never waits for the 401.
SESSION_REFRESH_LEAD_MINUTES = _env_int_or_default("SESSION_REFRESH_LEAD_MINUTES", 5, minimum=1)

# True idle logout, measured from the last REAL user interaction rather than the last HTTP
# request (VAPT: Improper Token Expiration - idle enforcement).
#
# Why this exists: SESSION_IDLE_TIMEOUT_MINUTES above is enforced against `last_activity`,
# which validate_and_touch_session() stamps on EVERY authenticated request. The UI polls
# /notifications once a minute for every role, and that poll is authenticated - so an
# abandoned-but-open tab kept renewing its own idle window and the 30-minute idle timeout
# could never fire. Only the 8-hour absolute cap actually bound such a session.
#
# The browser now tracks genuine interaction (pointer, keyboard, touch, scroll) and, after
# this many minutes with none, stops its own background polling and renewal and logs itself
# out. The server-side timer is unchanged and remains the real enforcement: once the client
# stops polling, `last_activity` stops moving and the DB session times out on its own even
# if the client is bypassed, patched or the tab is killed mid-countdown.
#
# Defaults to SESSION_IDLE_TIMEOUT_MINUTES so the two halves agree unless deliberately
# split. Set to an explicit 0 to disable the client half (server timer still applies).
#
# Parsed defensively rather than with a bare int(): a blank or mistyped value in a secret
# store would make int() raise and take the whole app down at import, and treating such a
# value as 0 would silently disable the control. Only a well-formed "0" disables it; anything
# unparseable falls back to the idle timeout, so a config typo fails SECURE.
def _env_idle_minutes(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if raw.isdigit():
        value = int(raw)
        return min(value, 1440)
    return default


CLIENT_IDLE_LOGOUT_MINUTES = _env_idle_minutes(
    "CLIENT_IDLE_LOGOUT_MINUTES", int(SESSION_IDLE_TIMEOUT_MINUTES)
)
# How long before the idle deadline the user is warned, so a logout is never silent and a
# user who is reading rather than clicking gets a chance to stay signed in. Clamped below
# the idle window itself in the client, since a lead >= the window could never be shown.
CLIENT_IDLE_WARNING_SECONDS = _env_idle_minutes("CLIENT_IDLE_WARNING_SECONDS", 60)

# Microphone input for the Chat Agent and the tutor's Raise Hand is transcribed by AWS
# Transcribe, the same service the Voice Training agent already uses, so recorded audio stays
# inside our own AWS account instead of being sent to a third-party recognizer.
# The browser Web Speech API is the fallback when Transcribe is unavailable.
CHAT_TRANSCRIBE_LANGUAGE = (
    os.environ.get("CHAT_TRANSCRIBE_LANGUAGE", "en-IN").strip() or "en-IN"
)
# How long the browser waits for the AWS transcript before it falls back to what the Web
# Speech API heard, so a slow Transcribe call cannot leave the mic button hanging.
CHAT_TRANSCRIBE_MAX_WAIT_MS = _env_int_or_default("CHAT_TRANSCRIBE_MAX_WAIT_MS", 12000, minimum=1000)

# Login brute-force throttling (VAPT: Password Bruteforcible). Counters are DB-backed
# so they hold across the container's `uvicorn --workers 3` processes.
# Per-account limit is the tighter one: it stops credential stuffing against a single
# user even when the attacker rotates source IPs. Per-IP is looser because a shared
# corporate NAT can legitimately place many users behind one address.
#
# LOGIN_MAX_FAILED_PER_IP is deliberately generous. TRUST_FORWARDED_FOR_HEADER is off (see
# below), so behind the ALB every request is attributed to the proxy's own address and the
# whole cohort shares one counter. At the old value of 20 a training class of 50-70 could
# be locked out for LOGIN_LOCKOUT_MINUTES by twenty mistyped passwords spread across
# twenty different people - including everyone who typed theirs correctly. The control that
# actually stops brute force here is LOGIN_MAX_FAILED_PER_ACCOUNT (5), which is per account
# and so is unaffected by how many users share an address; the per-IP figure is only a
# coarse backstop against one host hammering many accounts.
LOGIN_MAX_FAILED_PER_ACCOUNT = _env_int_or_default("LOGIN_MAX_FAILED_PER_ACCOUNT", 5, minimum=1)
LOGIN_MAX_FAILED_PER_IP = _env_int_or_default("LOGIN_MAX_FAILED_PER_IP", 200, minimum=1)
# Rolling window the failures are counted over, and the lockout applied on breach.
LOGIN_FAILURE_WINDOW_MINUTES = _env_int_or_default("LOGIN_FAILURE_WINDOW_MINUTES", 15, minimum=1)
LOGIN_LOCKOUT_MINUTES = _env_int_or_default("LOGIN_LOCKOUT_MINUTES", 15, minimum=1)
# Progressive delay: seconds added per failure already on record, capped.
# Slows scripted guessing without making a human's first retry feel broken.
LOGIN_PROGRESSIVE_DELAY_SECONDS = _env_float_or_default("LOGIN_PROGRESSIVE_DELAY_SECONDS", 0.5)
LOGIN_PROGRESSIVE_DELAY_MAX_SECONDS = _env_float_or_default(
    "LOGIN_PROGRESSIVE_DELAY_MAX_SECONDS", 4.0
)
# Off by default, and the default is the safe choice for this deployment: the app runs
# on ECS behind an ALB, and an ALB *appends* to `X-Forwarded-For` rather than replacing
# it, so the header always contains whatever the client chose to send. Leaving this off
# keys the throttle on the socket peer, which the client cannot forge.
# Turning it on only helps if the per-IP limit is being hit by many users sharing one
# corporate NAT address, since it narrows the key to the individual client. Even then
# only the proxy-appended (rightmost) hop is read, the value must parse as an IP, and
# the socket peer stays part of the key - see utils/login_throttle.client_ip().
# Blank values stay off (secure default).
TRUST_FORWARDED_FOR_HEADER = (
    os.environ.get("TRUST_FORWARDED_FOR_HEADER") or "false"
).strip().lower() in {"1", "true", "yes", "on"}

SUPER_ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL")
SUPER_ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD")
if not SUPER_ADMIN_EMAIL or not SUPER_ADMIN_PASSWORD:
    raise ValueError("SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD must be set in .env")


def _warn_if_seed_password_is_weak() -> None:
    """
    Warn when the seeded super-admin password fails the new-password policy.

    The signup gate (VAPT: Weak Password Accepted) only covers passwords chosen through
    the API. This one is injected from the environment and seeded directly by
    `services/auth_platform.seed_initial_data()`, so it bypasses that gate entirely -
    which would leave the single most privileged account on the weakest secret.

    A warning, not a hard failure: raising here would refuse to start a running
    deployment on config the operator cannot change without a release, and the account
    already exists in the database by then (the seed only runs when it is absent). The
    message deliberately carries no part of the password and no length, only the rule
    that failed, so the log never narrows the search space for an attacker who reads it.
    """
    try:
        from utils.input_validation import safe_password

        safe_password(SUPER_ADMIN_PASSWORD, field="SUPER_ADMIN_PASSWORD")
    except ValueError as exc:
        logger.warning(
            "SUPER_ADMIN_PASSWORD does not satisfy the new-password policy (%s). "
            "The seeded super admin is the most privileged account in the system - "
            "rotate it to a value that meets the policy, then update the secret store.",
            str(exc).replace(str(SUPER_ADMIN_PASSWORD), "<redacted>"),
        )
    except Exception as exc:  # pragma: no cover - never block startup on this check
        logger.debug("Could not evaluate SUPER_ADMIN_PASSWORD strength: %s", type(exc).__name__)


_warn_if_seed_password_is_weak()


def _clean_env(value: str | None) -> str | None:
    """Normalize env vars by trimming whitespace and wrapping quotes."""
    if value is None:
        return None
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()
    return normalized or None


AWS_ACCESS_KEY_ID = _clean_env(os.environ.get("AWS_ACCESS_KEY_ID"))
AWS_SECRET_ACCESS_KEY = _clean_env(os.environ.get("AWS_SECRET_ACCESS_KEY"))
AWS_SESSION_TOKEN = _clean_env(os.environ.get("AWS_SESSION_TOKEN"))
AWS_REGION = _clean_env(os.environ.get("AWS_REGION"))
S3_BUCKET_NAME = _clean_env(os.environ.get("S3_BUCKET_NAME"))
# Size of the HTTPS connection pool for S3. Must be at least S3_METADATA_MAX_WORKERS
# (features/assessment/router.py) or the concurrent metadata fetches queue on a connection.
S3_MAX_POOL_CONNECTIONS = _env_int_or_default("S3_MAX_POOL_CONNECTIONS", 32, minimum=10)
# Size of the HTTPS connection pool for Amazon Polly, for the same reason as S3's above - and
# Polly is the more concurrent of the two. The tutor asks for one synthesis per paragraph and
# prefetches SPEECH_PREFETCH_AHEAD more, for every trainee in a lecture at once, so a worker can
# easily have more Polly calls in flight than botocore's default pool of 10. The calls past that
# limit do not fail; they block waiting for a free HTTPS connection, which surfaces as narration
# that stalls under load rather than as an error in a log. Optional: this default IS the shipped
# configuration, so nothing has to be added to a task definition or a secret.
POLLY_MAX_POOL_CONNECTIONS = _env_int_or_default("POLLY_MAX_POOL_CONNECTIONS", 32, minimum=10)
# Size of the HTTPS connection pool for Bedrock. sync_bedrock_client below is shared by every
# caller - the agents in services/agents.py, grading, and the direct converse()/invoke_model()
# calls - so one lecture cohort chatting at once can have far more requests in flight than
# botocore's default pool of 10. Past that limit botocore does not block or fail: urllib3 opens
# an extra connection and discards it afterwards, so the cost is a fresh TLS handshake per call
# rather than an error, which reads as chat that is uniformly slow under load.
BEDROCK_MAX_POOL_CONNECTIONS = _env_int_or_default("BEDROCK_MAX_POOL_CONNECTIONS", 32, minimum=10)
# Concurrent GETs used to build a dashboard's module-count index. Raising this shortens a
# cold index but adds load; 16 keeps a few hundred objects well inside one request.
S3_METADATA_MAX_WORKERS = _env_int_or_default("S3_METADATA_MAX_WORKERS", 16, minimum=1)
PINECONE_API_KEY = _clean_env(os.environ.get("PINECONE_API_KEY"))
PINECONE_INDEX_NAME = _clean_env(os.environ.get("PINECONE_INDEX_NAME"))

TEMP_IMAGE_DIR = Path(tempfile.gettempdir()) / "smarttutor_images"
TEMP_IMAGE_DIR.mkdir(exist_ok=True)
logger.info("Temp image directory (legacy): %s", TEMP_IMAGE_DIR)

executor = ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
job_statuses = defaultdict(
    lambda: {
        "status": "pending",
        "result": None,
        "error": None,
        "progress_message": "Job is queued...",
    }
)

BEDROCK_GENERATION_MODEL = "amazon.nova-pro-v1:0"
BEDROCK_IMAGE_MODEL = BEDROCK_GENERATION_MODEL
BEDROCK_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
EMBEDDING_MODEL_DIMENSION = 1024

DEFAULT_CHUNK_SIZE_WORDS = 150
DEFAULT_CHUNK_OVERLAP_WORDS = 50
DEFAULT_MIN_CHUNK_WORDS = 10
DEFAULT_SEGMENTATION_THRESHOLD_MULTIPLIER = 1.8
DEFAULT_MIN_SEGMENT_WORDS = 25
DEFAULT_REPORT_LLM_MAX_TOKENS = 10000
DEFAULT_REPORT_LLM_TEMPERATURE = 0.15
DEFAULT_RAG_CHUNK_SIZE_WORDS = 250
DEFAULT_RAG_CHUNK_OVERLAP_WORDS = 50
DEFAULT_RAG_TOP_K = 5
CHAT_LLM_MAX_TOKENS = 4096
CHAT_LLM_TEMPERATURE = 0.1

logger.info("Using Generation Model: %s", BEDROCK_GENERATION_MODEL)
logger.info("Using Image/Vision Model: %s", BEDROCK_IMAGE_MODEL)
logger.info(
    "Using Embedding Model: %s (Dimension: %s)",
    BEDROCK_EMBEDDING_MODEL,
    EMBEDDING_MODEL_DIMENSION,
)
logger.info("Application starting up...")

if not all([AWS_REGION, S3_BUCKET_NAME, PINECONE_API_KEY]):
    raise ConnectionError("Missing required AWS region, S3 bucket, or Pinecone environment variables.")

if AWS_ACCESS_KEY_ID and AWS_ACCESS_KEY_ID.startswith("ASIA") and not AWS_SESSION_TOKEN:
    raise ConnectionError(
        "Temporary AWS credentials detected (ASIA… access key) but AWS_SESSION_TOKEN is "
        "missing in .env. Refresh SSO/role credentials and include all three values."
    )

_session_kwargs = {"region_name": AWS_REGION}
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    _session_kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
    _session_kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
    if AWS_SESSION_TOKEN:
        _session_kwargs["aws_session_token"] = AWS_SESSION_TOKEN

bedrock_boto_session = boto3.Session(**_session_kwargs)
if bedrock_boto_session.get_credentials() is None:
    raise ConnectionError(
        "No AWS credentials found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
        "(and AWS_SESSION_TOKEN for temporary credentials), or configure the default chain."
    )

# Tokenizer models used to be downloaded here on first run. utils/text_tokenize.py needs no
# model, so start-up no longer reaches the network to be able to split text into sentences.

logger.info("Initializing AWS and Bedrock resources.")
# A generated lecture or a full question set is a long single response, so the read timeout has to
# outlast the model rather than the network. This config is the ONLY thing that bounds a Bedrock
# socket: nothing patches urllib3 globally any more, so every Bedrock caller has to be handed this
# client (services/agents.py) or set its own config (utils/aws_helper.py) to get these numbers.
bedrock_config = Config(
    max_pool_connections=BEDROCK_MAX_POOL_CONNECTIONS,
    connect_timeout=120,
    read_timeout=600,
    retries={"max_attempts": 3, "mode": "adaptive"},
)
sync_bedrock_client = bedrock_boto_session.client("bedrock-runtime", config=bedrock_config)
embedding_model = BedrockEmbeddings(
    model_id=BEDROCK_EMBEDDING_MODEL,
    client=sync_bedrock_client,
    region_name=AWS_REGION,
)
# Report metadata is fetched concurrently when a dashboard builds its module-count index
# (features/assessment/router.py), so the pool has to be wider than botocore's default of 10
# or the extra threads just queue on a connection and the request is no faster. Timeouts stay
# at botocore's defaults: the same client streams video and document uploads.
s3_config = Config(
    max_pool_connections=S3_MAX_POOL_CONNECTIONS,
    retries={"max_attempts": 3, "mode": "standard"},
)
s3_client = bedrock_boto_session.client("s3", config=s3_config)
# Socket timeouts and the retry budget, both bounded against the browser's own speech deadline
# (API_SPEECH_TIMEOUT_MS = 45000 in static/script.js). botocore's defaults are 60s connect, 60s
# read and up to 5 attempts per call, so a single stalled synthesis could hold a request thread
# for minutes AFTER the browser had given up and moved on. Bounding it here is what stops a slow
# Polly from becoming a slow application.
#
# The retry budget stays small on purpose: features/general/router.py runs its own ladder over
# the retryable Polly codes (POLLY_MAX_RETRIES_PER_PAYLOAD, two payload shapes), and a second
# larger budget underneath a ladder that cannot see it is how a 45s deadline becomes minutes.
# One retry here still absorbs a transient socket blip without the learner noticing, exactly as
# botocore's default did.
#
# Worst case is now (5 + 10) x 2 attempts x 2 payload shapes = 60s of thread time instead of the
# several minutes the defaults allowed, and a typical stall is noticed in 10s rather than 60s.
# 10s is still five times a normal chunk synthesis, so no call that would have succeeded fails.
POLLY_CONNECT_TIMEOUT_SECONDS = _env_int_or_default("POLLY_CONNECT_TIMEOUT_SECONDS", 5, minimum=1)
POLLY_READ_TIMEOUT_SECONDS = _env_int_or_default("POLLY_READ_TIMEOUT_SECONDS", 10, minimum=1)
# Server-side cache of FINISHED Polly audio (features/general/router.py). This is the setting
# that decides whether a shared lecture scales, so it is worth being precise about why.
#
# Every trainee in a lecture is served the same paragraph text - /tutor/explain-module is cached
# in TutorResponseCache, so the wording is byte-identical for all of them - and each one then
# used to pay for their own identical synthesis of it. Fifty trainees on one module meant fifty
# Polly calls per paragraph to produce fifty copies of the same mp3, spent against a generative
# engine whose request quota is far lower than the standard engines'. Caching the finished audio
# by the text that produced it collapses that to one call per worker per paragraph, which is what
# makes 50-60 concurrent trainees a non-event rather than a throttling storm.
#
# Bounded by BOTH total size and entry count: an unbounded cache on a container this size is a
# memory leak with a friendly name. At roughly 40 KB per paragraph, 32 MB per worker holds
# several concurrent lectures. Set SPEECH_AUDIO_CACHE_MAX_MB=0 to turn the cache off entirely
# and go back to synthesising every request.
SPEECH_AUDIO_CACHE_MAX_MB = _env_int_or_default("SPEECH_AUDIO_CACHE_MAX_MB", 32, minimum=0)
SPEECH_AUDIO_CACHE_MAX_ENTRIES = _env_int_or_default(
    "SPEECH_AUDIO_CACHE_MAX_ENTRIES", 512, minimum=1
)
polly_config = Config(
    max_pool_connections=POLLY_MAX_POOL_CONNECTIONS,
    connect_timeout=POLLY_CONNECT_TIMEOUT_SECONDS,
    read_timeout=POLLY_READ_TIMEOUT_SECONDS,
    retries={"max_attempts": 2, "mode": "standard"},
)
polly_client = bedrock_boto_session.client("polly", config=polly_config)
# Amazon Polly engine used for the tutor voice. The generative engine gives the most natural
# delivery but rejects SSML outright ("InvalidSsmlException: Unsupported Generative feature"),
# so features/general/router.py only sends SSML when the configured engine accepts it. Keep
# this list to the engines AWS documents full SSML support for.
POLLY_ENGINE = (_clean_env(os.environ.get("POLLY_ENGINE")) or "generative").lower()
POLLY_SSML_ENGINES = frozenset({"standard", "neural"})
transcribe_client = bedrock_boto_session.client("transcribe")

logger.info("Initializing Pinecone client...")
pc = PineconeClient(api_key=PINECONE_API_KEY)
if PINECONE_INDEX_NAME not in pc.list_indexes().names():
    logger.info("Creating Pinecone serverless index '%s' in %s...", PINECONE_INDEX_NAME, AWS_REGION)
    pc.create_index(
        name=PINECONE_INDEX_NAME,
        dimension=EMBEDDING_MODEL_DIMENSION,
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region=AWS_REGION),
    )
pinecone_index = pc.Index(PINECONE_INDEX_NAME)

# --- Database connection pool -------------------------------------------------
# Every sync (`def`) endpoint runs in AnyIO's worker-thread pool and holds its get_db()
# connection for the whole request, including any slow S3 / Bedrock work still done inside
# that request. Two deployment shapes need different engines:
#
# 1) RDS Proxy (UAT behind proxy-*.rds.amazonaws.com):
#    Use NullPool. A large QueuePool kept (pool_size+overflow) idle client sockets open to
#    the proxy per uvicorn worker — (20+10) x 3 workers ≈ 90 sockets sitting open even when
#    traffic was light, which is what pushed the t3.medium connection count toward ~104.
#    That idle pile-up is a capacity / CloudWatch problem; it is NOT what raises
#    "QueuePool limit … connection timed out". That error means every slot in ONE worker's
#    pool was checked out at once, which was streaming endpoints holding a session across
#    Bedrock while ordinary requests waited — fixed by session_scope() / non-holding auth,
#    not by NullPool alone. NullPool still matters: it stops the app opening dozens of idle
#    proxy clients, and with no QueuePool the checkout-timeout error becomes impossible.
#    RDS Proxy multiplexes; the app opens a connection for the request and closes it when
#    the session closes.
#
# 2) Direct Postgres (dev / local, no proxy):
#    Keep a QueuePool so each worker is not opening a fresh TCP handshake on every request.
#    Safety-net defaults stay at the previously deployed 20+10 so a missing secret on DEV
#    does not silently shrink capacity. Env overrides still win when present.
#
# Deliberately NOT applied on either path: REQUEST_THREAD_LIMIT. Capping AnyIO's thread pool
# to the DB pool size serialized Polly (/speech/synthesize holds no session but IS a sync
# endpoint) behind other sync work and starved tutor audio. Connection lifetime (release
# before Bedrock / streams) is the real control for pool exhaustion; removing the thread
# cap is the real control for speech starvation. See session_scope() and
# authenticated_user_id_without_holding_db().
_DATABASE_URL = _build_database_connection_url()


def _env_flag(name: str) -> Optional[bool]:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def _host_looks_like_rds_proxy(host: str) -> bool:
    """AWS RDS Proxy endpoints contain '.proxy-' in the hostname."""
    return ".proxy-" in (host or "").lower()


def _detect_rds_proxy_usage(database_url: str) -> bool:
    explicit = _env_flag("USE_RDS_PROXY")
    if explicit is not None:
        return explicit
    try:
        # postgresql://user:pass@host:port/db
        host_port = database_url.split("@", 1)[1].split("/", 1)[0]
        host = host_port.rsplit(":", 1)[0]
    except Exception:
        host = ""
    return _host_looks_like_rds_proxy(host)


USE_RDS_PROXY = _detect_rds_proxy_usage(_DATABASE_URL)

# Direct-Postgres QueuePool knobs (ignored when USE_RDS_PROXY — NullPool has no size).
# Safety nets match the previously shipped DEV/UAT QueuePool numbers so one codebase works
# for both: UAT selects NullPool via hostname / USE_RDS_PROXY and ignores these; DEV keeps
# QueuePool and must not shrink to 5+5 when a secret key is briefly missing.
DB_POOL_SIZE = _pool_setting("DB_POOL_SIZE", 20, minimum=5)
DB_MAX_OVERFLOW = _pool_setting("DB_MAX_OVERFLOW", 10, minimum=0)
DB_POOL_TIMEOUT = _pool_setting("DB_POOL_TIMEOUT", 30, minimum=5)
DB_POOL_RECYCLE = _pool_setting("DB_POOL_RECYCLE", 1800, minimum=60)
# Retired, and kept as a name rather than deleted: this module is star-imported by the feature
# routers, so the name disappearing would be an ImportError in anything that still referred to it,
# and None makes an accidental arithmetic use fail loudly instead of silently capping something.
# Nothing reads it now - AnyIO's thread pool runs at its own default. What bounds the connection
# pool is how long a session is held (session_scope,
# authenticated_user_id_without_holding_db), not how many threads exist.
REQUEST_THREAD_LIMIT = None
UVICORN_WORKER_COUNT = 3
DB_CONNECTION_SAFETY_MARGIN = 10

# How long to wait for the TCP/TLS connection and authentication handshake before giving up.
#
# This matters far more under NullPool than it did under a QueuePool. Every request now opens its
# own connection, so a connection that hangs is a request that hangs, and there is no
# pool_timeout left to bound it - that setting only ever applied to waiting for a QueuePool slot.
# With nothing set, libpq falls back to the operating system's TCP behaviour, which outlives the
# ALB's 60s idle timeout and reaches the trainee as a bare 504 with nothing in the logs to say
# why. Failing in a bounded time instead lets the request return a real error.
#
# Sizing belongs to DevOps like the pool values; this is only the safety net.
DB_CONNECT_TIMEOUT = _pool_setting("DB_CONNECT_TIMEOUT", 15, minimum=5)

if USE_RDS_PROXY:
    # NullPool: one connection per Session, closed on session.close(). RDS Proxy owns the
    # real multiplexing to Postgres; the app must not keep a private queue of idle clients.
    #
    # pool_pre_ping is left on deliberately but does nothing here: SQLAlchemy only pings a
    # connection it is reusing, and NullPool never reuses one. It stays so that flipping
    # USE_RDS_PROXY off does not quietly lose the check.
    engine = create_engine(
        _DATABASE_URL,
        poolclass=NullPool,
        pool_pre_ping=True,
        connect_args={"connect_timeout": DB_CONNECT_TIMEOUT},
    )
    logger.info(
        "Database engine: NullPool (RDS Proxy). Per-request connections; no app-side QueuePool."
    )
else:
    engine = create_engine(
        _DATABASE_URL,
        pool_pre_ping=True,
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
        pool_timeout=DB_POOL_TIMEOUT,
        pool_recycle=DB_POOL_RECYCLE,
        pool_use_lifo=True,
        connect_args={"connect_timeout": DB_CONNECT_TIMEOUT},
    )
    if _pool_settings_missing_from_config:
        logger.warning(
            "Database pool configuration was not supplied for: %s. Using built-in safety-net "
            "values for direct Postgres. Behind RDS Proxy set USE_RDS_PROXY=true (or use a "
            "proxy-* DB_HOST) so NullPool is selected instead.",
            ", ".join(_pool_settings_missing_from_config),
        )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")


@event.listens_for(engine, "connect", insert=True)
def set_search_path(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("SET search_path TO public")
    cursor.close()


@app.on_event("startup")
def _report_db_connection_budget() -> None:
    """
    Check this worker's connection budget against the database server's real limit, at boot.

    With RDS Proxy + NullPool there is no fixed per-worker ceiling to multiply — concurrent
    requests equal concurrent client connections to the proxy. The check still reports
    max_connections so operators can see headroom on the instance behind the proxy.

    Purely informational: never changes a setting and never fails startup.
    """
    if USE_RDS_PROXY:
        try:
            from sqlalchemy import text

            with engine.connect() as conn:
                max_connections = int(conn.execute(text("SHOW max_connections")).scalar())
            logger.info(
                "RDS Proxy mode (NullPool): instance max_connections=%s. Concurrent app "
                "sessions map 1:1 to proxy client connections; keep request work short and "
                "release sessions before Polly/Bedrock/streams.",
                max_connections,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "Could not read max_connections behind RDS Proxy (%s).",
                type(exc).__name__,
            )
        return

    per_worker = DB_POOL_SIZE + DB_MAX_OVERFLOW
    budget = per_worker * UVICORN_WORKER_COUNT
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            max_connections = int(conn.execute(text("SHOW max_connections")).scalar())
            superuser_reserved = int(
                conn.execute(text("SHOW superuser_reserved_connections")).scalar()
            )
    except Exception as exc:  # pragma: no cover - diagnostics only, never fatal
        logger.warning(
            "Could not read the database connection limit (%s), so the pool budget could not be "
            "verified against this instance. Check it before scaling the worker count up.",
            type(exc).__name__,
        )
        return

    usable = max_connections - superuser_reserved - DB_CONNECTION_SAFETY_MARGIN
    if budget > usable:
        advised_total = max(5, usable // UVICORN_WORKER_COUNT)
        advised_overflow = min(DB_MAX_OVERFLOW, max(0, advised_total - 5))
        logger.error(
            "Database pool budget %s exceeds what this instance allows (%s usable of "
            "max_connections=%s). Set DB_POOL_SIZE=%s and DB_MAX_OVERFLOW=%s, or move to a "
            "larger instance class - otherwise a burst will be refused by Postgres.",
            budget,
            usable,
            max_connections,
            advised_total - advised_overflow,
            advised_overflow,
        )
    else:
        logger.debug(
            "Database pool budget fits this instance with %s connections spare.",
            usable - budget,
        )


# Safety nets, not tuning. How long a starting worker waits for another process that is already
# applying schema changes before it starts without them, and how long a request that finds the
# assessment tables unverified waits before carrying on.
_SCHEMA_LOCK_STARTUP_WAIT_SECONDS = 180
_SCHEMA_LOCK_REQUEST_WAIT_SECONDS = 5

# Set once this process has brought the schema up to date. Both startup handlers registered in
# services/auth_platform.py call create_db_and_tables(), so without this every worker ran the
# whole sequence twice.
_SCHEMA_APPLIED = threading.Event()
_ASSESSMENT_TABLES_VERIFIED = threading.Event()


def create_db_and_tables():
    """
    Bring the database schema up to date, one process at a time.

    Every container runs several workers, and a rolling deploy starts containers side by side, so
    several processes arrive here together. Unserialised, they ran the same CREATE TABLE and ALTER
    TABLE statements concurrently and the losers failed part-way, skipping every later step. The
    advisory lock makes them take turns: the first applies the changes, the rest find nothing left.

    The legacy idempotent steps run first, unchanged, then Alembic applies any versioned revision.
    Set RUN_SCHEMA_MIGRATIONS_ON_STARTUP=false once migrations run as a deploy step of their own.

    Once the database is at the newest revision, startup only checks that - no lock, no DDL - so a
    routine deploy never competes with live traffic for a table lock. If the database is reachable
    but cannot be brought up to date, startup fails instead of running new code against an old
    schema (which would break every query on the changed table); ECS then keeps the previous
    version serving. An unreachable database is logged and the app starts, as before.
    """
    if _SCHEMA_APPLIED.is_set():
        return
    if _env_flag("RUN_SCHEMA_MIGRATIONS_ON_STARTUP") is False:
        # The deploy step ran `python -m core.schema_migrations upgrade`, which applies this same
        # full sequence (legacy steps, then Alembic). Mark both events so a later assessment-config
        # save does not fall back to issuing CREATE INDEX against live tables.
        logger.info("Schema migrations on startup are disabled; expecting them to run as a deploy step.")
        _SCHEMA_APPLIED.set()
        _ASSESSMENT_TABLES_VERIFIED.set()
        return

    from core.schema_lock import schema_migration_lock
    from core.schema_migrations import schema_is_current

    try:
        if schema_is_current(engine):
            logger.info("Database schema is up to date; no migration needed.")
            _mark_schema_applied()
            return
    except Exception as e:
        logger.error("Error creating database tables: %s", e, exc_info=True)
        return

    try:
        with schema_migration_lock(engine, _SCHEMA_LOCK_STARTUP_WAIT_SECONDS) as acquired:
            if not acquired:
                logger.warning(
                    "Another process is still applying schema changes; checking whether it finished."
                )
            else:
                legacy_ok = apply_legacy_schema_steps()
                # Run even if a legacy step failed, as before; but only a fully clean pass is
                # remembered, so the second startup handler still retries anything that did not apply.
                if _apply_versioned_migrations() and legacy_ok:
                    _SCHEMA_APPLIED.set()
    except Exception as e:
        logger.error("Error creating database tables: %s", e, exc_info=True)
    _require_current_schema()


class SchemaNotCurrentError(RuntimeError):
    """The database is reachable but still behind the revisions this code needs."""


def _mark_schema_applied() -> None:
    _SCHEMA_APPLIED.set()
    _ASSESSMENT_TABLES_VERIFIED.set()


def _require_current_schema() -> None:
    """Refuse to start this code on an older schema; see create_db_and_tables."""
    from core.schema_migrations import head_revision, schema_is_current

    try:
        current = schema_is_current(engine)
    except Exception as e:
        logger.error("Could not confirm the database schema revision: %s", e)
        return
    if current:
        _mark_schema_applied()
        return
    raise SchemaNotCurrentError(
        f"The database is not at schema revision {head_revision()}, and this version of the app "
        "needs it. Refusing to start; the previous version keeps serving until a restart can "
        "apply the migration."
    )


def apply_legacy_schema_steps() -> bool:
    """
    The schema steps that predate versioned migrations: create_all, the assessment tables, and
    add_missing_columns. Idempotent and frozen.

    Startup and `python -m core.schema_migrations upgrade` both run these before Alembic. That is
    what lets a deploy step replace startup migrations (RUN_SCHEMA_MIGRATIONS_ON_STARTUP=false)
    without a new environment losing its tables, or an existing one a pending legacy column.
    Caller holds the schema lock. Returns True only if every step succeeded.
    """
    # The models register their tables on Base.metadata when imported. The app's routers have
    # always done that before startup, but the migration command imports only this module, and
    # create_all against empty metadata "succeeds" while creating nothing.
    importlib.import_module("models.domain")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created successfully.")
    assessment_ok = _ensure_assessment_tables_unlocked()
    if assessment_ok:
        _ASSESSMENT_TABLES_VERIFIED.set()
    columns_ok = add_missing_columns()
    return assessment_ok and columns_ok


def _apply_versioned_migrations() -> bool:
    """Run pending Alembic revisions; a failure is logged and leaves the legacy schema in place."""
    try:
        from core.schema_migrations import upgrade_to_head

        upgrade_to_head(engine)
        return True
    except Exception as e:
        logger.error("Versioned schema migration failed: %s", e, exc_info=True)
        return False


def add_missing_columns() -> bool:
    """
    Add missing columns to existing tables for backward compatibility.

    Frozen: these are the schema changes made before versioned migrations existed. Every new
    change belongs in a revision under core/migrations/versions, not here. Call only while
    holding the schema lock (create_db_and_tables does). Returns False if a step failed.
    """
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'assessment_questions'
                    AND column_name = 'source_doc_name'
                );
                """
                )
            )
            column_exists = result.scalar()
            if not column_exists:
                logger.info("Adding source_doc_name column to assessment_questions table...")
                conn.execute(
                    text(
                        """
                    ALTER TABLE assessment_questions
                    ADD COLUMN source_doc_name VARCHAR(255) NULL;
                    """
                    )
                )
                conn.commit()
                logger.info("Successfully added source_doc_name column")
            else:
                logger.info("source_doc_name column already exists")

            rejected_col_exists = conn.execute(
                text(
                    """
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'assessment_questions'
                    AND column_name = 'is_rejected'
                );
                """
                )
            ).scalar()
            if not rejected_col_exists:
                logger.info("Adding is_rejected column to assessment_questions table...")
                conn.execute(
                    text(
                        """
                    ALTER TABLE assessment_questions
                    ADD COLUMN is_rejected BOOLEAN NOT NULL DEFAULT FALSE;
                    """
                    )
                )
                conn.commit()
                logger.info("Successfully added is_rejected column")
            else:
                logger.info("is_rejected column already exists")

            for col_name, col_type in (
                ("docx_s3_key", "VARCHAR"),
                ("export_format", "VARCHAR(16)"),
            ):
                col_exists = conn.execute(
                    text(
                        """
                    SELECT EXISTS (
                        SELECT FROM information_schema.columns
                        WHERE table_name = 'video_analysis_history'
                        AND column_name = :col
                    );
                    """
                    ),
                    {"col": col_name},
                ).scalar()
                if not col_exists:
                    logger.info(
                        "Adding %s column to video_analysis_history table...", col_name
                    )
                    conn.execute(
                        text(
                            f"ALTER TABLE video_analysis_history ADD COLUMN {col_name} {col_type} NULL;"
                        )
                    )
                    conn.commit()
                    logger.info("Successfully added %s column", col_name)

            video_embedding_cols = (
                ("source_doc_name", "VARCHAR"),
                ("embedding_status", "VARCHAR(32)"),
                ("embedded_at", "TIMESTAMP"),
                ("chunk_count", "INTEGER"),
                ("video_s3_key", "VARCHAR"),
            )
            for col_name, col_type in video_embedding_cols:
                col_exists = conn.execute(
                    text(
                        """
                    SELECT EXISTS (
                        SELECT FROM information_schema.columns
                        WHERE table_name = 'video_analysis_history'
                        AND column_name = :col
                    );
                    """
                    ),
                    {"col": col_name},
                ).scalar()
                if not col_exists:
                    logger.info(
                        "Adding %s column to video_analysis_history table...", col_name
                    )
                    conn.execute(
                        text(
                            f"ALTER TABLE video_analysis_history ADD COLUMN {col_name} {col_type} NULL;"
                        )
                    )
                    conn.commit()
                    logger.info("Successfully added %s column", col_name)

            chat_session_cols = (
                ("chat_type", "VARCHAR(32)"),
                ("kb_namespace_user_id", "VARCHAR"),
                ("video_history_ids", "TEXT"),
            )
            for col_name, col_type in chat_session_cols:
                col_exists = conn.execute(
                    text(
                        """
                    SELECT EXISTS (
                        SELECT FROM information_schema.columns
                        WHERE table_name = 'chat_sessions'
                        AND column_name = :col
                    );
                    """
                    ),
                    {"col": col_name},
                ).scalar()
                if not col_exists:
                    logger.info("Adding %s column to chat_sessions table...", col_name)
                    conn.execute(
                        text(
                            f"ALTER TABLE chat_sessions ADD COLUMN {col_name} {col_type} NULL;"
                        )
                    )
                    conn.commit()
                    logger.info("Successfully added %s column", col_name)

            # Original upload filenames for display. Storage names are randomized
            # ("<uuid>.<ext>", VAPT #4), so without this the UI can only show the UUID.
            display_names_exists = conn.execute(
                text(
                    """
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'courses'
                    AND column_name = 'source_document_display_names'
                );
                """
                )
            ).scalar()
            if not display_names_exists:
                logger.info(
                    "Adding source_document_display_names column to courses table..."
                )
                conn.execute(
                    text(
                        """
                    ALTER TABLE courses
                    ADD COLUMN source_document_display_names TEXT NULL;
                    """
                    )
                )
                conn.commit()
                logger.info("Successfully added source_document_display_names column")

            # Course-scoped notifications: deleting a course must also delete the
            # notifications about it, and text matching alone cannot find rows whose course
            # was renamed. SET NULL, so a notification never blocks a course delete.
            notif_course_exists = conn.execute(
                text(
                    """
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'notifications'
                    AND column_name = 'course_id'
                );
                """
                )
            ).scalar()
            if not notif_course_exists:
                logger.info("Adding course_id column to notifications table...")
                conn.execute(
                    text(
                        """
                    ALTER TABLE notifications
                    ADD COLUMN course_id VARCHAR NULL
                    REFERENCES courses(id) ON DELETE SET NULL;
                    """
                    )
                )
                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS ix_notifications_course_id
                    ON notifications (course_id);
                    """
                    )
                )
                conn.commit()
                logger.info("Successfully added course_id column to notifications")

            # How many attempts an approved request granted. Attempt caps are computed by counting
            # the attempts stored, so a grant that lives only in TraineeAttempt.attempts_left cannot
            # be counted - and that column is overwritten by the admin reset/sync endpoints. NULL
            # for rows approved before this column existed, which reads as one attempt.
            granted_attempts_exists = conn.execute(
                text(
                    """
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'additional_attempt_requests'
                    AND column_name = 'granted_attempts'
                );
                """
                )
            ).scalar()
            if not granted_attempts_exists:
                logger.info("Adding granted_attempts column to additional_attempt_requests table...")
                conn.execute(
                    text(
                        """
                    ALTER TABLE additional_attempt_requests
                    ADD COLUMN granted_attempts INTEGER NULL;
                    """
                    )
                )
                conn.commit()
                logger.info("Successfully added granted_attempts column")

            # Session refresh race fix: keep previous JWT so in-flight requests survive rotation
            prev_token_exists = conn.execute(
                text(
                    """
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'user_sessions'
                    AND column_name = 'previous_session_token'
                );
                """
                )
            ).scalar()
            if not prev_token_exists:
                logger.info("Adding previous_session_token column to user_sessions table...")
                conn.execute(
                    text(
                        """
                    ALTER TABLE user_sessions
                    ADD COLUMN previous_session_token VARCHAR NULL;
                    """
                    )
                )
                conn.commit()
                logger.info("Successfully added previous_session_token column")
        return True
    except Exception as e:
        logger.warning("Could not add missing columns: %s", e)
        return False


def ensure_assessment_tables_exist():
    """
    Ensure the assessment tables exist; a no-op once this process has verified them.

    The assessment routes call this on every config save. Startup already verifies the tables
    under the schema lock, and re-running the check from a request issued CREATE INDEX against
    live tables, so after one success it returns at once. If startup could not verify them, a
    request still tries - under the same lock, so it cannot race a starting container.
    """
    if _ASSESSMENT_TABLES_VERIFIED.is_set():
        return

    from core.schema_lock import schema_migration_lock

    try:
        with schema_migration_lock(engine, _SCHEMA_LOCK_REQUEST_WAIT_SECONDS) as acquired:
            if acquired and _ensure_assessment_tables_unlocked():
                _ASSESSMENT_TABLES_VERIFIED.set()
    except Exception as e:
        logger.error("Error ensuring assessment tables exist: %s", e, exc_info=True)


def _ensure_assessment_tables_unlocked() -> bool:
    """Create any missing assessment table and index. Caller holds the schema lock."""
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            checks = {
                "assessment_configs": """
                    CREATE TABLE assessment_configs (
                        id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::text,
                        course_id VARCHAR NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                        assessment_scope VARCHAR NOT NULL,
                        question_type VARCHAR NOT NULL,
                        total_questions INTEGER NOT NULL,
                        mcq_count INTEGER DEFAULT 0,
                        subjective_count INTEGER DEFAULT 0,
                        weightage_per_question INTEGER NOT NULL,
                        pass_percentage INTEGER NOT NULL,
                        max_attempts INTEGER NOT NULL,
                        ai_suggested_time INTEGER NOT NULL,
                        trainer_time_override INTEGER,
                        is_approved BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                """,
                "assessment_questions": """
                    CREATE TABLE assessment_questions (
                        id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::text,
                        course_id VARCHAR NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                        module_index INTEGER,
                        question_type VARCHAR NOT NULL,
                        question_text TEXT NOT NULL,
                        correct_answer TEXT,
                        option_a VARCHAR,
                        option_b VARCHAR,
                        option_c VARCHAR,
                        option_d VARCHAR,
                        explanation TEXT,
                        is_approved BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                """,
                "trainee_assessments": """
                    CREATE TABLE trainee_assessments (
                        id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::text,
                        trainee_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        course_id VARCHAR NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                        module_index INTEGER,
                        attempt_number INTEGER NOT NULL,
                        questions_data TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        total_possible INTEGER NOT NULL,
                        percentage INTEGER NOT NULL,
                        passed BOOLEAN NOT NULL,
                        time_taken INTEGER NOT NULL,
                        started_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        completed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        is_completed BOOLEAN DEFAULT FALSE,
                        partial_answers TEXT
                    );
                """,
                "additional_attempt_requests": """
                    CREATE TABLE additional_attempt_requests (
                        id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::text,
                        trainee_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        course_id VARCHAR NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                        module_index INTEGER,
                        reason TEXT NOT NULL,
                        status VARCHAR DEFAULT 'pending',
                        requested_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        reviewed_at TIMESTAMP WITH TIME ZONE,
                        reviewed_by VARCHAR REFERENCES users(id),
                        trainer_notes TEXT
                    );
                """,
                "trainee_question_sets": """
                    CREATE TABLE trainee_question_sets (
                        id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::text,
                        trainee_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        course_id VARCHAR NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                        module_index INTEGER,
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        questions_json TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                """,
            }

            for table_name, ddl in checks.items():
                exists = conn.execute(
                    text(
                        f"""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_schema = 'public'
                        AND table_name = '{table_name}'
                    );
                    """
                    )
                ).scalar()
                if not exists:
                    logger.info("Creating %s table...", table_name)
                    conn.execute(text(ddl))

            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_assessment_configs_course_id
                ON assessment_configs(course_id);
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_assessment_questions_course_id
                ON assessment_questions(course_id);
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_assessment_questions_module_index
                ON assessment_questions(course_id, module_index);
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_trainee_assessments_trainee_course
                ON trainee_assessments(trainee_id, course_id);
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_trainee_assessments_module
                ON trainee_assessments(course_id, module_index);
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_additional_attempt_requests_trainee
                ON additional_attempt_requests(trainee_id);
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_additional_attempt_requests_status
                ON additional_attempt_requests(status);
                """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_tqs_trainee_course_module
                ON trainee_question_sets(trainee_id, course_id, module_index, attempt_number);
                """
                )
            )
            conn.commit()
            logger.info("Assessment tables verification completed")
        return True
    except Exception as e:
        logger.error("Error ensuring assessment tables exist: %s", e, exc_info=True)
        return False


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def session_scope():
    """
    A pooled session that is returned as soon as the ``with`` block ends.

    ``Depends(get_db)`` is the right tool for an ordinary request, but FastAPI finalizes a
    yield-dependency only after the response has been sent. For a StreamingResponse that is the
    whole length of the stream, so an endpoint that merely reads a chat session before handing
    back a generator keeps a connection checked out for the entire model answer - seconds to
    minutes - while doing no database work at all.

    That is what exhausted the pool in UAT. The streaming chat endpoints are `async def`, so they
    do not occupy worker threads the way sync endpoints do, and nothing else bounded how many
    ran at once: N concurrent chat users meant N connections held for N model answers. Widening
    a QueuePool does not fix that; releasing the connection before the slow work does. Behind
    RDS Proxy the engine is NullPool for the same reason — idle app-side pools only inflate
    proxy client connection counts.

    Use this for the short bursts of database work around slow I/O. Rolls back on the way out of
    an exception so a failure cannot leave a half-written transaction on a pooled connection.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def describe_db_pool() -> str:
    """
    One-line census of the connection engine, for the log line that reports saturation.

    With NullPool (RDS Proxy) there is no QueuePool to size — concurrent Sessions are
    concurrent client connections to the proxy. With QueuePool, ``checked_out`` at the
    ceiling with ``overflow`` maxed means the pool is genuinely too small for the traffic.
    """
    try:
        if USE_RDS_PROXY:
            return "engine=NullPool (RDS Proxy); no app-side QueuePool"
        pool = engine.pool
        return (
            f"pool_size={DB_POOL_SIZE} max_overflow={DB_MAX_OVERFLOW} "
            f"checked_out={pool.checkedout()} overflow={pool.overflow()} "
            f"idle={pool.checkedin()}"
        )
    except Exception:  # pragma: no cover - diagnostics must never mask the original error
        return "pool status unavailable"


__all__ = [name for name in globals() if not name.startswith("__")]



# --- Frontend Assets ---
_chartjs_path = os.path.join("static", "chart.umd.min.js")
if not os.path.exists(_chartjs_path):
    logger.warning(f"Chart.js not found at {_chartjs_path}. Please ensure chart.umd.min.js is present in the static folder.")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

__all__ = [name for name in globals() if not name.startswith('_')]
