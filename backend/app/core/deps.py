"""Auth dependency — Bearer JWT → current user / admin."""

from __future__ import annotations

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from app.core.exceptions import AppError
from app.core.security import decode_access_token
from app.db.session import (
    DELETED_ACCOUNT_MESSAGE,
    UserRow,
    get_session,
    was_user_deleted,
)
from app.models.schemas import UserOut


def _to_user_out(row: UserRow) -> UserOut:
    return UserOut(
        id=row.id,
        email=row.email,
        name=row.name,
        role=getattr(row, "role", None) or "user",
        status=getattr(row, "status", None) or "pending",
    )


#: The JWT, declared as a security scheme rather than read as a bare header.
#:
#: Functionally the same as reading the header at runtime, but only a *scheme* reaches
#: OpenAPI — which is what puts the **Authorize** button in Swagger UI and a padlock on
#: every protected endpoint. Read as a plain `Header`, the docs page offered no way to send
#: a token, so every protected endpoint answered 401 from "Try it out".
#:
#: The password flow (rather than plain `HTTPBearer`) is what makes Authorize show a
#: username/password form: Swagger posts the credentials to `tokenUrl` itself and keeps the
#: returned token for every subsequent call, so nobody has to run login separately and
#: copy a token by hand. `username` is the email address. `client_id` / `client_secret`
#: come with the standard form and are ignored — this API has no registered clients.
#:
#: `tokenUrl` is an absolute path on purpose. Relative forms resolve against whatever page
#: Swagger was opened at, which breaks the moment /docs moves or sits behind a path prefix.
#:
#: `auto_error=False` keeps our own error shape: FastAPI's built-in rejection is a bare 401
#: `{"detail": "Not authenticated"}`, while the UI and CLI both branch on the 401 +
#: `message` that AppError produces (the CLI prints "run agentcraft auth login" off it).
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/token",
    description=(
        "Sign in with the email and password of an **approved** account. Leave `client_id` "
        "and `client_secret` blank. A pending or rejected account is refused here, exactly "
        "as it is in the UI."
    ),
    auto_error=False,
)


def get_current_user(token: str | None = Depends(oauth2_scheme)) -> UserOut:
    # OAuth2PasswordBearer hands over the bare token (the "Bearer " prefix already
    # stripped, the scheme compared case-insensitively) and None for a missing or
    # non-Bearer Authorization header alike.
    if token is None or not token.strip():
        raise AppError("Authentication required", status_code=401)
    token = token.strip()
    try:
        payload = decode_access_token(token)
    except Exception as exc:  # noqa: BLE001
        raise AppError("Invalid or expired token", status_code=401) from exc
    user_id = str(payload.get("sub") or "")
    if not user_id:
        raise AppError("Invalid token", status_code=401)
    with get_session() as db:
        row = db.get(UserRow, user_id)
        if not row:
            # A token issued before the admin deleted the account still decodes, so the
            # tab stays "logged in" until a request fails. 403 + this message tells the
            # UI to log out with an explanation rather than a bare "User not found".
            if was_user_deleted(user_id):
                raise AppError(DELETED_ACCOUNT_MESSAGE, status_code=403)
            raise AppError("User not found", status_code=401)
        user = _to_user_out(row)
        if user.role != "admin" and user.status != "approved":
            if user.status == "pending":
                raise AppError(
                    "Your account is waiting for admin approval",
                    status_code=403,
                )
            raise AppError("Your account was rejected by the admin", status_code=403)
        return user


def get_current_admin(user: UserOut = Depends(get_current_user)) -> UserOut:
    if user.role != "admin":
        raise AppError("Admin access required", status_code=403)
    return user
