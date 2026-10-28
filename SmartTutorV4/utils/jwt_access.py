"""Access-token encode/decode with strict claim and algorithm validation (VAPT)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from jwt.exceptions import PyJWTError as JWTError

from core.runtime import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_LEEWAY_SECONDS,
    JWT_SECRET_KEY,
)

# Only this algorithm is accepted for Smart Tutor access tokens.
_ALLOWED_ALGORITHMS = [JWT_ALGORITHM]


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Issue a signed access token with required registered claims."""
    if not JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY is not configured")

    to_encode = dict(data)
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update(
        {
            "exp": expire,
            "iat": now,
            "nbf": now,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "token_use": "access",
        }
    )
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, *, verify_exp: bool = True) -> dict[str, Any]:
    """
    Fully validate an access token:
    - pinned algorithm (HS256 only; rejects none / RS* confusion)
    - signature
    - iss, aud, iat, nbf, sub (and exp when verify_exp=True)
    - clock leeway

    Set ``verify_exp=False`` only for ``POST /auth/refresh`` so a tab that was
    background-throttled past JWT expiry can still renew while the DB session is
    inside the idle/absolute windows. All other endpoints keep verify_exp=True.

    Algorithm and signature are enforced only by ``jwt.decode`` (Sonar python:S5659):
    reading the header with ``get_unverified_header`` first is unnecessary for HS256
    because the allowed-algorithms list already rejects ``none`` / RS confusion.
    """
    if not token or not JWT_SECRET_KEY:
        raise JWTError("Missing token or signing key")

    require_claims = ["iat", "nbf", "sub", "iss", "aud"]
    if verify_exp:
        require_claims = ["exp", *require_claims]

    return jwt.decode(
        token,
        JWT_SECRET_KEY,
        algorithms=_ALLOWED_ALGORITHMS,
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
        leeway=timedelta(seconds=max(0, JWT_LEEWAY_SECONDS)),
        options={
            "require": require_claims,
            "verify_signature": True,
            "verify_exp": verify_exp,
            "verify_nbf": True,
            "verify_iat": True,
            "verify_aud": True,
            "verify_iss": True,
        },
    )
