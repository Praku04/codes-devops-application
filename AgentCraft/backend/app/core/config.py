"""Application settings — AWS Bedrock via LiteLLM, SQLite, CORS."""

import sys
from functools import lru_cache
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The repo-root .env — the single source of truth for configuration.
#:
#: Pinned to an absolute path on purpose. A bare `env_file=".env"` resolves against the
#: *working directory*, so running from backend/ (which is uvicorn's cwd) read a
#: backend/.env instead, and the same settings could differ by code path: main.py loads
#: the root file explicitly, while anything constructing Settings() directly picked up
#: whatever .env sat in its cwd. A duplicate backend/.env pinned CORS to a stale port
#: that way. Now the file is the same no matter where the process starts.
ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


def _ensure_utf8_env(path: Path) -> str:
    """Re-encode the .env as UTF-8 if it is not, and return the encoding to read it with.

    A .env is plain text that people edit in whatever is to hand, and an editor that saves it as
    cp1252 turns `--` into a single 0x97 byte -- which UTF-8 cannot decode. Every reader of this
    file decodes strictly, so one such byte *in a comment* stopped the API at import with a
    `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x97 in position 34` from inside
    dotenv/parser.py: a traceback that names a codec and a byte offset, and never the file.

    Repairing rather than merely tolerating, because tolerating cannot work. `litellm/__init__.py`
    calls `load_dotenv()` itself with no `encoding=` to pass, so however carefully this app reads
    the file, importing the LLM client re-reads it strictly and dies. Fixing the bytes once fixes
    it for every reader, including the ones in site-packages.

    Lossless for configuration: cp1252 (then latin-1, which cannot fail on any byte) decodes
    every ASCII key and value exactly, so only the comment punctuation changes. The original is
    kept alongside as `.env.<encoding>-backup` and the swap is announced -- silently rewriting
    someone's config is worse than the crash.

    If the rewrite is not possible (read-only file, no permission), the fallback encoding is
    returned anyway: this app then still starts, and the failure moves back to whichever
    dependency reads the file next, with a message on stderr explaining what to run.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return "utf-8"  # missing or unreadable: Settings falls back to its defaults, as before

    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as exc:
        offender = f"byte {exc.object[exc.start]:#04x} at position {exc.start}"

    for encoding in ("cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        print(f"[env] {path} is not UTF-8 ({offender}); read as {encoding}.", file=sys.stderr)
        backup = path.with_suffix(f"{path.suffix}.{encoding}-backup")
        try:
            if not backup.exists():
                backup.write_bytes(raw)
            path.write_text(text, encoding="utf-8", newline="\n")
        except OSError as err:
            print(
                f"[env] could not rewrite it as UTF-8 ({err}). Dependencies that read .env "
                f"themselves will still fail -- fix it with:\n"
                f"[env]   iconv -f {encoding} -t utf-8 '{path}' -o /tmp/env.utf8 "
                f"&& mv /tmp/env.utf8 '{path}'",
                file=sys.stderr,
            )
            return encoding
        print(f"[env] rewrote it as UTF-8; original kept at {backup.name}", file=sys.stderr)
        return "utf-8"

    return "latin-1"


#: Runs once at import, before anything reads the file -- main.py imports this module first, so
#: the repair happens ahead of its load_dotenv and ahead of litellm's.
ENV_FILE_ENCODING = _ensure_utf8_env(ROOT_ENV)


def _origin_of(url: str) -> str:
    """`scheme://host[:port]` from a configured URL, or "" if there is nothing usable.

    Deliberately forgiving about what lands in a .env: a trailing slash, a path, or a bare
    hostname with no scheme are all things people write, and none of them should turn into a
    silent CORS rejection. A bare host is assumed https — the only shape it takes in practice is
    a proxied domain.
    """
    raw = url.strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw if "//" in raw else f"https://{raw}")
    if not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV,
        env_file_encoding=ENV_FILE_ENCODING,
        extra="ignore",
    )

    # AWS / Bedrock (consumed by boto3 / LiteLLM)
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    aws_region_name: str = "us-east-1"

    # LLM — the two ids are shaped differently and neither is a typo.
    #
    # Sonnet 4.6 is served through cross-region inference rather than on-demand throughput, so it
    # needs a routing prefix: `us.` pins the region (10% premium), `global.` routes dynamically.
    # The bare base id is rejected with a 400 telling you to pass an inference profile. Its base
    # id is also dateless with no `-v1:0` suffix — `anthropic.claude-sonnet-4-6` is the whole id.
    #
    # Sonnet 5 is the reverse: it has no ARN-versioned id and no inference profile, so the bare
    # `anthropic.claude-sonnet-5` is correct and adding a prefix is what breaks it.
    #
    # A different model for the fallback, not a second profile of the primary — the reason to
    # fall back is that the first choice was unavailable or throttled, and a sibling profile
    # shares much of what made it unavailable. Sonnet 5 is a full-strength answer rather than a
    # downgrade (newer than 4.6, cheaper, 1M context), so a fallback generation is not a
    # second-rate one. What it does not accept is a `temperature` — see `_NO_SAMPLING_MODELS` in
    # services/llm/client.py, which drops the value below for this model and keeps it for 4.6.
    llm_model: str = "bedrock/us.anthropic.claude-sonnet-4-6"
    llm_fallback_model: str = "bedrock/anthropic.claude-sonnet-5"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 8192
    llm_timeout_seconds: int = 300

    # API / ports — see .env.example. API_PORT and FRONTEND_PORT are the single source
    # of truth: CORS below and the Angular/CLI API base URL are all derived from them.
    api_host: str = "0.0.0.0"
    api_port: int = 8555
    frontend_port: int = 4225
    # Hostname/IP the browser reaches this deployment on — an EC2 public IP or DNS name.
    # Empty means a local run, where loopback is correct. Set it and the CORS allowlist
    # below picks up the public origin, which is otherwise the first thing to break on a
    # remote box: the UI loads, then every API call fails a preflight.
    public_host: str = ""
    # The deployed URLs, as an infrastructure team hands them over: "the UI is served here, the
    # API is served there". `frontend_url` is the one that matters to this process — its origin
    # goes into the CORS allowlist, which is the only thing the API needs to know about a domain
    # in front of it. `backend_url` is read so the pair can be configured together and so
    # `api_base_url` can report the address people actually use; nothing routes on it.
    frontend_url: str = ""
    backend_url: str = ""
    # Extra CORS origins only. Left empty, the dev UI on FRONTEND_PORT is still allowed —
    # `cors_origin_list` always includes it, so changing FRONTEND_PORT cannot silently
    # break the browser with a CORS error.
    api_cors_origins: str = ""
    # True disables the any-host regex below, leaving only the explicit allowlist. Off by
    # default so a dev run and a demo box both work without configuration; turn it on for
    # anything internet-facing that matters.
    cors_strict: bool = False
    database_url: str = "sqlite:///./agentcraft.db"
    upload_max_mb: int = 20
    upload_dir: str = "uploads"
    log_level: str = "INFO"

    # Auth
    jwt_secret: str = "agentcraft-dev-secret-change-me"
    jwt_expire_hours: int = 72

    # Super admin (seeded on startup)
    super_admin_email: str = "admin@ac.com"
    super_admin_password: str = "1681149@sPk"
    super_admin_name: str = "Super Admin"

    @property
    def cors_origin_list(self) -> List[str]:
        """Origins the browser may call the API from.

        The dev UI on `frontend_port` is always included — on both hostnames, since
        localhost and 127.0.0.1 are distinct origins to a browser. That means moving
        FRONTEND_PORT needs no second edit; forgetting to update an allowlist used to
        surface as an opaque CORS failure in the console rather than an obvious error.
        `api_cors_origins` adds to this list (e.g. a deployed UI), it does not replace it.

        `public_host`, when set, adds the deployed origin on the same port — on http and
        https, since a box behind a TLS proxy serves the UI on https while the setting
        holds only a hostname. Loopback stays allowed so an SSH tunnel to the same server
        keeps working.
        """
        origins = [
            f"http://localhost:{self.frontend_port}",
            f"http://127.0.0.1:{self.frontend_port}",
        ]
        host = self.public_host.strip()
        if host:
            for candidate in (
                f"http://{host}:{self.frontend_port}",
                f"https://{host}:{self.frontend_port}",
                f"http://{host}",
                f"https://{host}",
            ):
                if candidate not in origins:
                    origins.append(candidate)
        # FRONTEND_URL is a full URL; CORS compares *origins*, so trim any path and trailing
        # slash. `https://ui.example.com/` and `https://ui.example.com/app` both send
        # `Origin: https://ui.example.com`, and an allowlist entry with the slash still on it
        # never matches — a preflight failure with nothing in the logs to explain it.
        for url in (self.frontend_url, self.api_cors_origins):
            for extra in url.split(","):
                origin = _origin_of(extra)
                if origin and origin not in origins:
                    origins.append(origin)
        return origins

    @property
    def cors_origin_regex(self) -> str | None:
        """Pattern allowing the UI on `frontend_port` at *any* hostname, or None.

        This is what lets one build run unchanged on localhost and on a server. The Angular
        app derives its API host from `window.location` (see frontend/src/app/api-base.ts),
        so it always calls the right backend — but the backend still has to accept the
        origin, and it cannot know in advance whether that is `localhost`, a LAN address or
        an EC2 public IP.

        The port is optional, which covers the other common shape: a UI behind a TLS reverse
        proxy is served on 443, so its Origin header carries no port at all
        (`https://agentcraft-poc.example.com`). Requiring `:{frontend_port}` used to reject
        exactly the deployments that need this most — the UI loaded, and every call failed a
        preflight with no clue that a port was the reason.

        Scope: http/https, a hostname, and either the dev-server port or the scheme's default.
        It is still broader than an allowlist — a page served from any host passes — so set
        `cors_strict` (API_CORS_STRICT=true) for a real deployment and name the origins in
        `public_host` / `api_cors_origins` instead. Tokens live in localStorage rather than
        cookies, so a matching third-party origin cannot ride along on a session, but that is
        a mitigation, not a reason to leave it open in production.
        """
        if self.cors_strict:
            return None
        # Hostname characters only (letters, digits, dot, hyphen) — no path or userinfo, so
        # this cannot be widened by a crafted Origin header. Starlette full-matches this.
        return rf"https?://[A-Za-z0-9.\-]+(:{self.frontend_port})?"

    @property
    def api_base_url(self) -> str:
        """URL the UI and CLI use to reach this API.

        `backend_url` first — behind a proxy it is the only address that is actually correct,
        scheme and all — then `public_host`, then loopback. `api_host` is deliberately not used:
        it is a *bind* address, and the usual 0.0.0.0 is not a valid address to connect to.
        """
        if self.backend_url.strip():
            return self.backend_url.strip().rstrip("/")
        host = self.public_host.strip() or "127.0.0.1"
        return f"http://{host}:{self.api_port}"

    @property
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


def update_llm_settings(
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Settings:
    """Hot-swap LLM settings for the running process."""
    settings = get_settings()
    if model is not None:
        settings.llm_model = model
    if temperature is not None:
        settings.llm_temperature = temperature
    if max_tokens is not None:
        settings.llm_max_tokens = max_tokens
    return settings
