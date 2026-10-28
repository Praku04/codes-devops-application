"""Signup / login / me — signup requires admin approval before access."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func

from app.core.deps import get_current_user
from app.core.exceptions import AppError
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import (
    DELETED_ACCOUNT_MESSAGE,
    UserRow,
    clear_deleted_tombstone,
    get_session,
    was_email_deleted,
)
from app.models.schemas import (
    AuthResponse,
    LoginRequest,
    SignupPendingResponse,
    SignupRequest,
    TokenResponse,
    UserOut,
)

router = APIRouter(prefix="/auth")


def _user_out(row: UserRow) -> UserOut:
    return UserOut(
        id=row.id,
        email=row.email,
        name=row.name,
        role=getattr(row, "role", None) or "user",
        status=getattr(row, "status", None) or "pending",
    )


@router.post("/signup", response_model=SignupPendingResponse)
def signup(body: SignupRequest) -> SignupPendingResponse:
    """Create a pending account — no JWT until an admin approves."""
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise AppError("Enter a valid email", status_code=400)
    name = (body.name or "AgentCraft User").strip() or "AgentCraft User"
    with get_session() as db:
        existing = db.query(UserRow).filter(func.lower(UserRow.email) == email).first()
        if existing:
            raise AppError("An account with this email already exists", status_code=409)
        row = UserRow(
            id=str(uuid4()),
            email=email,
            name=name[:120],
            password_hash=hash_password(body.password),
            role="user",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
    # A previously deleted person may sign up again; the new account is not deleted.
    clear_deleted_tombstone(email)
    return SignupPendingResponse(
        message=(
            "Account created and sent for admin approval. "
            "You can log in only after the super admin approves your account."
        ),
        status="pending",
        email=email,
    )


def _authenticate(email: str, password: str) -> UserOut:
    """Check the credentials and the account's standing, or raise. Shared by both logins.

    `/login` (JSON, for the UI and CLI) and `/token` (form-encoded, for Swagger's Authorize
    dialog) differ only in how the credentials arrive, so the rules live here once. Two
    copies would eventually disagree — and a token endpoint that skipped, say, the rejected
    check would be a way around the approval gate.
    """
    email = email.strip().lower()
    with get_session() as db:
        row = db.query(UserRow).filter(func.lower(UserRow.email) == email).first()
        if not row:
            # Tell a deleted user what actually happened instead of "invalid password",
            # which reads like their own mistake and sends them retrying forever.
            if was_email_deleted(email):
                raise AppError(DELETED_ACCOUNT_MESSAGE, status_code=403)
            raise AppError("Invalid email or password", status_code=401)
        if not verify_password(password, row.password_hash):
            raise AppError("Invalid email or password", status_code=401)
        user = _user_out(row)
        if user.role != "admin":
            if user.status == "pending":
                raise AppError(
                    "Your account is waiting for admin approval. Please try again later.",
                    status_code=403,
                )
            if user.status == "rejected":
                raise AppError(
                    "Your account was rejected by the admin. Contact support.",
                    status_code=403,
                )
            if user.status != "approved":
                raise AppError("Account is not active", status_code=403)
        return user


@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest) -> AuthResponse:
    user = _authenticate(body.email, body.password)
    token = create_access_token(user_id=user.id, email=user.email)
    return AuthResponse(access_token=token, user=user)


@router.post("/token", response_model=TokenResponse, summary="Login (OAuth2 password flow)")
def token(form: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    """Exchange email + password for a token — this is what **Authorize** in /docs calls.

    Same credentials and same rules as `/login`; only the wire format differs. OAuth2's
    password flow posts `application/x-www-form-urlencoded`, and its username field is our
    email address. `client_id` / `client_secret` are part of the standard form and are
    ignored here — this API has no registered clients, so there is nothing to check them
    against; leave them blank in the dialog.

    Prefer `/login` from code: it returns the user object alongside the token, saving a
    round trip to `/me`.
    """
    user = _authenticate(form.username, form.password)
    return TokenResponse(access_token=create_access_token(user_id=user.id, email=user.email))


@router.get("/me", response_model=UserOut)
def me(user: UserOut = Depends(get_current_user)) -> UserOut:
    return user
