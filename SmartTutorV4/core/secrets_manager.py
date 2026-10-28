"""
Load the whole application configuration from AWS Secrets Manager at start-up.

Why this exists
---------------
Configuration used to live in the ECS task definition's ``environment`` array, which
means every value - the RDS password, ``JWT_SECRET_KEY``, the Pinecone API key, the
seeded admin passwords - was stored in plain text inside the task definition and was
readable by anyone with ``ecs:DescribeTaskDefinition``. Task-definition revisions are
also immutable and permanent, so a rotated secret stays legible in an old revision
forever.

Instead the task definition now carries exactly one pointer -
``AWS_SECRETS_MANAGER_SECRET_ID`` - and this module resolves it into ``os.environ``
before any application module reads a setting. Nothing else about how the code reads
configuration changes: every existing ``os.environ`` / ``os.getenv`` call keeps working
unmodified, which is what keeps this migration free of behavioural risk.

Ordering guarantee
------------------
``core/runtime.py`` calls :func:`load_secrets_into_environ` immediately after
``load_dotenv()`` and ``setup_logging()``, and ``core/runtime.py`` has no first-party
imports at all (``core/__init__.py`` is only ``from .runtime import *``, and the first
statement in ``main.py`` imports ``core.runtime``). So this module's work is finished
before the first module-level configuration read anywhere in the process.

Secrets Manager is authoritative: a key present in the secret overrides whatever the
process environment or ``.env`` already had, so a stale leftover task-definition entry
can never quietly win over the secret.

Local development is unaffected - with no pointer set this is a no-op and ``.env``
continues to drive the app.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

# The pointer to the secret. Primary name first; the shorter alias exists only so an
# existing task definition that already used it keeps working.
SECRET_ID_ENV_VARS = ("AWS_SECRETS_MANAGER_SECRET_ID", "SECRETS_MANAGER_SECRET_ID")

# Region for the GetSecretValue call itself, used only when the pointer is a bare
# secret NAME. A full ARN already carries its region, which is preferred over these.
REGION_ENV_VARS = ("AWS_SECRETS_MANAGER_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")

# Bounded so a network problem in the VPC surfaces as a fast, clear start-up failure
# instead of an ECS task that hangs in "activating" until the health check kills it.
_CONNECT_TIMEOUT_SECONDS = 5
_READ_TIMEOUT_SECONDS = 10
_MAX_ATTEMPTS = 3

_loaded = False


def _safe_for_log(value: Any) -> str:
    """Strip CR/LF before a value reaches the log (CodeQL log-injection)."""
    return str(value or "").replace("\r", "").replace("\n", "")


def _source_label(position: int, total: int) -> str:
    """
    Describe one configured secret WITHOUT naming it.

    Neither the secret id nor the pointer variable's value is ever written to a log
    record or an exception message. A secret ARN is not a credential, but it does carry
    the AWS account id and the secret's name, and start-up logs are far more widely
    readable than Secrets Manager itself - so identifying a source by its position in
    the configured list is both sufficient for diagnosis and free of disclosure. The
    position is what the operator needs anyway: the id is in the task definition, and
    with several comma-separated ids only the position says which one failed.
    """
    if total <= 1:
        return "the configured secret"
    return f"configured secret entry {position} of {total}"


def _env_value(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None:
        return None
    trimmed = raw.strip()
    # A quoted value is a common copy/paste artefact when pasting an ARN into a console.
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"'", '"'}:
        trimmed = trimmed[1:-1].strip()
    return trimmed or None


def _configured_secret_ids() -> List[str]:
    """
    Secret names/ARNs to load, in order. Comma separated, so dev can keep one shared
    secret plus a small per-service override secret; later entries win on conflict.
    """
    for name in SECRET_ID_ENV_VARS:
        raw = _env_value(name)
        if not raw:
            continue
        ids = [entry.strip() for entry in raw.split(",")]
        return [entry for entry in ids if entry]
    return []


def _region_for_secret(secret_id: str) -> Optional[str]:
    """
    Region of a full secret ARN (``arn:aws:secretsmanager:<region>:<account>:secret:...``),
    falling back to the configured AWS region for a bare secret name.
    """
    if secret_id.startswith("arn:"):
        parts = secret_id.split(":")
        # arn : partition : service : region : account : secret : name
        if len(parts) > 3 and parts[3]:
            return parts[3]
    for name in REGION_ENV_VARS:
        region = _env_value(name)
        if region:
            return region
    return None


def _build_client(region: Optional[str]):
    config = Config(
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        read_timeout=_READ_TIMEOUT_SECONDS,
        retries={"max_attempts": _MAX_ATTEMPTS, "mode": "standard"},
    )
    if region:
        return boto3.client("secretsmanager", region_name=region, config=config)
    return boto3.client("secretsmanager", config=config)


def _payload_from_response(response: Dict[str, Any], secret_label: str) -> str:
    """Return the secret payload from either SecretString or base64 SecretBinary."""
    payload = response.get("SecretString")
    if payload:
        return payload
    binary = response.get("SecretBinary")
    if not binary:
        return ""
    if isinstance(binary, (bytes, bytearray)):
        raw = bytes(binary)
    else:
        try:
            raw = base64.b64decode(str(binary), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(
                f"Could not use {secret_label}: its binary payload is not valid base64."
            ) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"Could not use {secret_label}: its binary payload is not UTF-8 text."
        ) from exc


def _stringify(value: Any) -> str:
    """
    Render one secret value as an environment-variable string.

    Booleans become lower-case ``true`` / ``false`` because that is what the app's own
    flag parsing and the ``.env`` file already use; anything structured is re-encoded as
    JSON rather than dropped, so no key is silently lost.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


def _flatten_secret(parsed: Any, secret_label: str) -> Dict[str, str]:
    """
    Accept both shapes DevOps realistically stores.

    * ``{"KEY": "value", ...}`` - the normal Secrets Manager key/value secret.
    * ``[{"name": "KEY", "value": "..."}, ...]`` - the ECS task-definition
      ``environment`` array, so the existing ``docs/*_credentials.json`` manifest can be
      pasted into the secret verbatim with no hand editing.
    """
    if isinstance(parsed, dict):
        # Keys are stripped, like the list branch below: a name typed into the console
        # with a stray leading/trailing space would otherwise be silently unreadable and
        # surface much later as "Database credentials missing".
        return {
            str(key).strip(): _stringify(value)
            for key, value in parsed.items()
            if str(key).strip()
        }

    if isinstance(parsed, list):
        flattened: Dict[str, str] = {}
        for entry in parsed:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"Could not use {secret_label}: it has a list entry that is not a "
                    "name/value object."
                )
            key = str(entry.get("name") or "").strip()
            if not key:
                raise RuntimeError(
                    f"Could not use {secret_label}: it has a list entry without a "
                    "'name' field."
                )
            flattened[key] = _stringify(entry.get("value"))
        return flattened

    raise RuntimeError(
        f"Could not use {secret_label}: a secret must be a JSON object of key/value "
        "pairs or an ECS-style name/value array."
    )


def _fetch_secret(secret_id: str, label: str) -> Dict[str, str]:
    """
    Resolve one secret to a flat key/value mapping.

    ``label`` is the position-based description from :func:`_source_label`. Neither the
    secret value nor the secret id reaches any message raised from here.
    """
    region = _region_for_secret(secret_id)
    try:
        client = _build_client(region)
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        # The AWS error CODE is the whole diagnostic value here (AccessDeniedException,
        # ResourceNotFoundException, DecryptionFailure...) and carries no secret data.
        code = ""
        error = getattr(exc, "response", None)
        if isinstance(error, dict):
            code = str(error.get("Error", {}).get("Code") or "")
        raise RuntimeError(
            f"Could not read {label} from AWS Secrets Manager"
            f"{f' ({_safe_for_log(code)})' if code else ''}. Check the id in "
            "AWS_SECRETS_MANAGER_SECRET_ID and that the ECS task role is allowed "
            "secretsmanager:GetSecretValue on it."
        ) from exc
    except BotoCoreError as exc:
        raise RuntimeError(
            f"Could not reach AWS Secrets Manager for {label} "
            f"({type(exc).__name__}). Check the task subnet route to the "
            "secretsmanager endpoint and that a region is configured."
        ) from exc

    payload = _payload_from_response(response, label)
    if not payload.strip():
        raise RuntimeError(f"Could not use {label}: it is empty.")

    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        # Deliberately does not include the payload - it is the secret.
        raise RuntimeError(
            f"Could not use {label}: it is not valid JSON. Store it as "
            "key/value pairs, not as plaintext."
        ) from exc

    return _flatten_secret(parsed, label)


def _is_valid_env_name(name: str) -> bool:
    """
    Accept only names the application could actually read back: POSIX-style
    ``[A-Za-z_][A-Za-z0-9_]*``. This intentionally covers the lower-case ``username`` /
    ``password`` keys the database URL builder uses, and rejects a stray JSON field such
    as a description or a note that was typed into the secret next to the real settings.
    """
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == "_") or not name[0].isascii():
        return False
    return all((char.isascii() and (char.isalnum() or char == "_")) for char in name)


def load_secrets_into_environ() -> int:
    """
    Resolve the configured secret(s) into ``os.environ`` and return the number of keys
    applied. Safe to call more than once - only the first call does any work.

    Fails fast: when a pointer is configured but the secret cannot be read or parsed,
    this raises instead of letting the app boot on partial configuration. The container
    image never ships ``.env`` (see ``.dockerignore``), so a silent fallback would start
    the app with default JWT/session/login settings and an unusable database - much
    harder to diagnose than a start-up error naming the secret and the AWS error code.
    """
    global _loaded
    if _loaded:
        return 0

    secret_ids = _configured_secret_ids()
    if not secret_ids:
        # The pointer variable's NAME is written as a literal rather than passed as a
        # format argument: nothing that could be mistaken for configuration data flows
        # into a log record from this module. `scripts/check_secrets_manager.py` asserts
        # this message still names the real SECRET_ID_ENV_VARS[0], so it cannot drift.
        logger.info(
            "Secrets Manager: no AWS_SECRETS_MANAGER_SECRET_ID configured; "
            "using the process environment / .env."
        )
        _loaded = True
        return 0

    total = len(secret_ids)
    resolved: Dict[str, str] = {}
    for position, secret_id in enumerate(secret_ids, start=1):
        values = _fetch_secret(secret_id, _source_label(position, total))
        # Counts and positions only - see _source_label() for why the id is not logged.
        logger.info(
            "Secrets Manager: loaded %s key(s) from source %s of %s.",
            len(values),
            position,
            total,
        )
        resolved.update(values)

    # The pointer itself is provided by the platform; a copy inside the secret must not
    # be able to redirect (or recurse into) the lookup that just happened.
    reserved = set(SECRET_ID_ENV_VARS)

    applied = 0
    skipped = 0
    for key, value in resolved.items():
        if key in reserved:
            continue
        # A NUL anywhere in the pair makes `os.environ[key] = value` raise a bare
        # "embedded null byte" ValueError, which would abort start-up with no indication
        # of which setting caused it. Skipping is both clearer and recoverable.
        if not _is_valid_env_name(key) or "\x00" in value:
            # Counted, not named: a key typed next to the real settings may itself be
            # descriptive of a secret, so only the count is safe to log.
            skipped += 1
            continue
        os.environ[key] = value
        applied += 1

    if skipped:
        logger.warning(
            "Secrets Manager: skipped %s key(s) that are not usable as environment variables.",
            skipped,
        )

    if "AWS_SESSION_TOKEN" in resolved:
        # Session tokens are short lived. Baking one into the secret means Bedrock, S3,
        # Polly and Transcribe all start failing with ExpiredToken a few hours after the
        # token was minted, with no code change to blame. The ECS task role is the fix.
        logger.warning(
            "Secrets Manager: the secret contains AWS_SESSION_TOKEN. Temporary "
            "credentials expire - prefer the ECS task role and remove the static "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN keys."
        )

    logger.info(
        "Secrets Manager: applied %s configuration key(s) to the environment.", applied
    )
    _loaded = True
    return applied


__all__ = [
    "REGION_ENV_VARS",
    "SECRET_ID_ENV_VARS",
    "load_secrets_into_environ",
]
