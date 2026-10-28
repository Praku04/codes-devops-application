from dotenv import load_dotenv
import json
import logging
import os
import re
from datetime import timedelta
from typing import NamedTuple
from urllib.parse import urlparse

from fastapi import HTTPException
import jwt
from jwt.exceptions import PyJWTError as JWTError
from pydantic import BaseModel
import requests

load_dotenv()

logger = logging.getLogger(__name__)

# ── Provider selection ───────────────────────────────────────────────────────
# SSO_PROVIDER selects which IdP the "Sign in with SSO" button uses.
#   msal  (default) — Microsoft Entra ID via @azure/msal-browser
#   okta            — Okta OIDC Authorization Code + PKCE
_ALLOWED_SSO_PROVIDERS = ("msal", "okta")


def _resolve_sso_provider() -> str:
    raw = (os.getenv("SSO_PROVIDER") or "msal").strip().lower()
    if raw in _ALLOWED_SSO_PROVIDERS:
        return raw
    # Do not log the raw value (caller-influenced; Sonar python:S5145).
    logger.warning(
        "SSO_PROVIDER is not one of %s; using msal.",
        ", ".join(_ALLOWED_SSO_PROVIDERS),
    )
    return "msal"


SSO_PROVIDER = _resolve_sso_provider()

# ── Microsoft Entra (MSAL) ───────────────────────────────────────────────────
TENANT_ID = (os.getenv("TENANT_ID") or "").strip() or None
CLIENT_ID = (os.getenv("CLIENT_ID") or "").strip() or None
# Microsoft ID tokens are RS256 via JWKS (not HS256).
_MS_ALLOWED_ALGORITHMS = ["RS256"]

# ── Okta OIDC ────────────────────────────────────────────────────────────────
OKTA_CLIENT_ID = (os.getenv("OKTA_CLIENT_ID") or "").strip() or None
OKTA_ISSUER_RAW = (os.getenv("OKTA_ISSUER") or "").strip() or None
# ID-token audience; blank means the SPA client id (Okta default for ID tokens).
OKTA_AUDIENCE = (os.getenv("OKTA_AUDIENCE") or "").strip() or None
_OKTA_ALLOWED_ALGORITHMS = ["RS256"]
# Only these host suffixes may be used as Okta issuers (SSRF guard on JWKS fetch).
_OKTA_HOST_SUFFIXES = (".okta.com", ".oktapreview.com", ".okta-emea.com")
# Custom authorization-server path: /oauth2/default or /oauth2/<serverId>
_OKTA_AUTH_SERVER_PATH_RE = re.compile(r"^/oauth2/[A-Za-z0-9_-]+$")

# Shown to the caller, so worded once: configuration problems are a 500, token problems a 403.
_SSO_CONFIG_INCOMPLETE = "SSO configuration is incomplete. Please check environment variables."
_SSO_LOGIN_REJECTED = "Invalid idToken. Could not Login with SSO!"


class OktaEndpoints(NamedTuple):
    """The issuer and the endpoints derived from it, all on one allow-listed Okta host."""

    issuer: str
    jwks_url: str
    authorize_url: str
    token_url: str


def _ms_leeway_seconds() -> int:
    """Blank SSO_JWT_LEEWAY_SECONDS must not crash import — fall back to 60."""
    raw = (os.getenv("SSO_JWT_LEEWAY_SECONDS") or "").strip()
    if not raw:
        return 60
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("SSO_JWT_LEEWAY_SECONDS is not an integer; using 60.")
        return 60


_MS_LEEWAY_SECONDS = _ms_leeway_seconds()


def _is_allowed_okta_host(hostname: str) -> bool:
    host = (hostname or "").lower()
    if not host or ".." in host:
        return False
    return any(host == suffix[1:] or host.endswith(suffix) for suffix in _OKTA_HOST_SUFFIXES)


def _parse_okta_issuer(raw: str | None) -> OktaEndpoints | None:
    """
    Validate OKTA_ISSUER and derive JWKS / authorize / token URLs.

    Returns None when unset. Raises ValueError when set but unsafe/invalid so a
    misconfiguration fails closed instead of fetching JWKS from an arbitrary host.
    """
    if not raw:
        return None
    parsed = urlparse(raw.strip().rstrip("/"))
    if parsed.scheme != "https":
        raise ValueError("OKTA_ISSUER must use https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("OKTA_ISSUER must not include credentials, query, or fragment")
    host = (parsed.hostname or "").lower()
    if not _is_allowed_okta_host(host):
        raise ValueError("OKTA_ISSUER host is not an allowed Okta domain")
    if parsed.port not in (None, 443):
        raise ValueError("OKTA_ISSUER must use the default HTTPS port")
    path = (parsed.path or "").rstrip("/")
    if path and not _OKTA_AUTH_SERVER_PATH_RE.match(path):
        raise ValueError("OKTA_ISSUER path must be empty or /oauth2/<authorizationServerId>")

    issuer = f"https://{host}{path}"
    # A custom authorization server serves its endpoints under the issuer; the org authorization
    # server (issuer is the Okta domain itself) serves them under /oauth2.
    base = issuer if path else f"https://{host}/oauth2"
    return OktaEndpoints(
        issuer=issuer,
        jwks_url=f"{base}/v1/keys",
        authorize_url=f"{base}/v1/authorize",
        token_url=f"{base}/v1/token",
    )


try:
    _OKTA_ENDPOINTS = _parse_okta_issuer(OKTA_ISSUER_RAW)
except ValueError:
    logger.warning("OKTA_ISSUER is invalid; Okta SSO will be unavailable until it is fixed.")
    _OKTA_ENDPOINTS = None

JWKS_URL = (
    f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys" if TENANT_ID else ""
)
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0" if TENANT_ID else ""

_ms_jwks_cache = None
_okta_jwks_cache = None


def _fetch_ms_jwks():
    if not TENANT_ID:
        raise HTTPException(
            status_code=500,
            detail=_SSO_CONFIG_INCOMPLETE,
        )
    response = requests.get(JWKS_URL, timeout=10)
    response.raise_for_status()
    return response.json()


def _fetch_okta_jwks():
    if not _OKTA_ENDPOINTS:
        raise HTTPException(
            status_code=500,
            detail=_SSO_CONFIG_INCOMPLETE,
        )
    # URL is built only from allow-listed Okta hosts (see _parse_okta_issuer). Redirects are not
    # followed: a redirect would take the fetch to a host the allow-list never checked.
    response = requests.get(_OKTA_ENDPOINTS.jwks_url, timeout=10, allow_redirects=False)
    response.raise_for_status()
    return response.json()


def _get_ms_jwks(*, force_refresh: bool = False):
    """Return Microsoft JWKS; refreshable for key rotation."""
    global _ms_jwks_cache
    if force_refresh or _ms_jwks_cache is None:
        _ms_jwks_cache = _fetch_ms_jwks()
    return _ms_jwks_cache


def _get_okta_jwks(*, force_refresh: bool = False):
    """Return Okta JWKS; refreshable for key rotation."""
    global _okta_jwks_cache
    if force_refresh or _okta_jwks_cache is None:
        _okta_jwks_cache = _fetch_okta_jwks()
    return _okta_jwks_cache


# Back-compat aliases used by older imports / docs references.
def _fetch_jwks():
    return _fetch_ms_jwks()


def _get_jwks(*, force_refresh: bool = False):
    return _get_ms_jwks(force_refresh=force_refresh)


class TokenRequest(BaseModel):
    ms_token: str
    auth_type: str


class SSOConfigResponse(BaseModel):
    auth_type: str
    client_id: str
    tenant_id: str = ""
    issuer: str = ""
    authorize_url: str = ""
    token_url: str = ""
    cache_location: str = "sessionStorage"


# ── MSAL cache location (VAPT #10: HttpOnly missing) ─────────────────────────
# The POC flagged a `msal.cache.encryption` cookie with HttpOnly=false. That cookie is
# written by @azure/msal-browser itself, from JavaScript, via `document.cookie` - and a
# cookie written that way can NEVER carry HttpOnly, because HttpOnly exists precisely to
# hide a cookie from JavaScript. Adding the attribute is not an option the browser offers.
#
# The bundle contains exactly one `document.cookie =` write, inside the localStorage cache
# manager's initialize(). It stores the AES key that encrypts MSAL's own localStorage cache
# (NOT this application's session - that is a Bearer JWT, never a cookie). The
# sessionStorage cache manager has no such code path, so selecting sessionStorage removes
# the cookie at the source instead of trying to re-attribute it.
#
# Kept as an env knob so the previous behaviour can be restored by ops without a redeploy.
_ALLOWED_SSO_CACHE_LOCATIONS = ("sessionStorage", "localStorage", "memoryStorage")
_DEFAULT_SSO_CACHE_LOCATION = "sessionStorage"


def _resolve_sso_cache_location() -> str:
    """
    Cache location handed to MSAL in the browser.

    Anything unrecognised falls back to the secure default rather than being passed
    through: this value is interpolated into a `new msal.PublicClientApplication({...})`
    call, so it must be one of the three names MSAL defines and nothing else.
    """
    configured = (os.getenv("SSO_CACHE_LOCATION") or "").strip()
    if not configured:
        return _DEFAULT_SSO_CACHE_LOCATION

    for allowed in _ALLOWED_SSO_CACHE_LOCATIONS:
        if configured.lower() == allowed.lower():
            return allowed

    # Not logging the received value: it is caller-influenced configuration and would be a
    # log-injection sink (Sonar python:S5145 / CodeQL log injection).
    logger.warning(
        "SSO_CACHE_LOCATION is not one of %s; using the %s default.",
        ", ".join(_ALLOWED_SSO_CACHE_LOCATIONS),
        _DEFAULT_SSO_CACHE_LOCATION,
    )
    return _DEFAULT_SSO_CACHE_LOCATION


SSO_CACHE_LOCATION = _resolve_sso_cache_location()


def get_signing_key(kid: str):
    """Resolve Microsoft signing key by kid; refresh JWKS once if kid is unknown (rotation)."""
    jwks = _get_ms_jwks()
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key

    jwks = _get_ms_jwks(force_refresh=True)
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key

    raise HTTPException(status_code=403, detail="Invalid idToken signing key.")


def get_supported_sso_config():
    if SSO_PROVIDER == "okta":
        if not OKTA_CLIENT_ID or not _OKTA_ENDPOINTS:
            raise HTTPException(
                status_code=500,
                detail=_SSO_CONFIG_INCOMPLETE,
            )
        return {
            "auth_type": "okta",
            "client_id": OKTA_CLIENT_ID,
            "tenant_id": "",
            "issuer": _OKTA_ENDPOINTS.issuer,
            "authorize_url": _OKTA_ENDPOINTS.authorize_url,
            "token_url": _OKTA_ENDPOINTS.token_url,
            "cache_location": SSO_CACHE_LOCATION,
        }

    if not TENANT_ID or not CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail=_SSO_CONFIG_INCOMPLETE,
        )
    return {
        "auth_type": "msal",
        "client_id": CLIENT_ID,
        "tenant_id": TENANT_ID,
        # Validated against a fixed allow-list above, so the front end can use it directly.
        "cache_location": SSO_CACHE_LOCATION,
    }


def _oidc_decode_options() -> dict:
    # Align with app JWT policy: require critical time/identity claims.
    # nbf is optional on some IdP tokens — verify when present.
    return {
        "require": ["exp", "iat", "aud", "iss"],
        "verify_signature": True,
        "verify_exp": True,
        "verify_iat": True,
        "verify_nbf": True,
        "verify_aud": True,
        "verify_iss": True,
    }


def _decode_id_token_with_jwks(
    id_token: str,
    jwks: dict,
    *,
    audience: str,
    issuer: str,
    algorithms: list[str],
):
    """
    Verify the ID token by trying JWKS RSA keys until signature + claims succeed.

    Deliberately avoids ``jwt.get_unverified_header`` (Sonar python:S5659). IdP
    JWKS sets are small; only a key that cryptographically verifies the signature can
    produce a payload, so iterating keys does not weaken validation.
    """
    last_error: Exception | None = None
    for key in jwks.get("keys", []):
        try:
            signing_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
            return jwt.decode(
                id_token,
                signing_key,
                algorithms=algorithms,
                audience=audience,
                issuer=issuer,
                leeway=timedelta(seconds=max(0, _MS_LEEWAY_SECONDS)),
                options=_oidc_decode_options(),
            )
        except (JWTError, ValueError, TypeError) as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise JWTError("No JWKS signing keys available")


def _decode_ms_id_token_with_jwks(id_token: str, jwks: dict):
    return _decode_id_token_with_jwks(
        id_token,
        jwks,
        audience=CLIENT_ID,
        issuer=ISSUER,
        algorithms=_MS_ALLOWED_ALGORITHMS,
    )


def _decode_okta_id_token_with_jwks(id_token: str, jwks: dict):
    audience = OKTA_AUDIENCE or OKTA_CLIENT_ID
    return _decode_id_token_with_jwks(
        id_token,
        jwks,
        audience=audience,
        issuer=_OKTA_ENDPOINTS.issuer,
        algorithms=_OKTA_ALLOWED_ALGORITHMS,
    )


def _user_details_from_ms_claims(decoded: dict) -> dict:
    user_id = decoded.get("oid")
    email = decoded.get("email")
    adid_email = decoded.get("preferred_username")

    if not user_id:
        raise HTTPException(status_code=403, detail=_SSO_LOGIN_REJECTED)
    if (not email) and (not adid_email):
        raise HTTPException(status_code=403, detail="Login Failed. Email id not found in idToken!")

    return {
        "user_id": user_id,
        "email": email,
        "adidEmail": adid_email,
    }


def _user_details_from_okta_claims(decoded: dict) -> dict:
    # Okta ID tokens use `sub` as the stable subject; email arrives with the email scope.
    user_id = decoded.get("sub")
    # The SmartTutor account is selected by this address, so an address Okta has not verified must
    # not select one: where users may edit their own profile email, it could name someone else's
    # account. Such a login falls back to preferred_username, the Okta login the org assigns.
    email = decoded.get("email") if decoded.get("email_verified") is not False else None
    adid_email = decoded.get("preferred_username") or email

    if not user_id:
        raise HTTPException(status_code=403, detail=_SSO_LOGIN_REJECTED)
    if (not email) and (not adid_email):
        raise HTTPException(status_code=403, detail="Login Failed. Email id not found in idToken!")

    return {
        "user_id": user_id,
        "email": email,
        "adidEmail": adid_email,
    }


def get_user_details_from_id_token(auth_type: str, id_token: str):
    requested = (auth_type or "").strip().lower()
    if requested != SSO_PROVIDER:
        raise HTTPException(status_code=400, detail="Unsupported authentication method")

    if not id_token or not isinstance(id_token, str):
        raise HTTPException(status_code=403, detail=_SSO_LOGIN_REJECTED)

    if SSO_PROVIDER == "okta":
        if not OKTA_CLIENT_ID or not _OKTA_ENDPOINTS:
            raise HTTPException(
                status_code=500,
                detail=_SSO_CONFIG_INCOMPLETE,
            )
        try:
            decoded = _decode_okta_id_token_with_jwks(id_token, _get_okta_jwks())
        except JWTError:
            try:
                decoded = _decode_okta_id_token_with_jwks(
                    id_token, _get_okta_jwks(force_refresh=True)
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=403, detail=_SSO_LOGIN_REJECTED
                ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=403, detail=_SSO_LOGIN_REJECTED
            ) from exc
        return _user_details_from_okta_claims(decoded)

    # MSAL / Entra
    if not TENANT_ID or not CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail=_SSO_CONFIG_INCOMPLETE,
        )
    try:
        decoded = _decode_ms_id_token_with_jwks(id_token, _get_ms_jwks())
    except JWTError:
        # Key rotation: refresh JWKS once and retry with a verified decode only.
        try:
            decoded = _decode_ms_id_token_with_jwks(id_token, _get_ms_jwks(force_refresh=True))
        except Exception as exc:
            raise HTTPException(
                status_code=403, detail=_SSO_LOGIN_REJECTED
            ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=403, detail=_SSO_LOGIN_REJECTED
        ) from exc

    return _user_details_from_ms_claims(decoded)
