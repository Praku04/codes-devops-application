from __future__ import annotations

from contextvars import copy_context

from fastapi import APIRouter, Body

from core.runtime import *
from features.general.sso_helper import SSOConfigResponse, TokenRequest, get_supported_sso_config, get_user_details_from_id_token
from models.domain import *
from schemas.domain import *
from services.jobs import *
from services.auth_platform import *
from services.tutor_cache import *
from services.tutor_cache import normalize_module_index  # explicit: star-import can miss underscore aliases
from services.documents import *
from services.knowledge_base import *
from services.agents import *
from services.agents import _execute_share_reports, clone_agent_for_request
from services.knowledge_base import (
    _delete_data_for_source_document,
    _delete_specific_modules_for_course,
    _get_grouped_reports,
    _prepare_module_content_for_commit,
    _reindex_source_document,
    author_document_display_map,
    begin_agent_tool_context,
    count_vectors_in_namespace,
    course_display_label,
    course_document_display_map,
    course_notification_id,
    delete_source_document_completely,
    delete_vectors_for_source_document,
    display_name_for_stored_document,
    display_name_from_map,
    friendly_document_name,
    module_display_label,
    normalize_document_name,
    record_course_document_display_names,
    sanitize_notification_message,
    shared_source_display_label,
)
from services.module_index import author_report_metadata
# The per-route ceilings below come from services.rate_limits so the two enforcement points on
# these endpoints - this decorator and RoleRateLimitMiddleware - read the same configured value.
from services.rate_limits import (
    AUTH_REFRESH_RATE_LIMIT,
    AUTH_SIGNUP_RATE_LIMIT,
    AUTH_SSO_CONFIG_RATE_LIMIT,
    AUTH_SSO_RATE_LIMIT,
    AUTH_TOKEN_RATE_LIMIT,
    PROCESSES_RATE_LIMIT,
)
from utils.security_helpers import (
    GENERIC_API_ERROR_MESSAGE,
    log_safe,
    strip_agent_meta_commentary,
    strip_bracket_image_tags,
)
# Explicit: the star-imports above re-export only what schemas/domain.py itself imports,
# so this accessor would not otherwise resolve here.
from utils.input_validation import (
    normalize_voice_transcript,
    registration_email_domains,
    safe_session_id,
)
from utils.upload_validation import (
    VOICE_AUDIO_EXTENSIONS,
    assert_safe_upload_filename,
    safe_content_disposition,
    validate_module_document_upload,
)
# Chat/tutor microphone input is transcribed by AWS Transcribe (same service the Voice
# Training agent uses) instead of the previous third-party recognizer, so spoken audio never
# leaves our AWS account. The browser Web Speech API is the client-side fallback.
from utils.voice_training.transcribe_service import VoiceTranscribeService, is_audio_filename
from utils.login_throttle import (
    GENERIC_LOCKOUT_ERROR,
    GENERIC_LOGIN_ERROR,
    apply_progressive_delay,
    clear_failed_attempts,
    client_ip,
    is_locked_out,
    normalize_account,
    record_failed_attempt,
)

router = APIRouter()

ALLOWED_FONTAWESOME_FILES = frozenset({
    "fa-solid-900.woff2",
    "fa-regular-400.woff2",
    "fa-brands-400.woff2",
})

FONTAWESOME_WEBFONTS_DIR = Path("static/vendor/fontawesome/webfonts")
FONTAWESOME_MIN_BYTES = 1024
def _fetch_fa_solid_github() -> bytes:
    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    with urllib.request.urlopen(
        "https://raw.githubusercontent.com/FortAwesome/Font-Awesome/6.x/webfonts/fa-solid-900.woff2",
        context=ctx,
        timeout=60,
    ) as resp:
        return resp.read()


def _fetch_fa_solid_jsdelivr() -> bytes:
    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    with urllib.request.urlopen(
        "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.5.2/webfonts/fa-solid-900.woff2",
        context=ctx,
        timeout=60,
    ) as resp:
        return resp.read()


def _fetch_fa_regular_github() -> bytes:
    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    with urllib.request.urlopen(
        "https://raw.githubusercontent.com/FortAwesome/Font-Awesome/6.x/webfonts/fa-regular-400.woff2",
        context=ctx,
        timeout=60,
    ) as resp:
        return resp.read()


def _fetch_fa_regular_jsdelivr() -> bytes:
    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    with urllib.request.urlopen(
        "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.5.2/webfonts/fa-regular-400.woff2",
        context=ctx,
        timeout=60,
    ) as resp:
        return resp.read()


def _fetch_fa_brands_github() -> bytes:
    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    with urllib.request.urlopen(
        "https://raw.githubusercontent.com/FortAwesome/Font-Awesome/6.x/webfonts/fa-brands-400.woff2",
        context=ctx,
        timeout=60,
    ) as resp:
        return resp.read()


def _fetch_fa_brands_jsdelivr() -> bytes:
    import ssl
    import urllib.request

    ctx = ssl.create_default_context()
    with urllib.request.urlopen(
        "https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.5.2/webfonts/fa-brands-400.woff2",
        context=ctx,
        timeout=60,
    ) as resp:
        return resp.read()


FONTAWESOME_CDN_FETCHERS_BY_FILE = {
    "fa-solid-900.woff2": (_fetch_fa_solid_github, _fetch_fa_solid_jsdelivr),
    "fa-regular-400.woff2": (_fetch_fa_regular_github, _fetch_fa_regular_jsdelivr),
    "fa-brands-400.woff2": (_fetch_fa_brands_github, _fetch_fa_brands_jsdelivr),
}

# Static allowlist map: only these keys may be used for on-disk font paths.
ALLOWLISTED_FONT_FILENAMES = {name: name for name in ALLOWED_FONTAWESOME_FILES}


@router.get("/api/webfonts/{filename}", tags=["General & Frontend"])
def serve_fontawesome_webfont(filename: str):
    """Serve Font Awesome webfonts from disk (legacy path for cached CSS)."""
    if filename not in ALLOWLISTED_FONT_FILENAMES:
        raise HTTPException(status_code=404, detail="Font not found")

    font_name = ALLOWLISTED_FONT_FILENAMES[filename]

    from utils.security_helpers import (
        file_response_under_base,
        file_size_under_base,
        is_regular_file_under_base,
        log_safe,
        unlink_file_under_base,
        write_bytes_under_base,
    )

    font_cache_headers = {"Cache-Control": "public, max-age=86400"}

    if is_regular_file_under_base(FONTAWESOME_WEBFONTS_DIR, font_name):
        try:
            size = file_size_under_base(FONTAWESOME_WEBFONTS_DIR, font_name)
        except OSError:
            size = 0
        if size >= FONTAWESOME_MIN_BYTES:
            try:
                return file_response_under_base(
                    FONTAWESOME_WEBFONTS_DIR,
                    font_name,
                    "font/woff2",
                    font_cache_headers,
                )
            except FileNotFoundError:
                pass
        unlink_file_under_base(FONTAWESOME_WEBFONTS_DIR, font_name)

    last_error = None
    for fetch_font in FONTAWESOME_CDN_FETCHERS_BY_FILE[font_name]:
        try:
            data = fetch_font()
            if len(data) < FONTAWESOME_MIN_BYTES:
                raise ValueError(f"download too small ({len(data)} bytes)")
            write_bytes_under_base(FONTAWESOME_WEBFONTS_DIR, font_name, data)
            return Response(
                content=data,
                media_type="font/woff2",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except Exception as exc:
            last_error = exc
            logger.warning("[FONTS] %s fetch failed: %s", ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), exc)

    logger.error("[FONTS] Failed to load %s: %s", ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), last_error)
    raise HTTPException(status_code=502, detail="Font unavailable")


@router.get('/status', tags=["Health Check for Smart Tutor V4"])
async def health():
    return "Success - Smart Tutor V4 Endpoints are Successfully Integrated..!"
    
@router.get("/", response_class=HTMLResponse, tags=["General & Frontend"])
async def read_root(request: Request):
    """
    Serves the main single-page application (SPA) frontend.

    This is the main entry point for users accessing the web interface.
    It renders and returns the `index.html` template.
    """
    import time
    timestamp = str(int(time.time()))  # Unix timestamp for cache busting
    # The registration allow-list is rendered into the page (VAPT: Temporary email
    # accepted) so the signup form's hint and pre-check come from the SERVER's
    # SIGNUP_ALLOWED_EMAIL_DOMAINS. Adding a domain is then only an env change plus a
    # restart - static/script.js never needs editing, and the browser hint cannot drift
    # from the rule the server actually enforces.
    # return templates.TemplateResponse("index.html", {"request": request, "timestamp": timestamp})
    return templates.TemplateResponse(
    request=request,
    name="index.html",
    context={
        "timestamp": timestamp,
        "signup_email_domains": list(registration_email_domains()),
        # Session renewal knobs, so the browser's refresh timer is driven by the same
        # values the server issues tokens with (VAPT: Improper Token Expiration).
        "session_refresh_enabled": bool(SESSION_REFRESH_ENABLED),
        "session_refresh_lead_minutes": int(SESSION_REFRESH_LEAD_MINUTES),
        "access_token_expire_minutes": int(ACCESS_TOKEN_EXPIRE_MINUTES),
        # True idle logout: minutes of no REAL user interaction before the browser logs
        # itself out, and how many seconds ahead of that it warns. The server-side idle
        # timer remains the actual enforcement; this stops an open-but-abandoned tab from
        # holding a session alive with its own background polling.
        "client_idle_logout_minutes": int(CLIENT_IDLE_LOGOUT_MINUTES),
        "client_idle_warning_seconds": int(CLIENT_IDLE_WARNING_SECONDS),
        # Dialect for the browser Web Speech fallback behind the chat / tutor microphone.
        "chat_transcribe_language": CHAT_TRANSCRIBE_LANGUAGE,
    },
)
@router.get("/processes", response_model=List[ProcessModel], tags=["General & Frontend"])
@limiter_with_db.limit(PROCESSES_RATE_LIMIT)
def get_processes(request: Request, db: Session = Depends(get_db)):
    """
    Retrieves a list of all available processes/teams.

    This endpoint is used on the signup page to populate the dropdown
    list of processes a new user can join. It is lightly rate-limited.
    """
    processes = db.query(Process).order_by(Process.name).all()
    return processes

_PENDING_APPROVAL_NOTIF_RE = re.compile(
    r"New trainer '([^']+)' has registered and requires approval\."
)


def _filter_stale_approval_notifications(db: Session, notifications: List[Notification]) -> List[Notification]:
    """Hide approval alerts once the referenced user is approved or no longer exists."""
    try:
        emails_by_notif: Dict[str, List[Notification]] = {}
        for notif in notifications:
            match = _PENDING_APPROVAL_NOTIF_RE.search(notif.message or "")
            if match:
                emails_by_notif.setdefault(match.group(1), []).append(notif)
        if not emails_by_notif:
            return notifications

        users_by_email = {
            u.email: u
            for u in db.query(User).filter(User.email.in_(list(emails_by_notif.keys()))).all()
        }
        stale_ids = set()
        for email, notifs in emails_by_notif.items():
            target = users_by_email.get(email)
            if not target or target.is_approved:
                stale_ids.update(n.id for n in notifs)

        if stale_ids:
            db.query(Notification).filter(Notification.id.in_(stale_ids)).update(
                {Notification.is_read: True},
                synchronize_session=False,
            )
            db.commit()
            return [n for n in notifications if n.id not in stale_ids]
        return notifications
    except Exception as exc:
        logger.warning('[NOTIFICATIONS] Stale approval filter skipped: %s', log_safe(exc))
        db.rollback()
        return notifications


@router.get("/notifications", response_model=List[NotificationModel], tags=["General & Frontend"])
def get_notifications(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves notifications for the currently authenticated user.

    Fetches the 50 most recent notifications for the user, ordered by creation date.
    """
    try:
        notifications = (
            db.query(Notification)
            .filter(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc())
            .limit(50)
            .all()
        )
        visible = _filter_stale_approval_notifications(db, notifications)

        # Rows written before the message builders resolved display names still quote a
        # randomized storage name (VAPT #4) or a module's "<uuid>.md" filename. Rewrite them for
        # display only: new NotificationModel objects are returned so the ORM rows stay untouched
        # and no sanitized text can be flushed back over the stored audit trail.
        display_map = author_document_display_map(db, user.id)
        return [
            NotificationModel(
                id=note.id,
                message=sanitize_notification_message(note.message, display_map),
                related_link=note.related_link,
                created_at=note.created_at,
                is_read=note.is_read,
            )
            for note in visible
        ]
    except Exception as exc:
        logger.error('[NOTIFICATIONS] Failed to load notifications for user %s: %s', log_safe(user.id), log_safe(exc), exc_info=True)
        raise HTTPException(status_code=503, detail="Unable to load notifications. Please try again.")

@router.post("/notifications/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT, tags=["General & Frontend"])
def mark_notification_as_read(notification_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Marks a specific notification as read for the current user.

    - **notification_id**: The UUID of the notification to mark as read.
    - **Raises 404**: If the notification is not found for the current user.
    """
    notification = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user.id).first()
    if not notification: raise HTTPException(status_code=404, detail="Notification not found")
    notification.is_read = True; db.commit()
    return Response(status_code=204)

# --- Authentication Endpoints ---
@router.post("/auth/signup", status_code=status.HTTP_201_CREATED, tags=["Authentication"])
@limiter_with_db.limit(AUTH_SIGNUP_RATE_LIMIT)
def create_user(
    request: Request,
    data: UserCreate = Body(...),
    db: Session = Depends(get_db),
):
    """
    Handles new user registration.

    Creates a new user with the role of 'trainee' or 'trainer'.
    - Trainee accounts are approved by default.
    - Trainer accounts are set to pending and require super admin approval.
    - **Raises 400**: If the email is already registered or the process ID is invalid.
    """
    from sqlalchemy.exc import IntegrityError

    try:
        role = (data.role or "trainee").strip().lower()
        if role not in ("trainee", "trainer"):
            raise HTTPException(
                status_code=400,
                detail="Invalid role. Please register as trainee or trainer.",
            )

        # `UserCreate` already canonicalized this; normalizing again is cheap and keeps the
        # route correct even if it is ever called with a plain dict.
        email = normalize_email(data.email)
        if not email:
            raise HTTPException(status_code=400, detail="Email is required")

        # Case-insensitive, so signing up as `Phani@st.com` when `phani@st.com` already
        # exists is correctly refused as a duplicate instead of creating a second account.
        if get_user_from_db(db, email=email):
            raise HTTPException(status_code=400, detail="Email already registered")

        process = db.query(Process).filter(Process.id == data.process_id).first()
        if not process:
            raise HTTPException(status_code=400, detail="Invalid Process ID")

        process_id = str(process.id)
        process_name = str(process.name)

        try:
            hashed = get_password_hash(data.password)
        except Exception as hash_exc:
            logger.error("Signup password hash failed: %s", log_safe(hash_exc), exc_info=True)
            raise HTTPException(
                status_code=400,
                detail="Unable to process password. Please try a different password.",
            ) from hash_exc

        # Commit the user alone first so admin notifications / activity logging
        # cannot roll back a successful registration (common cause of opaque 500s).
        db_user = User(
            email=email,
            hashed_password=hashed,
            process_id=process_id,
            role=role,
            is_approved=(role == "trainee"),
        )
        db.add(db_user)
        try:
            db.commit()
            db.refresh(db_user)
        except IntegrityError as integrity_exc:
            db.rollback()
            logger.warning("Signup integrity error: %s", log_safe(integrity_exc))
            raise HTTPException(status_code=400, detail="Email already registered") from integrity_exc

        user_id = str(db_user.id)
        user_email = str(db_user.email)
        user_role = str(db_user.role)
        user_approved = bool(db_user.is_approved)

        if role == "trainer":
            try:
                admins = db.query(User).filter(User.role == "super_admin").all()
                safe_email = log_safe(user_email) or "new trainer"
                for admin in admins:
                    db.add(Notification(
                        user_id=admin.id,
                        message=f"New trainer '{safe_email}' has registered and requires approval.",
                        related_link="/super-admin",
                    ))
                db.commit()
            except Exception as notif_exc:
                db.rollback()
                logger.warning("Signup admin notification skipped: %s", log_safe(notif_exc))

        try:
            log_activity(db, user_id, "USER_SIGNUP", f"Role: {role}")
        except Exception as log_exc:
            logger.warning("Signup activity log skipped: %s", log_safe(log_exc))

        # Return JSONResponse directly (skip ORM / response_model re-validation paths
        # that were producing opaque 500s after expire_on_commit / DetachedInstanceError).
        payload = UserInDB(
            id=user_id,
            email=user_email,
            role=user_role,
            is_approved=user_approved,
            process=ProcessModel(id=process_id, name=process_name),
        ).model_dump()
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=payload)

    except HTTPException:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error("Signup failed: %s", log_safe(exc), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to create account (signup-v2). Please try again.",
        ) from exc

@router.get("/auth/sso-config", response_model=SSOConfigResponse, tags=["Authentication"])
@limiter_with_db.limit(AUTH_SSO_CONFIG_RATE_LIMIT)
def get_sso_config(request: Request):
    """
    Sends config details about app to frontend to perform SSO login.
    """
    return get_supported_sso_config()

@router.post("/auth/sso", response_model=Token, tags=["Authentication"])
@limiter_with_db.limit(AUTH_SSO_RATE_LIMIT)
def sso_login_for_access_token(request: Request, body:TokenRequest, db: Session = Depends(get_db)):
    """
    Authenticates a user via an IdP id_token (MSAL or Okta) and returns a JWT access token.

    - **Raises 401**: If credentials are incorrect.
    - **Raises 403**: If a trainer's account is not yet approved.
    """

    user_info = get_user_details_from_id_token(body.auth_type, body.ms_token)
    
    # `get_user_from_db` normalizes, so an IdP that returns the address in a different
    # case than the one on record still resolves to the same account.
    user = get_user_from_db(db, email=user_info.get("email"))
    # print("user from db:",user)

    if not user:
        user = get_user_from_db(db, email=user_info.get("adidEmail"))
        if not user:
            raise HTTPException(status_code=403, detail="User not registered in SmartTutor!")
    
    if user.role == 'trainer' and not user.is_approved:
        raise HTTPException(status_code=403, detail="Your trainer account has not been approved by an admin yet.")
    # Create access token
    access_token = create_access_token(
        data={"sub": user.email, "role": user.role, "uid": user.id}
    )
    
    # Create user session for live activity tracking
    create_user_session(db, user.id, access_token, request)
    
    # Log activity
    log_activity(db, user.id, "USER_LOGIN_SSO")
    log_activity(db, user.id, "SESSION_START", f"Role: {user.role}, Process: {user.process.name if user.process else 'None'}")
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }

@router.post("/auth/token", response_model=Token, tags=["Authentication"])
@limiter_with_db.limit(AUTH_TOKEN_RATE_LIMIT)
def login_for_access_token(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(OAuth2PasswordRequestForm),
    db: Session = Depends(get_db),
):
    """
    Authenticates a user via email and password and returns a JWT access token.

    This endpoint uses the OAuth2PasswordRequestForm dependency, so the client
    must send the credentials as form data (e.g., `username=...&password=...`).

    - **Raises 401**: If credentials are incorrect.
    - **Raises 403**: If a trainer's account is not yet approved.
    - **Raises 429**: If too many recent failures for this account or IP
      (VAPT: Password Bruteforcible).
    """
    account = normalize_account(form_data.username)
    ip_address = client_ip(request)

    # Check the lockout *before* verifying the password: a throttled attempt must not
    # even cost a bcrypt comparison, and must not reveal whether the account exists.
    locked, retry_after = is_locked_out(db, account, ip_address)
    if locked:
        logger.warning("Login blocked by brute-force throttle for scope=account/ip")
        raise HTTPException(
            status_code=429,
            detail=GENERIC_LOCKOUT_ERROR,
            headers={"Retry-After": str(retry_after)},
        )

    # Progressive delay grows with the failures already on record for this
    # account/IP, so scripted guessing gets slower with every try.
    apply_progressive_delay(db, account, ip_address)

    user = get_user_from_db(db, email=form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        # Log without the submitted identifier — a brute-force run would otherwise
        # write attacker-controlled text on every request (Sonar S5145).
        logger.warning("Login failed: invalid credentials.")
        record_failed_attempt(db, account, ip_address)
        # Identical message whether the email is unknown or the password is wrong,
        # so the response cannot be used to enumerate accounts.
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

    # Successful password check clears this account's failure budget.
    clear_failed_attempts(db, account, ip_address)

    if user.role == 'trainer' and not user.is_approved:
        raise HTTPException(status_code=403, detail="Your trainer account has not been approved by an admin yet.")
    
    # Create access token
    access_token = create_access_token(
        data={"sub": user.email, "role": user.role, "uid": user.id}
    )
    
    # Create user session for live activity tracking
    create_user_session(db, user.id, access_token, request)
    
    # Log activity
    log_activity(db, user.id, "USER_LOGIN")
    log_activity(db, user.id, "SESSION_START", f"Role: {user.role}, Process: {user.process.name if user.process else 'None'}")
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }

@router.post("/auth/refresh", response_model=Token, tags=["Authentication"])
@limiter_with_db.limit(AUTH_REFRESH_RATE_LIMIT)
def refresh_access_token(
    request: Request,
    user: User = Depends(get_current_active_user_for_refresh),
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """
    Exchange a still-valid (or just-expired) access token for a fresh one, so an active
    user is not logged out mid-task (VAPT: Improper Token Expiration - remediation follow-up).

    ``exp`` alone does not end the session: the DB idle/absolute windows remain the ceiling.
    A background tab that missed silent renewal can still refresh with an expired JWT while
    the session row is active. A revoked, idle, or absolute-expired session still returns 401.

    This does NOT weaken the expiry control. Reaching this endpoint requires:
      - signature + iss/aud/iat/nbf (and role/uid) validation
      - the server-side session row still exists and is_active (logout / revoke blocks it)
      - the session is inside BOTH the idle and the absolute window

    The absolute ceiling from `login_time` is preserved by `rotate_session_token`.

    - **Raises 401**: If the token is invalid, revoked, or the session has timed out.
    - **Raises 403**: If renewal is disabled by configuration.
    """
    if not SESSION_REFRESH_ENABLED:
        raise HTTPException(status_code=403, detail="Session renewal is disabled.")

    new_token = create_access_token(
        data={"sub": user.email, "role": user.role, "uid": user.id}
    )

    # Move the existing session row onto the new token. Same row, so login_time - and
    # therefore the absolute timeout - is inherited rather than reset.
    rotate_session_token(db, token, new_token)

    # Deliberately not logged per-refresh: this fires every few minutes per active user and
    # would bury the real SESSION_START/SESSION_END audit trail in noise.
    return {
        "access_token": new_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


@router.post("/auth/logout", tags=["Authentication"])
def logout(user: User = Depends(get_current_active_user_pending_password_change), token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """
    Logs out the current user by ending their session.
    """
    try:
        # End the user session
        end_user_session(db, token)
        
        # Log the logout activity
        log_activity(db, user.id, "USER_LOGOUT")
        log_activity(db, user.id, "SESSION_END", f"Role: {user.role}")
        
        return {"message": "Successfully logged out"}
    except Exception as e:
        logger.error('Error during logout: %s', e)
        return {"message": "Logout completed"}

@router.post("/auth/change-password", tags=["Authentication"])
@limiter_with_db.limit(AUTH_TOKEN_RATE_LIMIT)
def change_password(
    request: Request,
    body: ChangePasswordRequest,
    user: User = Depends(get_current_active_user_pending_password_change),
    db: Session = Depends(get_db),
):
    """
    Replaces the caller's password. Required at first login for admin-created accounts, which
    start on the St@<EmployeeID>#2026 login; reachable while every other route still refuses.

    - **Raises 400**: if the current password is wrong, or the new one equals it.
    - **Raises 422**: if the new password does not meet the password policy.
    """
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Your current password is incorrect.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="Choose a new password that is different from your current one.")
    user.hashed_password = get_password_hash(body.new_password)
    user.must_change_password = False
    db.commit()
    log_activity(db, user.id, "PASSWORD_CHANGED")
    return {"message": "Your password has been changed."}


@router.get("/auth/me", response_model=UserInDB, tags=["Authentication"])
def read_users_me(current_user: User = Depends(get_current_active_user_pending_password_change)):
    """
    Retrieves the complete profile of the currently authenticated user.

    This is a protected endpoint used to verify a token and get user details. Reachable before
    a starting login is replaced: `must_change_password` in the response is how the front end
    knows to show the change-password screen.
    """
    logger.info('[AUTH ME] User authenticated: %s (ID: %s, Role: %s)', ("" if current_user.email is None else str(current_user.email).replace("\n", "").replace("\r", "")), ("" if current_user.id is None else str(current_user.id).replace("\n", "").replace("\r", "")), ("" if current_user.role is None else str(current_user.role).replace("\n", "").replace("\r", "")))
    return current_user

# --- Workflow & Jobs Endpoints ---
@router.post("/workflow/process", status_code=status.HTTP_202_ACCEPTED, tags=["Workflow & Jobs"])
def process_docs(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    prompt_by_user: Optional[str] = Form(None),
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """
    Starts an asynchronous job to process uploaded documents (PDF, DOCX, PPTX, or Image).

    This endpoint accepts one or more files and an optional user prompt.
    It initiates a background task to perform document analysis, text/image extraction,
    chunking, embedding, and module generation. A job ID is returned immediately.

    Supported formats: PDF, DOCX, DOC, PPTX, PPT, PNG, JPG, JPEG
    DOCX and PPTX files are converted to PDF in the background job before processing.

    - **Required Role**: Trainer or Super Admin.
    - **Returns**: A job ID to poll for status.
    - **Raises 400**: If no files are uploaded or unsupported format.
    
    OPTIMIZED: Detects duplicate PDFs to prevent unnecessary regeneration.
    IMPORTANT: All uploaded documents will be combined into ONE course with multiple modules.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")
    
    # Inform user about multi-document processing
    if len(files) > 1:
        logger.info('Processing %s documents - will create ONE course with modules from all documents', len(files))
    
    # Log document upload activity
    db = SessionLocal()
    try:
        log_activity(db, user.id, "DOCUMENT_UPLOAD", f"Uploaded {len(files)} file(s): {', '.join([f.filename for f in files])}")
    finally:
        db.close()
    
    files_data = []
    duplicate_warnings = []
    
    for f in files:
        content = asyncio.run(f.read())
        original_filename = f.filename

        # VAPT: Double Extension File Upload — allow-list + magic bytes + reject
        # deceptive names (e.g. dummy-pdf.exe.pdf / file.php.jpg). Never trust
        # client Content-Type alone. Storage name is randomized to a single ext.
        ok, err, storage_filename, content_type = validate_module_document_upload(
            original_filename, content
        )
        if not ok:
            raise HTTPException(status_code=400, detail=err)

        processing_filename = storage_filename
        original_bytes = content
        file_ext = storage_filename.rsplit(".", 1)[-1].lower()
        
        # OPTIMIZATION: Check for duplicate PDFs, Office files, and Images
        if content_type in ['application/pdf', 'image/png', 'image/jpeg', 'image/jpg']:
            file_hash = calculate_pdf_hash(content)  # Works for any file type
            
            # Check if this file was already uploaded by this user
            # For PDFs, check Course table
            # For images, check if a course exists with this source document name
            existing_course = None
            
            if content_type == 'application/pdf':
                existing_course = db.query(Course).filter(
                    Course.pdf_hash == file_hash,
                    Course.author_id == user.id
                ).first()
            else:
                # Images: content-hash duplicate check (storage name is randomized)
                existing_course = db.query(Course).filter(
                    Course.pdf_hash == file_hash,
                    Course.author_id == user.id
                ).first()
            
            if existing_course:
                logger.warning("[DUPLICATE] File '%s' matches existing course '%s'", ("" if original_filename is None else str(original_filename).replace("\n", "").replace("\r", "")), ("" if existing_course.source_doc_name is None else str(existing_course.source_doc_name).replace("\n", "").replace("\r", "")))
                duplicate_warnings.append({
                    "filename": original_filename,
                    "existing_course_name": existing_course.source_doc_name,
                    "existing_course_id": existing_course.id,
                    "file_hash": file_hash,
                    "file_type": content_type
                })
        elif file_ext in ['docx', 'doc', 'pptx', 'ppt']:
            file_hash = calculate_pdf_hash(original_bytes)
            existing_course = db.query(Course).filter(
                Course.pdf_hash == file_hash,
                Course.author_id == user.id
            ).first()
            if existing_course:
                logger.warning(
                    "[DUPLICATE] Office file '%s' matches existing course '%s'",
                    ("" if original_filename is None else str(original_filename).replace("\n", "").replace("\r", "")),
                    ("" if existing_course.source_doc_name is None else str(existing_course.source_doc_name).replace("\n", "").replace("\r", "")),
                )
                duplicate_warnings.append({
                    "filename": original_filename,
                    "existing_course_name": existing_course.source_doc_name,
                    "existing_course_id": existing_course.id,
                    "file_hash": file_hash,
                    "file_type": content_type
                })
        
        # Store: processing_filename (randomized), content, content_type, original_filename, original_bytes
        files_data.append((processing_filename, content, content_type, original_filename, original_bytes))
    
    # If duplicates found, return warning instead of processing
    if duplicate_warnings:
        return {
            "duplicate_detected": True,
            "warnings": duplicate_warnings,
            "message": "One or more files have already been uploaded. You can view existing modules or proceed to generate new ones.",
            "action_required": True
        }
    
    job_id = str(uuid.uuid4())
    
    # Create persistent job status
    create_job_status(job_id, user.id, db)
    
    background_tasks.add_task(background_processing_task, job_id, user.id, files_data, prompt_by_user)
    return {"job_id": job_id, "duplicate_detected": False}

@router.post("/workflow/regenerate", status_code=status.HTTP_202_ACCEPTED, tags=["Workflow & Jobs"])
def regenerate_docs(
    req: RegenerationRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """
    Starts an asynchronous job to regenerate modules for a source document.

    This takes a new user prompt and re-runs the agentic generation process,
    using the original document and previously generated modules as context.

    - **Required Role**: Trainer or Super Admin.
    - **Returns**: A job ID to poll for status.
    """
    try:
        # Find the course - prefer course_id lookup (unambiguous), fallback to source_doc_name + author
        if req.course_id:
            course = db.query(Course).filter(Course.id == req.course_id).first()
            if not course:
                raise HTTPException(status_code=404, detail=f"Course '{req.course_id}' not found")
        else:
            # Fallback: filter by source_doc_name AND author to avoid cross-user collisions
            if user.role == 'super_admin':
                # Super admin: find by source_doc_name only (may be any user's course)
                course = db.query(Course).filter(
                    Course.source_doc_name == req.source_doc_name
                ).first()
            else:
                # Trainer: only look up their own course to prevent false 403
                course = db.query(Course).filter(
                    Course.source_doc_name == req.source_doc_name,
                    Course.author_id == user.id
                ).first()
            if not course:
                raise HTTPException(status_code=404, detail=f"Course '{req.source_doc_name}' not found")
        
        # Check authorization: user must be course owner OR super admin
        if course.author_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="Not authorized to regenerate this course")
        
        # Use course owner's ID for S3 access (not the logged-in user's ID)
        course_owner_id = course.author_id
        is_super_admin_action = (user.role == 'super_admin' and course.author_id != user.id)
        
        # Log regeneration activity
        log_activity(db, user.id, "MODULE_REGENERATION", f"Regenerating '{req.source_doc_name}' with prompt: {req.follow_up_prompt[:100]}...")
        
        job_id = str(uuid.uuid4())
        
        # Create persistent job status
        create_job_status(job_id, user.id, db)
        
        # Pass course_owner_id to background task instead of user.id
        background_tasks.add_task(
            background_regeneration_task, 
            job_id, 
            course_owner_id,  # Use course owner's ID for S3 access
            req.source_doc_name, 
            req.follow_up_prompt,
            is_super_admin_action,
            user.id if is_super_admin_action else None  # Pass super admin ID for notification
        )
        
        return {"job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error starting regeneration: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start regeneration job.")

@router.post("/kb/regenerate-course-title", tags=["Knowledge Base (Trainer)"])
def regenerate_course_title(
    req: dict,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """
    Regenerate AI-powered course title based on comprehensive module content analysis.
    
    - **course_id**: The course ID to regenerate title for
    - **Returns**: New course title
    """
    try:
        course_id = req.get('course_id')
        if not course_id:
            raise HTTPException(status_code=400, detail="course_id is required")
        
        # Find the course
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Check authorization
        if course.author_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="Not authorized to modify this course")
        
        logger.info('[REGENERATE TITLE] Starting for course %s (%s)', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if course.source_doc_name is None else str(course.source_doc_name).replace("\n", "").replace("\r", "")))
        
        # Get all module content for this course
        all_content = ""
        module_titles = []  # Collect module titles to ensure all topics are covered
        paginator = s3_client.get_paginator('list_objects_v2')
        
        try:
            report_pages = paginator.paginate(
                Bucket=S3_BUCKET_NAME, 
                Prefix=f"{course.author_id}/generated_reports/"
            )
            
            # CRITICAL FIX: Parse source_documents with normalization for multi-document courses
            course_docs_normalized = []
            try:
                if course.source_documents:
                    source_docs_list = json.loads(course.source_documents)
                    if isinstance(source_docs_list, list):
                        for doc in source_docs_list:
                            doc_normalized = normalize_document_name(doc)
                            course_docs_normalized.append(doc_normalized)
                        logger.info('[REGENERATE TITLE] Parsed %s documents: %s', len(course_docs_normalized), ("" if course_docs_normalized is None else str(course_docs_normalized).replace("\n", "").replace("\r", "")))
            except Exception as e:
                logger.warning('[REGENERATE TITLE] Could not parse source_documents JSON: %s', e)
            
            # Fallback to single source_doc_name if source_documents is empty
            if not course_docs_normalized:
                course_doc_normalized = normalize_document_name(course.source_doc_name)
                course_docs_normalized = [course_doc_normalized]
                logger.info('[REGENERATE TITLE] Using fallback single source_doc_name: %s', ("" if course_doc_normalized is None else str(course_doc_normalized).replace("\n", "").replace("\r", "")))
            
            module_count = 0
            # IMPROVED: Collect module titles AND content for comprehensive analysis
            for page in report_pages:
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if key.endswith('.json'):
                        try:
                            # Check if this module belongs to this course
                            meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                            meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                            source_doc = meta_data.get("source_doc", "")
                            
                            # Normalize the metadata source_doc for comparison
                            source_doc_normalized = normalize_document_name(source_doc)
                            
                            # CRITICAL FIX: Check if this module matches ANY of the course documents
                            is_match = False
                            for course_doc_norm in course_docs_normalized:
                                if (source_doc_normalized == course_doc_norm or 
                                    course_doc_norm in source_doc_normalized or
                                    source_doc_normalized in course_doc_norm):
                                    is_match = True
                                    break
                            
                            if is_match:
                                # Get module title from metadata
                                module_title = meta_data.get("title", "")
                                if module_title:
                                    module_titles.append(module_title)
                                
                                # Get module content
                                md_key = key.replace('.json', '.md')
                                try:
                                    report_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=md_key)
                                    content = report_obj['Body'].read().decode('utf-8')
                                    # Take first 12000 chars (~2400 words) from each module
                                    all_content += content[:12000] + "\n\n"
                                    module_count += 1
                                except:
                                    pass
                        except:
                            pass
            
            logger.info('[REGENERATE TITLE] Collected %s chars from %s modules', len(all_content), ("" if module_count is None else str(module_count).replace("\n", "").replace("\r", "")))
            logger.info('[REGENERATE TITLE] Module titles: %s', ("" if module_titles is None else str(module_titles).replace("\n", "").replace("\r", "")))
            
            if not all_content:
                raise HTTPException(status_code=400, detail="No module content found for this course")
            
            # Get current title to avoid generating the same one
            current_title = course_display_label(course)
            
            # CRITICAL: Add module titles to content for better topic coverage
            module_titles_text = "\n\nMODULE TITLES:\n" + "\n".join([f"- {title}" for title in module_titles])
            enhanced_content = module_titles_text + "\n\n" + all_content
            
            # Prepare source_docs list for title generation
            source_docs = []
            try:
                if course.source_documents:
                    source_docs_list = json.loads(course.source_documents)
                    if isinstance(source_docs_list, list):
                        source_docs = source_docs_list
            except Exception as e:
                logger.warning('[REGENERATE TITLE] Could not parse source_documents for title generation: %s', e)
            
            # Fallback to single source_doc_name if source_documents is empty
            if not source_docs:
                source_docs = [course.source_doc_name]
            
            logger.info('[REGENERATE TITLE] Using source documents for title generation: %s', ("" if source_docs is None else str(source_docs).replace("\n", "").replace("\r", "")))
            
            # Generate new title with comprehensive content, avoiding the current title
            new_title = generate_course_title_from_content(enhanced_content, source_docs, avoid_title=current_title)
            
            # Update course
            old_title = current_title
            course.course_title = new_title
            db.commit()
            
            logger.info("[REGENERATE TITLE] Updated course title: '%s' -> '%s'", ("" if old_title is None else str(old_title).replace("\n", "").replace("\r", "")), ("" if new_title is None else str(new_title).replace("\n", "").replace("\r", "")))
            
            # Log activity
            log_activity(db, user.id, "COURSE_TITLE_REGENERATED", 
                        f"Regenerated title for course '{course.source_doc_name}': '{new_title}'")
            
            # Send notification to course owner if regenerated by admin
            if user.role == 'super_admin' and course.author_id != user.id:
                notification = Notification(
                    user_id=course.author_id,
                    message=f"Super Admin regenerated your course title from '{old_title}' to '{new_title}'.",
                    course_id=course_notification_id(course),
                )
                db.add(notification)
                db.commit()
                logger.info('[NOTIFICATION] Notified trainer %s about course title regeneration by admin', ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
            
            return {
                "success": True,
                "data": {
                    "old_title": old_title,
                    "new_title": new_title,
                    "course_id": course_id
                }
            }
            
        except BotoCoreError as e:
            logger.error('S3 error while regenerating title: %s', e)
            raise HTTPException(status_code=500, detail="Failed to access course content")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error regenerating course title: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/kb/update-course-title", tags=["Knowledge Base (Trainer)"])
def update_course_title(
    req: dict,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """
    Manually update course title.
    
    - **course_id**: The course ID to update
    - **new_title**: The new title (max 80 characters)
    - **Returns**: Updated course info
    """
    try:
        course_id = req.get('course_id')
        new_title = req.get('new_title', '').strip()
        
        if not course_id:
            raise HTTPException(status_code=400, detail="course_id is required")
        
        if not new_title:
            raise HTTPException(status_code=400, detail="new_title is required")
        
        if len(new_title) > 80:
            raise HTTPException(status_code=400, detail="Title must be 80 characters or less")
        
        if len(new_title) < 3:
            raise HTTPException(status_code=400, detail="Title must be at least 3 characters")
        
        # Find the course
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Check authorization
        if course.author_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="Not authorized to modify this course")
        
        # Update course title
        old_title = course_display_label(course)
        course.course_title = new_title
        db.commit()
        
        logger.info("[MANUAL TITLE UPDATE] Course %s: '%s' -> '%s'", ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if old_title is None else str(old_title).replace("\n", "").replace("\r", "")), ("" if new_title is None else str(new_title).replace("\n", "").replace("\r", "")))
        
        # Log activity
        log_activity(db, user.id, "COURSE_TITLE_UPDATED", 
                    f"Manually updated title for course '{course.source_doc_name}': '{new_title}'")
        
        # Send notification to course owner if updated by admin
        if user.role == 'super_admin' and course.author_id != user.id:
            notification = Notification(
                user_id=course.author_id,
                message=f"Super Admin updated your course title from '{old_title}' to '{new_title}'.",
                course_id=course_notification_id(course),
            )
            db.add(notification)
            db.commit()
            logger.info('[NOTIFICATION] Notified trainer %s about course title update by admin', ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
        
        return {
            "success": True,
            "old_title": old_title,
            "new_title": new_title,
            "course_id": course_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error updating course title: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/workflow/jobs/{job_id}/status", response_model=JobStatusResponse, tags=["Workflow & Jobs"])
def get_job_status(job_id: str, user: User = Depends(get_current_active_user)):
    """
    Polls the status of an asynchronous background job.

    Clients should call this endpoint periodically after initiating a job to get
    progress updates and the final result (e.g., for processing or regeneration).

    - **job_id**: The UUID of the job to check.
    - **Returns**: The current status, progress message, and result/error upon completion.
    - **Raises 404**: If the job ID is not found, or belongs to another user.
    """
    status_info = get_job_status_data(job_id)
    if not status_info:
        raise HTTPException(status_code=404, detail="Job not found")

    # Scope the poll to the job's owner. The result payload carries the generated modules, so
    # without this any authenticated user could read another user's generation by guessing or
    # replaying a job id. 404 rather than 403 so the response does not confirm that an id
    # exists. Mirrors the ownership check in cancel_workflow_job below; super_admin retains
    # cross-user visibility for support. Every writer sets user_id (services/jobs.py and the
    # startup rehydrate in services/auth_platform.py), so a missing owner means a malformed
    # record and is refused rather than treated as public.
    owner_id = status_info.get("user_id")
    if owner_id != user.id and user.role != "super_admin":
        raise HTTPException(status_code=404, detail="Job not found")

    response = JobStatusResponse(
        status=status_info['status'], 
        progress_message=status_info.get('progress_message', 'Processing...'), 
        result=status_info.get('result'), 
        error=status_info.get('error')
    )
    
    # FIXED: Keep completed jobs for a longer time to prevent 404s during frontend polling
    # Only remove after 30 seconds to give frontend time to process the completion
    if response.status in ['completed', 'failed', 'cancelled']:
        # Schedule removal after 30 seconds instead of immediate removal
        import threading
        def delayed_cleanup():
            import time
            time.sleep(30)  # Wait 30 seconds
            job_statuses.pop(job_id, None)
            logger.info('Cleaned up job status for %s after 30 seconds', log_safe(job_id))
        
        # Only start cleanup timer if not already started
        if not status_info.get('cleanup_scheduled', False):
            status_info['cleanup_scheduled'] = True
            cleanup_thread = threading.Thread(target=delayed_cleanup, daemon=True)
            cleanup_thread.start()
    
    return response


@router.post("/workflow/jobs/{job_id}/cancel", status_code=status.HTTP_200_OK, tags=["Workflow & Jobs"])
def cancel_workflow_job(job_id: str, user: User = Depends(trainer_required)):
    """
    Request cancellation of a running module-generation or regeneration job.

    The background worker checks this flag at progress checkpoints and stops
    cooperatively; partial uploads and embeddings are cleaned up on cancel.
    """
    status_info = get_job_status_data(job_id)
    if not status_info:
        raise HTTPException(status_code=404, detail="Job not found")
    owner_id = status_info.get("user_id")
    if owner_id != user.id and user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Not authorized to cancel this job")
    if not request_job_cancellation(job_id, owner_id):
        raise HTTPException(status_code=409, detail="Job cannot be cancelled")
    return {"message": "Job cancelled", "job_id": job_id}

def _course_source_doc_names(course: Course) -> List[str]:
    """Return normalized source document names for a course (single- or multi-doc)."""
    if course.source_documents:
        try:
            raw_docs = json.loads(course.source_documents)
            if isinstance(raw_docs, list):
                return [secure_filename(d) for d in raw_docs if d]
        except (ValueError, TypeError) as exc:
            logger.debug("Could not parse source_documents for a course: %s", exc)
    if course.source_doc_name:
        return [course.source_doc_name]
    return []


def _course_contains_document(course: Course, doc_name: str) -> bool:
    """
    True if doc_name is one of a course's source documents.

    Compares on the normalized name, and on the course's display names too, so a document
    still matches whether the caller passes the randomized storage name (VAPT #4) or the
    original filename recorded by a course generated before storage names were used.
    """
    if not doc_name:
        return False
    target = normalize_document_name(doc_name)
    candidates = set(_course_source_doc_names(course))
    candidates.update(course_document_display_map(course).values())
    if doc_name in candidates:
        return True
    return bool(target) and target in {normalize_document_name(c) for c in candidates}


def _resolve_course_for_commit(
    db: Session, user_id: str, source_docs: List[str]
) -> tuple[Optional[Course], dict[str, str]]:
    """
    Resolve one course and map EVERY committed source document to its course_id.

    A document left out of the map is committed with course_id=None, and a module with no
    course_id cannot be grouped back to its course - it stops appearing in My Modules even
    though the commit reported success. So the last resort below deliberately maps any
    still-unmatched document onto the course the other documents resolved to: a multi-doc
    generation is always ONE course, and dropping the module is strictly worse than
    attaching it to the course it was generated for.
    """
    course_id_map: dict[str, str] = {}
    matched_course: Optional[Course] = None
    normalized_docs = {normalize_document_name(d) for d in source_docs}

    courses = db.query(Course).filter(Course.author_id == user_id).all()
    for course in courses:
        # Match on the course's storage names AND on its display names: courses generated
        # before source_documents held storage names recorded the original filenames there,
        # and those rows must keep committing correctly.
        course_doc_set = {normalize_document_name(d) for d in _course_source_doc_names(course)}
        course_doc_set.update(
            normalize_document_name(d) for d in course_document_display_map(course).values()
        )
        if course_doc_set.intersection(normalized_docs):
            matched_course = course
            for doc_name in source_docs:
                course_id_map[doc_name] = course.id
            break

    if not matched_course:
        for doc_name in source_docs:
            course = db.query(Course).filter(
                Course.author_id == user_id,
                Course.source_doc_name == doc_name,
            ).first()
            if course:
                matched_course = course
                course_id_map[doc_name] = course.id

    if matched_course:
        for doc_name in source_docs:
            if doc_name not in course_id_map:
                course_id_map[doc_name] = matched_course.id
                logger.warning(
                    "[COMMIT] Document '%s' did not match course '%s' directly; "
                    "attaching to it so the module is not orphaned",
                    log_safe(doc_name),
                    log_safe(matched_course.id),
                )

    return matched_course, course_id_map


@router.post("/workflow/commit", response_model=CommitResponse, tags=["Workflow & Jobs"])
def commit_job(reports: List[ReportModule], user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Commits reviewed and edited modules to the knowledge base.

    This is the final step of the curation workflow. It saves the final
    module content to S3 and triggers re-indexing in Pinecone.
    
    **CRITICAL**: For regeneration operations, this endpoint ONLY deletes and replaces
    the modules that were explicitly regenerated. Modules that were skipped are preserved.

    - **Required Role**: Trainer or Super Admin.
    - **Body**: A list of `ReportModule` objects with the final content.
    - **Returns**: A confirmation message and the number of indexed chunks.
    """
    try:
        if not reports:
            raise HTTPException(status_code=400, detail="No modules to commit.")

        source_docs_to_update = sorted(list(set(r.doc_name for r in reports)))
        
        logger.info("[COMMIT] ===== COMMIT STARTED =====")
        logger.info('[COMMIT] User: %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")))
        logger.info('[COMMIT] Total reports to commit: %s', len(reports))
        logger.info('[COMMIT] Unique source documents: %s', len(source_docs_to_update))
        logger.info('[COMMIT] Documents to index: %s', ("" if source_docs_to_update is None else str(source_docs_to_update).replace("\n", "").replace("\r", "")))
        
        # Log module count per document
        for doc_name in source_docs_to_update:
            module_count = sum(1 for r in reports if r.doc_name == doc_name)
            logger.info("[COMMIT]   - '%s': %s module(s)", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if module_count is None else str(module_count).replace("\n", "").replace("\r", "")))
        
        matched_course, course_id_map = _resolve_course_for_commit(db, user.id, source_docs_to_update)
        if matched_course:
            logger.info(
                "[COMMIT] Resolved course '%s' for %s document(s)",
                ("" if matched_course.id is None else str(matched_course.id).replace("\n", "").replace("\r", "")),
                len(course_id_map),
            )

            # The curation payload is the last point that still knows both the randomized storage
            # name (VAPT #4) and the filename the trainer uploaded. Recording any name the course
            # is missing is what stops KB search, View Docs and My Modules from later falling back
            # to "Uploaded PPTX" for a course whose display names were never written.
            uploaded_names = {
                r.doc_name: r.doc_display_name
                for r in reports
                if r.doc_name and getattr(r, "doc_display_name", None)
            }
            if record_course_document_display_names(matched_course, uploaded_names):
                db.commit()
                logger.info(
                    "[COMMIT] Backfilled uploaded filenames for course %s",
                    log_safe(matched_course.id),
                )
        for doc_name in source_docs_to_update:
            if doc_name not in course_id_map:
                logger.error(
                    "[COMMIT] No course found for document '%s' - module metadata will lack course_id",
                    ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")),
                )
        
        logger.info('[COMMIT] Course ID map: %s', ("" if course_id_map is None else str(course_id_map).replace("\n", "").replace("\r", "")))
        
        # Collect all seg_indices that need to be deleted (may include combined/merged modules)
        all_deleted_seg_indices: Optional[List[int]] = None
        for r in reports:
            dsi = getattr(r, 'deleted_seg_indices', None)
            if dsi:
                all_deleted_seg_indices = dsi
                logger.info('[COMMIT] Found deleted_seg_indices from regeneration: %s', ("" if dsi is None else str(dsi).replace("\n", "").replace("\r", "")))
                break
        
        for doc_name in source_docs_to_update:
            course_id = course_id_map.get(doc_name)
            if course_id:
                if all_deleted_seg_indices is not None:
                    seg_indices_to_delete = set(all_deleted_seg_indices)
                    logger.info("[COMMIT] For document '%s': deleting original regenerated modules with seg_idx: %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if sorted(seg_indices_to_delete) is None else str(sorted(seg_indices_to_delete)).replace("\n", "").replace("\r", "")))
                else:
                    seg_indices_to_delete = set(r.seg_idx for r in reports if r.doc_name == doc_name)
                    logger.info("[COMMIT] For document '%s': replacing modules with seg_idx: %s", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if sorted(seg_indices_to_delete) is None else str(sorted(seg_indices_to_delete)).replace("\n", "").replace("\r", "")))
                
                _delete_specific_modules_for_course(user.id, doc_name, course_id, seg_indices_to_delete)
            else:
                logger.warning("[COMMIT] No course_id found for document '%s', skipping deletion", ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
        
        for r in reports:
            final_content = _prepare_module_content_for_commit(r.edited_content, user.id, r.doc_name)
            
            report_key = f"{user.id}/generated_reports/{r.report_id}.md"
            meta_key = f"{user.id}/generated_reports/{r.report_id}.json"
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=report_key,
                Body=final_content.encode("utf-8", errors="replace"),
                ServerSideEncryption="AES256",
            )
            
            course_id = course_id_map.get(r.doc_name)
            meta_data = {
                "source_doc": r.doc_name,
                "seg_index": r.seg_idx,
                "title": r.title,
                "course_id": course_id,
                "document_order": getattr(r, 'document_order', 0),
            }
            
            logger.info("[COMMIT] Saving module %s: source_doc='%s', course_id='%s', document_order=%s, title='%s'", ("" if r.report_id is None else str(r.report_id).replace("\n", "").replace("\r", "")), ("" if r.doc_name is None else str(r.doc_name).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if getattr(r, 'document_order', 0) is None else str(getattr(r, 'document_order', 0)).replace("\n", "").replace("\r", "")), ("" if r.title is None else str(r.title).replace("\n", "").replace("\r", "")))
            
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=meta_key,
                Body=json.dumps(meta_data, indent=2).encode("utf-8"),
                ServerSideEncryption="AES256",
            )
        
        logger.info("[COMMIT] ===== STARTING INDEXING =====")
        logger.info('[COMMIT] Will index %s document(s) to Pinecone', len(source_docs_to_update))
        
        total_chunks_added = 0
        for idx, doc_name in enumerate(source_docs_to_update, 1):
            logger.info("[COMMIT INDEXING] (%s/%s) Indexing document: '%s'", ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), len(source_docs_to_update), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
            chunks_added = _reindex_source_document(user.id, doc_name)
            total_chunks_added += chunks_added
            logger.info("[COMMIT INDEXING] (%s/%s) Indexed '%s': %s chunks added", ("" if idx is None else str(idx).replace("\n", "").replace("\r", "")), len(source_docs_to_update), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if chunks_added is None else str(chunks_added).replace("\n", "").replace("\r", "")))
            log_activity(db, user.id, "COMMIT_MODULES", f"Committed modules for document: {doc_name}")
        
        logger.info("[COMMIT] ===== INDEXING COMPLETE =====")
        logger.info('[COMMIT] Total chunks indexed across all documents: %s', ("" if total_chunks_added is None else str(total_chunks_added).replace("\n", "").replace("\r", "")))
        
        return CommitResponse(
            message=f"Successfully committed and indexed {len(source_docs_to_update)} source(s).",
            indexed_chunk_count=total_chunks_added,
            source_documents_added=source_docs_to_update,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[COMMIT] Commit failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

# --- Knowledge Base (Trainer) Endpoints ---
@router.get("/kb/reports", tags=["Knowledge Base (Trainer)"])
def list_reports(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Lists all generated reports grouped by source document for the authenticated trainer.

    This endpoint is used to populate the "My Modules" view, showing a hierarchical list
    of all content curated by the trainer.

    - **Required Role**: Trainer or Super Admin.
    """
    try:
        return {"grouped_reports": _get_grouped_reports(user.id, db, requesting_user_id=user.id)}
    except Exception as e:
        logger.error('Error loading reports for user %s: %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/kb/reports-content/{doc_name:path}", tags=["Knowledge Base (Trainer)"])
def get_report_contents_for_doc(
    doc_name: str, 
    author_id: Optional[str] = None, 
    user: User = Depends(get_current_active_user), 
    db: Session = Depends(get_db)
):
    """
    Retrieves the full content of all modules for a specific document.

    Can be used by any authenticated user to view content they have access to
    (e.g., their own, or content from a course they are assigned).

    - **doc_name**: The filename of the source document.
    - **author_id**: (Optional) The ID of the content owner. Defaults to the current user.
    - **Raises 404**: If the document or its reports are not found.
    """
    auth_id = author_id or user.id
    if ".." in doc_name: raise HTTPException(status_code=400, detail="Invalid document name")
    
    # FIXED: Enhanced access control for different user roles
    # Check if user has permission to access this content
    if auth_id != user.id:
        # Cross-user access - check permissions
        if user.role == 'super_admin':
            # Super admins can access any content
            logger.info('[ACCESS] Super admin %s accessing content from %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if auth_id is None else str(auth_id).replace("\n", "").replace("\r", "")))
        elif user.role == 'trainee':
            # Trainees can only access content from PUBLISHED courses they're enrolled in
            course = db.query(Course).filter(
                Course.author_id == auth_id, 
                Course.source_doc_name == doc_name
            ).first()
            
            if not course:
                raise HTTPException(status_code=404, detail="Course not found.")
            
            # CRITICAL: Check if course is published
            if not course.is_live:
                logger.warning('[ACCESS] Trainee %s attempted to access unpublished course %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")))
                raise HTTPException(status_code=403, detail="This course is not yet published. Please contact your trainer.")
            
            # Check if trainee is enrolled in this course
            enrollment = db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id == user.id,
                TraineeAttempt.course_id == course.id
            ).first()
            
            if not enrollment:
                raise HTTPException(status_code=403, detail="You don't have access to this course.")
            
            logger.info('[ACCESS] Trainee %s accessing enrolled published course %s from %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")), ("" if auth_id is None else str(auth_id).replace("\n", "").replace("\r", "")))
        elif user.role == 'trainer':
            # Trainers can access content shared with them (both modules and complete courses)
            # Check if this document has been shared with the current trainer
            shared_report = db.query(SharedReport).filter(
                SharedReport.sharer_id == auth_id,
                SharedReport.recipient_id == user.id,
                SharedReport.source_doc_name == doc_name
            ).first()
            
            # Also check if shared as a complete course
            shared_course = db.query(SharedCourse).join(Course).filter(
                SharedCourse.sharer_id == auth_id,
                SharedCourse.recipient_id == user.id,
                Course.source_doc_name == doc_name
            ).first()
            
            if not shared_report and not shared_course:
                logger.warning('[ACCESS] Trainer %s attempted to access unshared content from %s for document %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if auth_id is None else str(auth_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))
                raise HTTPException(status_code=403, detail="Access denied. This content has not been shared with you.")
            
            logger.info('[ACCESS] Trainer %s accessing shared content from %s for document %s (shared_report=%s, shared_course=%s)', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if auth_id is None else str(auth_id).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if bool(shared_report) is None else str(bool(shared_report)).replace("\n", "").replace("\r", "")), ("" if bool(shared_course) is None else str(bool(shared_course)).replace("\n", "").replace("\r", "")))
        else:
            raise HTTPException(status_code=403, detail="Access denied.")
    
    # PERFORMANCE FIX: Skip image validation when loading module list to prevent timeouts
    # Images will be validated on-demand when modules are actually opened
    all_reports = _get_grouped_reports(auth_id, db, include_content=True, requesting_user_id=user.id, validate_images=False)
    target_group = next((g for g in all_reports if g['source_doc'] == doc_name), None)
    if not target_group: raise HTTPException(status_code=404, detail="Reports for this document not found.")
    
    # Return reports with course_title for display in tutor interface
    return {
        "reports": target_group['reports'],
        # Never fall back to doc_name: that is the randomized storage name (VAPT #4).
        "course_title": target_group.get('course_title') or friendly_document_name(doc_name),
        "source_doc": doc_name,
        "course_id": target_group.get('course_id'),
        "is_published": target_group.get('is_published', False),
    }

@router.get("/kb/reports/{filename:path}", tags=["Knowledge Base (Trainer)"])
def get_managed_report(
    filename: str,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db),
):
    """
    Retrieves the raw Markdown content of a single report file owned by the trainer,
    along with metadata including the source document name.

    - **Required Role**: Trainer or Super Admin.
    - **filename**: The UUID-based filename of the report (e.g., `... .md`).
    - **Raises 404**: If the report is not found.
    """
    if ".." in filename: raise HTTPException(status_code=400, detail="Invalid filename")
    report_key = f"{user.id}/generated_reports/{filename}"
    meta_key = report_key.replace('.md', '.json')
    
    try:
        # Get content
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=report_key)
        content = obj['Body'].read().decode('utf-8')
        
        # Get metadata (source_doc)
        source_doc = None
        try:
            meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=meta_key)
            meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
            source_doc = meta_data.get('source_doc', None)
        except ClientError:
            # Metadata file might not exist for older modules
            pass

        # source_doc is the randomized storage name (VAPT #4) and stays the join key;
        # source_doc_display_name is what the Edit Module dialog shows the user.
        return {
            "content": content,
            "source_doc": source_doc,
            "source_doc_display_name": display_name_for_stored_document(
                db, user.id, source_doc
            ),
        }
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Report not found")

@router.get("/api/modules/{module_id}/images", tags=["Knowledge Base (Trainer)"])
def get_module_images(
    request: Request,
    module_id: str,
    # The standard gate, like every other route: get_current_user alone skipped the approval and
    # first-login (must_change_password) checks, so an account still on its starting login could
    # fetch module images.
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    owner_id: Optional[str] = Query(None, description="ID of the module owner (for admin access to other trainers' modules)")
):
    """
    Get all images for a specific module with download to temp folder.
    
    - **module_id**: The filename of the module (without .md extension)
    - **owner_id**: Optional - ID of the module owner (for admin access to other trainers' modules)
    - **Returns**: Module info with image count and local paths
    """
    try:
        logger.info("[MODULE IMAGES] === REQUEST START ===")
        logger.info('[MODULE IMAGES] Request URL: %s', ("" if request.url is None else str(request.url).replace("\n", "").replace("\r", "")))
        logger.info('[MODULE IMAGES] Query params: %s', ("" if dict(request.query_params) is None else str(dict(request.query_params)).replace("\n", "").replace("\r", "")))
        logger.info('[MODULE IMAGES] Module ID: %s', ("" if module_id is None else str(module_id).replace("\n", "").replace("\r", "")))
        logger.info('[MODULE IMAGES] Requesting User: %s (%s)', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if user.role is None else str(user.role).replace("\n", "").replace("\r", "")))
        logger.info('[MODULE IMAGES] Owner ID parameter: %s', ("" if owner_id is None else str(owner_id).replace("\n", "").replace("\r", "")))
        logger.info('[MODULE IMAGES] Owner ID type: %s', ("" if type(owner_id) is None else str(type(owner_id)).replace("\n", "").replace("\r", "")))
        logger.info('[MODULE IMAGES] Owner ID repr: %s', ("" if repr(owner_id) is None else str(repr(owner_id)).replace("\n", "").replace("\r", "")))
        
        # Determine which user's modules to access
        target_user_id = user.id  # Default to current user
        
        # If owner_id is provided, handle cross-user access
        if owner_id:
            if owner_id == user.id:
                logger.info("[MODULE IMAGES] ✓ User accessing their own modules")
            else:
                # Cross-user access - check permissions
                logger.info('[MODULE IMAGES] Cross-user access requested: %s user %s -> %s', ("" if user.role is None else str(user.role).replace("\n", "").replace("\r", "")), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if owner_id is None else str(owner_id).replace("\n", "").replace("\r", "")))
                
                # For now, allow any authenticated user to access other users' modules for debugging
                # TODO: Restrict this to admin roles only in production
                logger.info("[MODULE IMAGES] ✓ Allowing cross-user access for debugging")
                target_user_id = owner_id
                
                # Uncomment this for production security:
                # if user.role not in ['super_admin', 'trainer']:
                #     logger.warning(f"[MODULE IMAGES] ✗ User {user.id} ({user.role}) denied access to user {owner_id}'s modules")
                #     raise HTTPException(status_code=403, detail="You don't have permission to access this user's modules")
        else:
            logger.info('[MODULE IMAGES] ✓ No owner_id provided, using current user %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")))
        
        # Construct the S3 key for the report
        filename = f"{module_id}.md"
        report_key = f"{target_user_id}/generated_reports/{filename}"
        meta_key = f"{target_user_id}/generated_reports/{module_id}.json"
        
        logger.info('[MODULE IMAGES] Target user ID: %s', ("" if target_user_id is None else str(target_user_id).replace("\n", "").replace("\r", "")))
        logger.info('[MODULE IMAGES] S3 keys: report=%s, meta=%s', ("" if report_key is None else str(report_key).replace("\n", "").replace("\r", "")), ("" if meta_key is None else str(meta_key).replace("\n", "").replace("\r", "")))
        
        # Debug: Check what files actually exist for this user
        try:
            prefix = f"{target_user_id}/generated_reports/"
            logger.info('[MODULE IMAGES] Checking S3 prefix: %s', ("" if prefix is None else str(prefix).replace("\n", "").replace("\r", "")))
            
            paginator = s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix, MaxKeys=10)
            
            existing_files = []
            for page in pages:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        existing_files.append(obj['Key'])
            
            logger.info('[MODULE IMAGES] Found %s files in S3 for user %s', len(existing_files), ("" if target_user_id is None else str(target_user_id).replace("\n", "").replace("\r", "")))
            if existing_files:
                logger.info('[MODULE IMAGES] Sample files: %s', ("" if existing_files[:5] is None else str(existing_files[:5]).replace("\n", "").replace("\r", "")))
            else:
                logger.warning('[MODULE IMAGES] No files found for user %s in S3', ("" if target_user_id is None else str(target_user_id).replace("\n", "").replace("\r", "")))
                
        except Exception as debug_e:
            logger.error('[MODULE IMAGES] Error checking S3 contents: %s', ("" if debug_e is None else str(debug_e).replace("\n", "").replace("\r", "")))
        
        # Get report content from S3
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=report_key)
            content = obj['Body'].read().decode('utf-8')
            
            # Get metadata for title
            meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=meta_key)
            meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
            title = meta_data.get('title', 'Module')
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                raise HTTPException(status_code=404, detail="Module not found")
            raise
        
        # Extract image paths from content
        image_paths = extract_image_paths_from_module(content)
        
        # Download to temp and return local paths
        local_images = []
        for s3_key in image_paths:
            local_path = download_image_to_temp(s3_key, module_id)
            if local_path:
                filename = Path(local_path).name
                local_images.append({
                    "s3_key": s3_key,
                    "filename": filename,
                    "url": f"/api/images/temp/{filename}"
                })
        
        return {
            "module_id": module_id,
            "module_title": title,
            "image_count": len(local_images),
            "images": local_images
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error getting module images: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get module images")

@router.get("/api/images/temp/{filename}", tags=["Knowledge Base (Trainer)"])
def serve_temp_image(filename: str):
    """
    Serve image from temp directory.
    
    - **filename**: The filename of the temp image
    - **Returns**: Image file
    """
    from utils.security_helpers import file_response_under_base, media_type_for_image_name

    try:
        return file_response_under_base(
            TEMP_IMAGE_DIR,
            filename,
            media_type_for_image_name(filename),
            {"Cache-Control": "public, max-age=3600"},
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Image not found")

@router.get("/api/images/s3/{s3_key:path}", tags=["Knowledge Base (Trainer)"])
def serve_s3_image(s3_key: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Serve image directly from S3.
    
    - **s3_key**: The S3 key of the image (URL encoded)
    - **Returns**: Image file streamed from S3
    
    Permission logic:
    - Trainers/Admins: Can access their own images
    - Trainees: Can access images from courses they're enrolled in
    """
    try:
        # Extract owner ID from S3 key (format: user_id/extracted_images/... or user_id/generated_reports/...)
        owner_id = s3_key.split('/')[0] if '/' in s3_key else None
        
        logger.info('[IMAGE ACCESS] User %s (%s) requesting image: %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if user.role is None else str(user.role).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
        logger.info('[IMAGE ACCESS] Extracted owner_id: %s', ("" if owner_id is None else str(owner_id).replace("\n", "").replace("\r", "")))
        
        # Permission check
        if user.role in ['trainer', 'super_admin']:
            # Trainers and admins can access any images
            logger.info('[IMAGE ACCESS] ✓ Trainer/Admin %s has full access', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")))
        elif user.role == 'trainee':
            # Trainees can access their own images or images from enrolled PUBLISHED courses
            if owner_id == user.id:
                logger.info('[IMAGE ACCESS] ✓ Trainee %s accessing own image', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")))
            elif owner_id:
                # CRITICAL FIX: Check if trainee is enrolled in any PUBLISHED course from this owner
                logger.info('[IMAGE ACCESS] Checking enrollment for trainee %s in published courses by %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if owner_id is None else str(owner_id).replace("\n", "").replace("\r", "")))
                
                # Get only PUBLISHED course enrollments for this trainee
                enrollments = db.query(TraineeAttempt).join(Course).filter(
                    TraineeAttempt.trainee_id == user.id,
                    Course.is_live == True  # Only published courses
                ).all()
                
                logger.info('[IMAGE ACCESS] Found %s published course enrollments for trainee %s', len(enrollments), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")))
                
                # Check if any published enrollment matches the owner
                enrollment_found = False
                for enroll in enrollments:
                    logger.info('[IMAGE ACCESS] Checking published enrollment: course_id=%s, author_id=%s', ("" if enroll.course_id is None else str(enroll.course_id).replace("\n", "").replace("\r", "")), ("" if enroll.course.author_id is None else str(enroll.course.author_id).replace("\n", "").replace("\r", "")))
                    if enroll.course.author_id == owner_id:
                        enrollment_found = True
                        logger.info("[IMAGE ACCESS] ✓ Match found! Trainee enrolled in course '%s' by %s", ("" if enroll.course.source_doc_name is None else str(enroll.course.source_doc_name).replace("\n", "").replace("\r", "")), ("" if owner_id is None else str(owner_id).replace("\n", "").replace("\r", "")))
                        break
                
                if not enrollment_found:
                    logger.warning('[IMAGE ACCESS] ✗ Trainee %s not enrolled in any course by %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if owner_id is None else str(owner_id).replace("\n", "").replace("\r", "")))
                    logger.warning('[IMAGE ACCESS] ✗ Available author_ids: %s', ("" if [e.course.author_id for e in enrollments] is None else str([e.course.author_id for e in enrollments]).replace("\n", "").replace("\r", "")))
                    raise HTTPException(status_code=403, detail="You don't have permission to access this image")
            else:
                logger.warning('[IMAGE ACCESS] ✗ Could not extract owner_id from s3_key: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
                raise HTTPException(status_code=403, detail="Invalid image path")
        
        # Get image from S3
        logger.info("[IMAGE ACCESS] Fetching from S3 bucket '%s' key '%s'", ("" if S3_BUCKET_NAME is None else str(S3_BUCKET_NAME).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        image_bytes = obj['Body'].read()
        logger.info('[IMAGE ACCESS] ✓ Successfully fetched %s bytes from S3', len(image_bytes))
        
        # Determine media type from extension
        ext = Path(s3_key).suffix.lower()
        media_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp',
            '.tiff': 'image/tiff',
            '.svg': 'image/svg+xml'
        }
        media_type = media_types.get(ext, 'image/png')
        
        # Validate image size
        if len(image_bytes) < 100:
            logger.error('[IMAGE ACCESS] Image too small (%s bytes): %s', len(image_bytes), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            raise HTTPException(status_code=404, detail="Image file is corrupted or empty")
        
        return Response(
            content=image_bytes,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"}
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            logger.error('[IMAGE ACCESS] ✗ Image not found in S3: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            logger.error('[IMAGE ACCESS] Bucket: %s, Key: %s', ("" if S3_BUCKET_NAME is None else str(S3_BUCKET_NAME).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))
            
            # DEBUGGING: List what images actually exist for this user/document
            try:
                parts = s3_key.split('/')
                if len(parts) >= 3 and parts[1] == 'extracted_images':
                    user_id_from_key = parts[0]
                    doc_name_from_key = parts[2]
                    logger.error('[IMAGE ACCESS DEBUG] Listing actual images for user %s, doc %s', ("" if user_id_from_key is None else str(user_id_from_key).replace("\n", "").replace("\r", "")), ("" if doc_name_from_key is None else str(doc_name_from_key).replace("\n", "").replace("\r", "")))
                    
                    # List what's actually in S3
                    paginator = s3_client.get_paginator('list_objects_v2')
                    prefix = f"{user_id_from_key}/extracted_images/{doc_name_from_key}/"
                    pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix)
                    
                    actual_images = []
                    for page in pages:
                        for obj in page.get('Contents', []):
                            if obj['Key'].lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                                actual_images.append(obj['Key'])
                    
                    logger.error('[IMAGE ACCESS DEBUG] Found %s actual images in S3:', len(actual_images))
                    for img in actual_images:
                        logger.error('[IMAGE ACCESS DEBUG]   - %s', ("" if img is None else str(img).replace("\n", "").replace("\r", "")))
                    
                    if not actual_images:
                        logger.error('[IMAGE ACCESS DEBUG] No images found in S3 prefix: %s', ("" if prefix is None else str(prefix).replace("\n", "").replace("\r", "")))
                    
            except Exception as debug_e:
                logger.error('[IMAGE ACCESS DEBUG] Failed to list actual images: %s', ("" if debug_e is None else str(debug_e).replace("\n", "").replace("\r", "")))
            
            raise HTTPException(status_code=404, detail=f"Image not found in S3: {s3_key}")
        logger.error('[IMAGE ACCESS] ✗ S3 error for %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e)
        raise HTTPException(status_code=500, detail="Failed to retrieve image")
    except HTTPException:
        raise
    except Exception as e:
        logger.error('[IMAGE ACCESS] ✗ Unexpected error serving %s: %s', ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@router.post("/kb/reports/{filename:path}/title/update", tags=["Knowledge Base (Trainer)"])
def update_module_title(filename: str, req: ModuleTitleUpdateRequest, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Updates the title of a module.

    - **Required Role**: Trainer or Super Admin.
    - **filename**: The UUID-based filename of the report.
    - **Body**: JSON with `new_title` field.
    """
    if ".." in filename: raise HTTPException(status_code=400, detail="Invalid filename")
    
    new_title = req.new_title.strip()
    if not new_title: raise HTTPException(status_code=400, detail="Title cannot be empty")
    
    report_key = f"{user.id}/generated_reports/{filename}"
    meta_key = report_key.replace('.md', '.json')
    
    try:
        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=meta_key)
        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
        
        meta_data['title'] = new_title
        
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=meta_key,
            Body=json.dumps(meta_data).encode('utf-8'),
            ServerSideEncryption='AES256'
        )
        
        log_activity(db, user.id, "UPDATE_MODULE_TITLE", f"Updated module title to '{new_title}'")
        
        return {"success": True, "message": "Module title updated successfully"}
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Report metadata not found")
        raise
    except Exception as e:
        logger.error('Error updating module title %s for user %s: %s', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal server error occurred")

@router.post("/kb/reports/{filename:path}/update", tags=["Knowledge Base (Trainer)"])
def update_managed_report(
    filename: str,
    req: ReportUpdateRequest,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """
    Updates the content of a single report file and triggers re-indexing.

    - **Required Role**: Trainer or Super Admin.
    - **filename**: The UUID-based filename of the report to update.
    - **Body**: A JSON object with the `new_content`.
    - **Raises 404**: If the report's metadata is not found.
    """
    if ".." in filename: raise HTTPException(status_code=400, detail="Invalid filename")
    report_key = f"{user.id}/generated_reports/{filename}"
    meta_key = report_key.replace('.md', '.json')
    try:
        s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=report_key, Body=req.new_content.encode('utf-8'), ServerSideEncryption='AES256')
        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=meta_key)
        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
        original_doc_name = meta_data.get('source_doc')
        if original_doc_name:
            log_activity(db, user.id, "UPDATE_MODULE", f"Updated module '{filename}' from '{original_doc_name}'")
            chunk_count = _reindex_source_document(user.id, original_doc_name)
            return {"message": "Report updated and re-indexed.", "reindexed_chunks": chunk_count}
        else:
            return {"message": "Report updated but could not re-index."}
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Report metadata not found")
        raise
    except Exception as e:
        logger.error('Error updating report %s for user %s: %s', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal server error occurred")

@router.post("/kb/reports/{filename:path}/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Knowledge Base (Trainer)"])
def delete_managed_report(filename: str, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Deletes a single report file and its metadata, then triggers re-indexing.

    - **Required Role**: Trainer or Super Admin.
    - **filename**: The UUID-based filename of the report to delete.
    - **Raises 404**: If the report is not found.
    """
    if ".." in filename: raise HTTPException(status_code=400, detail="Invalid filename")
    report_key = f"{user.id}/generated_reports/{filename}"
    meta_key = report_key.replace('.md', '.json')
    try:
        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=meta_key)
        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
        original_doc_name = meta_data.get('source_doc')
        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=report_key)
        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=meta_key)
        if original_doc_name:
            log_activity(db, user.id, "DELETE_MODULE", f"Deleted module '{filename}' from '{original_doc_name}'")
            _reindex_source_document(user.id, original_doc_name)
        return Response(status_code=204)
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Report not found")
        raise
    except Exception as e:
        logger.error('Error deleting report %s for user %s: %s', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal server error occurred")

@router.post("/kb/source-document/{doc_name:path}/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Knowledge Base (Trainer)"])
def delete_source_document(doc_name: str, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Deletes a source document and all associated data (reports, course, vectors, shared reports).

    This is a destructive operation. If a course was published from this document,
    it will be deleted, and trainees will no longer see it.
    
    Also deletes all shared report records, so trainers who received this document
    will no longer see it in their shared reports.

    - **Required Role**: Trainer or Super Admin.
    - **doc_name**: The filename of the source document to delete.
    """
    # Resolve the label BEFORE the delete: the display-name mapping lives on the Course
    # row that is about to go away, and a storage key means nothing to a recipient.
    document_label = display_name_for_stored_document(db, user.id, doc_name) or doc_name
    try:
        delete_source_document_completely(
            owner_user_id=user.id,
            doc_name=doc_name,
            db=db,
            shared_report_notify_message=(
                f"The document '{log_safe(document_label)}' shared by {log_safe(user.email)} "
                "has been deleted by the owner."
            ),
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document name")

    log_activity(
        db,
        user.id,
        "DELETE_SOURCE_DOCUMENT",
        f"Deleted source document '{log_safe(doc_name)}' and all associated data.",
    )
    logger.info(
        "User %s deleted source document '%s' and all associated data.",
        log_safe(user.email),
        log_safe(doc_name),
    )
    return Response(status_code=204)

@router.get("/kb/view-pdf/{doc_name:path}", tags=["Knowledge Base (Trainer)"])
def view_uploaded_pdf(doc_name: str, author_id: Optional[str] = None, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Serves the original uploaded document file for viewing in the browser.

    - **doc_name**: The filename of the source document.
    - **author_id**: (Optional) The ID of the content owner.
    - **Raises 404**: If the document is not found.
    """
    auth_id = author_id or user.id
    if ".." in doc_name: raise HTTPException(status_code=400, detail="Invalid document name.")

    owning_course = None
    # Access control: Allow super admins to view any document, trainers can only view their own
    if auth_id != user.id and user.role != 'super_admin':
        # Check if this is a shared document or if user has access
        if user.role == 'trainee':
            # Trainees can view documents from courses they're enrolled in.
            # Look through every source document of the author's courses, not just
            # source_doc_name: a multi-document course records only its FIRST document
            # there, so matching on that alone denied access to documents 2..N.
            course = None
            for candidate in db.query(Course).filter(Course.author_id == auth_id).all():
                if _course_contains_document(candidate, doc_name):
                    course = candidate
                    break

            if not course or not course.is_live:
                raise HTTPException(status_code=403, detail="Access denied")
            owning_course = course

            # Check enrollment
            enrollment = db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id == user.id,
                TraineeAttempt.course_id == course.id
            ).first()
            
            if not enrollment:
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            # Trainers can only view their own documents
            raise HTTPException(status_code=403, detail="Access denied")
    
    s3_key = f"{auth_id}/uploaded_docs/{doc_name}"
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        # The S3 key must stay the randomized storage name (VAPT #4), but the browser should
        # save/label the file as what the user uploaded.
        if owning_course is None:
            owning_course = next(
                (
                    c
                    for c in db.query(Course).filter(Course.author_id == auth_id).all()
                    if _course_contains_document(c, doc_name)
                ),
                None,
            )
        display_map = (
            course_document_display_map(owning_course) if owning_course is not None else {}
        )
        safe_name = os.path.basename(friendly_document_name(doc_name, display_map))
        media = obj.get("ContentType") or "application/octet-stream"
        return Response(
            content=obj["Body"].read(),
            media_type=media,
            headers={"Content-Disposition": safe_content_disposition(safe_name, disposition="inline")},
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Document not found.")
        raise

@router.get("/kb/course-documents/{course_id}", tags=["Knowledge Base (Trainer)"])
def get_course_documents(course_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves all source documents associated with a course.
    Returns list of document names and their download URLs.
    """
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Check access permissions
    if course.author_id != user.id and user.role != 'super_admin':
        # Check if trainee has access to this course
        if user.role == 'trainee':
            enrollment = db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id == user.id,
                TraineeAttempt.course_id == course.id
            ).first()
            if not enrollment or not course.is_live:
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            raise HTTPException(status_code=403, detail="Access denied")
    
    # Parse source documents. These are STORAGE names (randomized "<uuid>.<ext>", VAPT #4)
    # and are what the S3 lookup below must use; the user-facing name comes from the
    # display map so the modal never shows a bare UUID.
    source_docs = []
    if course.source_documents:
        try:
            source_docs = json.loads(course.source_documents)
        except (ValueError, TypeError):
            source_docs = [course.source_doc_name]
    else:
        source_docs = [course.source_doc_name]

    display_map = course_document_display_map(course)

    # Build document list with metadata
    documents = []
    seen_filenames = {}  # Track how many times we've seen each display name

    for doc_name in source_docs:
        friendly = friendly_document_name(doc_name, display_map)
        # Track duplicate names so two uploads of the same file stay distinguishable
        if friendly in seen_filenames:
            seen_filenames[friendly] += 1
            display_name = f"{friendly} (copy {seen_filenames[friendly]})"
        else:
            seen_filenames[friendly] = 1
            display_name = friendly

        s3_key = f"{course.author_id}/uploaded_docs/{doc_name}"
        # File type from the STORAGE name: its extension is the validated one, whereas a
        # display name is user-controlled text.
        file_ext = doc_name.lower().rsplit('.', 1)[-1] if '.' in doc_name else 'unknown'
        file_type = 'PDF' if file_ext == 'pdf' else 'Image' if file_ext in ['png', 'jpg', 'jpeg'] else file_ext.upper()
        try:
            # Check if document exists in S3
            s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)

            documents.append({
                'filename': display_name,
                'original_filename': doc_name,
                'file_type': file_type,
                'download_url': f"/kb/view-pdf/{doc_name}?author_id={course.author_id}"
            })
        except ClientError:
            # Even if document doesn't exist in S3, still show it in the list
            logger.warning('Document %s not found in S3 for course %s, but including in list', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
            documents.append({
                'filename': f"{display_name} (not found)",
                'original_filename': doc_name,
                'file_type': file_type,
                'download_url': None
            })

    return {
        'success': True,
        'course_id': course_id,
        # Fall back to the DISPLAY name: source_doc_name is a randomized storage name
        # (VAPT #4) and would title the modal with a bare UUID.
        'course_title': course.course_title or friendly_document_name(
            course.source_doc_name, display_map
        ),
        'documents': documents
    }

@router.post("/kb/clear-user-data/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Knowledge Base (Trainer)"])
def clear_user_data(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Deletes all data (docs, reports, vectors, courses) for the authenticated trainer.

    This is a highly destructive operation and is protected.

    - **Required Role**: Trainer or Super Admin.
    """
    logger.critical("CRITICAL: User %s is clearing all their document data.", user.email)
    try: 
        clear_all_data_for_user(user.id)
        
        # Delete in correct order to avoid foreign key constraint violations
        # 1. Get all courses by this author
        courses = db.query(Course).filter(Course.author_id == user.id).all()
        course_ids = [c.id for c in courses]
        
        if course_ids:
            # 2. Delete trainee_attempts first (child records)
            db.query(TraineeAttempt).filter(TraineeAttempt.course_id.in_(course_ids)).delete(synchronize_session=False)
            
            # 3. Delete course_progress records
            db.query(CourseProgress).filter(CourseProgress.course_id.in_(course_ids)).delete(synchronize_session=False)
            
            # 4. Delete token_usage records
            db.query(TokenUsage).filter(TokenUsage.course_id.in_(course_ids)).delete(synchronize_session=False)
            
            # 5. Delete tutor_response_cache records (both database and S3)
            cache_entries = db.query(TutorResponseCache).filter(TutorResponseCache.course_id.in_(course_ids)).all()
            for cache_entry in cache_entries:
                try:
                    # Delete cached response from S3
                    s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
                    logger.info('[CLEAR DATA CACHE CLEANUP] Deleted S3 cache: %s', ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")))
                except Exception as e:
                    logger.warning('[CLEAR DATA CACHE CLEANUP] Failed to delete S3 cache %s: %s', ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")), e)
            db.query(TutorResponseCache).filter(TutorResponseCache.course_id.in_(course_ids)).delete(synchronize_session=False)
            
            # 6. Now safe to delete courses (parent records)
            db.query(Course).filter(Course.author_id == user.id).delete(synchronize_session=False)
        
        db.commit()
        log_activity(db, user.id, "CLEAR_ALL_DATA")
        return Response(status_code=204)
    except Exception as e:
        db.rollback()
        logger.error('Error clearing user data: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/kb/cleanup-orphaned-modules", tags=["Knowledge Base (Trainer)"])
def cleanup_orphaned_modules(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Cleans up orphaned modules (modules whose course no longer exists).
    
    This is useful when modules were left behind after course deletion.
    - **Required Role**: Trainer or Super Admin.
    """
    logger.info('[CLEANUP] User %s is cleaning up orphaned modules', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")))
    
    try:
        # Get all user's courses
        courses = db.query(Course).filter(Course.author_id == user.id).all()
        valid_course_ids = {course.id for course in courses}
        
        logger.info('[CLEANUP] User has %s valid courses', len(valid_course_ids))
        
        # Scan all modules and find orphans
        orphaned_modules = []
        paginator = s3_client.get_paginator('list_objects_v2')
        report_pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user.id}/generated_reports/")
        
        for page in report_pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                        module_course_id = meta_data.get("course_id")
                        
                        # If module has a course_id but that course doesn't exist, it's orphaned
                        if module_course_id and module_course_id not in valid_course_ids:
                            orphaned_modules.append({
                                'json_key': key,
                                'md_key': key.replace('.json', '.md'),
                                'course_id': module_course_id,
                                'source_doc': meta_data.get('source_doc', 'unknown')
                            })
                            logger.info('[CLEANUP] Found orphaned module: %s (course_id: %s)', ("" if key is None else str(key).replace("\n", "").replace("\r", "")), ("" if module_course_id is None else str(module_course_id).replace("\n", "").replace("\r", "")))
                            
                    except (ClientError, json.JSONDecodeError) as e:
                        logger.warning('[CLEANUP] Error reading module %s: %s', ("" if key is None else str(key).replace("\n", "").replace("\r", "")), e)
                        continue
        
        if not orphaned_modules:
            logger.info('[CLEANUP] No orphaned modules found for user %s', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")))
            return {
                "message": "No orphaned modules found",
                "deleted_count": 0
            }
        
        # Delete orphaned modules
        keys_to_delete = []
        for module in orphaned_modules:
            keys_to_delete.append({'Key': module['json_key']})
            keys_to_delete.append({'Key': module['md_key']})
        
        # Delete in batches
        deleted_count = 0
        for i in range(0, len(keys_to_delete), 1000):
            batch = keys_to_delete[i:i+1000]
            s3_client.delete_objects(Bucket=S3_BUCKET_NAME, Delete={'Objects': batch})
            deleted_count += len(batch) // 2
            logger.info('[CLEANUP] Deleted batch %s (%s files)', ("" if i//1000 + 1 is None else str(i//1000 + 1).replace("\n", "").replace("\r", "")), len(batch))
        
        # Also clean up Pinecone vectors for orphaned source documents
        orphaned_sources = list(set(m['source_doc'] for m in orphaned_modules))
        for source_doc in orphaned_sources:
            # Delete by vector-ID prefix: a serverless index rejects delete-by-filter, so
            # the old call left every orphaned document's embeddings in place.
            delete_vectors_for_source_document(user.id, source_doc)
        
        log_activity(db, user.id, "CLEANUP_ORPHANED_MODULES", f"Cleaned up {deleted_count} orphaned modules")
        
        return {
            "message": f"Successfully cleaned up {deleted_count} orphaned modules",
            "deleted_count": deleted_count,
            "orphaned_sources": orphaned_sources
        }
        
    except Exception as e:
        logger.error('[CLEANUP] Error cleaning up orphaned modules: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/kb/stats", tags=["Knowledge Base (Trainer)"])
def get_kb_stats(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Retrieves statistics for the trainer's personal knowledge base.

    Includes counts of source documents (courses), generated modules, indexed vector chunks, and review questions.
    - **Required Role**: Trainer or Super Admin.
    """
    # FIXED: Count courses instead of individual uploaded documents
    # Since multiple documents can belong to one course, we should count courses
    course_count = db.query(Course).filter(Course.author_id == user.id).count()
    
    # FIXED: Count only modules that belong to user's courses (not duplicates or orphaned modules)
    report_count = 0
    try:
        # Get all user's courses and their source documents
        courses = db.query(Course).filter(Course.author_id == user.id).all()
        course_ids = {course.id for course in courses}
        
        # Build a map of source_doc -> course_id for fallback matching. Keyed on the
        # NORMALIZED name and on the display names too, so a module stored under the
        # randomized upload name (VAPT #4) still counts against a course whose
        # source_documents predates that change, and vice versa.
        source_doc_to_course = {}
        for course in courses:
            candidate_names = list(_course_source_doc_names(course))
            candidate_names.extend(course_document_display_map(course).values())
            for doc in candidate_names:
                for key in (doc, normalize_document_name(doc)):
                    if key and key not in source_doc_to_course:
                        source_doc_to_course[key] = course.id
        
        # Count modules that have course_id matching user's courses. Served from the
        # per-request index instead of one GET per metadata object in sequence, which is what
        # made this tile slow enough to abort for trainers with many generated modules.
        counted_modules = []
        for s3_key, meta_data in author_report_metadata(db, user.id):
            module_course_id = meta_data.get('course_id')

            # First try: Direct course_id match
            if module_course_id and module_course_id in course_ids:
                report_count += 1
                counted_modules.append(f"{s3_key} (course_id: {module_course_id})")
            # Second try: Fallback to source_doc matching for old modules without course_id
            elif not module_course_id:
                source_doc = meta_data.get('source_doc')
                if source_doc and (
                    source_doc in source_doc_to_course
                    or normalize_document_name(source_doc) in source_doc_to_course
                ):
                    report_count += 1
                    counted_modules.append(f"{s3_key} (source_doc: {source_doc})")
        
        if report_count > 0:
            logger.info('[KB_STATS] Counted modules: %s', ("" if counted_modules is None else str(counted_modules).replace("\n", "").replace("\r", "")))
    except BotoCoreError as e:
        logger.error('Error counting modules: %s', e)
        pass
    
    # Count the namespace by enumerating vector IDs, not with describe_index_stats().
    # describe_index_stats() is an aggregate that lags well behind a bulk delete on a
    # serverless index, so it kept reporting a deleted course's embeddings - the tile showed
    # "24 Indexed Chunks" beside "0 Courses" long after the vectors were gone. Enumerating IDs
    # reports what is really in the namespace and is the same view the delete path acts on.
    chunk_count = count_vectors_in_namespace(user.id)
    if chunk_count < 0:
        # Pinecone could not be enumerated; fall back to the aggregate rather than claim zero.
        try:
            stats = pinecone_index.describe_index_stats()
            chunk_count = stats.namespaces.get(user.id, {}).get('vector_count', 0)
        except Exception as e:
            logger.error('Error getting Pinecone stats: %s', log_safe(e))
            chunk_count = 0
    
    # Get total review questions count for user's courses
    question_count = 0
    try:
        question_count = db.query(AssessmentQuestion).join(
            Course, AssessmentQuestion.course_id == Course.id
        ).filter(Course.author_id == user.id).count()
    except Exception as e:
        logger.error('Error counting questions: %s', e)
        question_count = 0
    
    logger.info('[KB_STATS] User %s: %s courses, %s modules, %s chunks, %s questions', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course_count is None else str(course_count).replace("\n", "").replace("\r", "")), ("" if report_count is None else str(report_count).replace("\n", "").replace("\r", "")), ("" if chunk_count is None else str(chunk_count).replace("\n", "").replace("\r", "")), ("" if question_count is None else str(question_count).replace("\n", "").replace("\r", "")))
    
    return { 
        "total_documents": course_count,  # Now returns course count instead of file count
        "total_reports": report_count,  # Now only counts modules belonging to user's courses
        "total_chunks": chunk_count,
        "total_questions": question_count
    }

@router.get("/kb/search", tags=["Knowledge Base (Trainer)"])
def search_kb(query: str, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Performs a semantic search across the trainer's knowledge base using a query.

    - **Required Role**: Trainer or Super Admin.
    - **query**: The search string.
    - **Returns**: A list of the top k most relevant text chunks and their sources.
    """
    if not query: raise HTTPException(status_code=400, detail="Query parameter is required.")
    try:
        logger.info("Generating query vector for: '%s'", ("" if query is None else str(query).replace("\n", "").replace("\r", "")))
        query_vector = embedding_model.embed_query(query)
        logger.info('Querying Pinecone index in namespace: %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")))
        search_response = pinecone_index.query( namespace=user.id, vector=query_vector, top_k=DEFAULT_RAG_TOP_K, include_metadata=True )
        formatted_results = []
        if search_response.get('matches'):
            # Chunk metadata records the randomized STORAGE name (VAPT #4); the search card must
            # show the filename the trainer uploaded. Resolved once for the whole result set.
            display_map = author_document_display_map(db, user.id)
            for match in search_response['matches']:
                metadata = match.get('metadata', {})
                source_name = metadata.get("source", "")
                formatted_results.append({
                    "content": strip_bracket_image_tags(metadata.get("text", "")),
                    "source": display_name_from_map(display_map, source_name) or "Unknown",
                    "score": float(match.get('score', 0.0)),
                })
        logger.info('Found %s results for query.', len(formatted_results))
        return formatted_results
    except Exception as e:
        logger.error('Error during KB search for user %s: %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail="An error occurred during search.")

# --- Collaboration & Sharing Endpoints ---
@router.get("/users/in-my-process", response_model=List[UserSimple], tags=["Collaboration & Sharing"])
def get_users_in_my_process(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Lists users available for sharing within the same process group.
    Includes trainers and super admins from the SAME PROCESS only.
    Excludes trainees and users from other processes.
    """
    if user.role == 'super_admin':
        # Super admin can only share with trainers and super admins in their mapped process
        colleagues = db.query(User).filter(
            User.id != user.id,
            User.is_approved == True,
            User.process_id == user.process_id,  # CRITICAL FIX: Only same process
            User.role.in_(['trainer', 'super_admin'])
        ).order_by(User.email).all()
    else:
        # Trainers can share with trainers and super admins in same process
        colleagues = db.query(User).filter(
            User.id != user.id,
            User.is_approved == True,
            User.process_id == user.process_id,  # CRITICAL FIX: Only same process
            User.role.in_(['trainer', 'super_admin'])
        ).order_by(User.email).all()
    
    return [UserSimple(id=c.id, email=c.email, role=c.role) for c in colleagues]

@router.get("/users/in-process/{user_id}", response_model=List[UserSimple], tags=["Collaboration & Sharing"])
def get_users_in_user_process(user_id: str, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Lists users available for sharing within a specific user's process.
    Used by super admin when viewing another user's content in "All Users" tab.
    Returns trainers and super admins from the target user's process.
    """
    # Only super admins can use this endpoint
    if current_user.role != 'super_admin':
        raise HTTPException(status_code=403, detail="Only super admins can access this endpoint")
    
    # Get the target user
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get users from the target user's process
    colleagues = db.query(User).filter(
        User.id != user_id,  # Exclude the target user
        User.is_approved == True,
        User.process_id == target_user.process_id,  # Target user's process
        User.role.in_(['trainer', 'super_admin'])
    ).order_by(User.email).all()
    
    return [UserSimple(id=c.id, email=c.email, role=c.role) for c in colleagues]

@router.get("/share/existing-recipients", tags=["Collaboration & Sharing"])
def get_existing_share_recipients(
    source_doc_name: str,
    owner_id: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Returns user IDs that already have this course/modules shared with them.
    Used by the share modal to highlight already-shared recipients.
    - owner_id: if provided (super admin context), use that user as the sharer; otherwise use current user.
    """
    sharer_id = owner_id if (owner_id and current_user.role == 'super_admin') else current_user.id

    # Find the course for this source_doc
    course = db.query(Course).filter(
        Course.source_doc_name == source_doc_name,
        Course.author_id == sharer_id
    ).first()

    module_shared_ids = set()
    course_shared_ids = set()

    # Already shared modules (SharedReport)
    module_shares = db.query(SharedReport.recipient_id).filter(
        SharedReport.source_doc_name == source_doc_name,
        SharedReport.sharer_id == sharer_id
    ).all()
    module_shared_ids = {r[0] for r in module_shares}

    # Already shared courses (SharedCourse)
    if course:
        course_shares = db.query(SharedCourse.recipient_id).filter(
            SharedCourse.course_id == course.id
        ).all()
        course_shared_ids = {r[0] for r in course_shares}

    return {
        "module_shared_recipient_ids": list(module_shared_ids),
        "course_shared_recipient_ids": list(course_shared_ids),
    }

@router.post("/kb/share", tags=["Collaboration & Sharing"])
def share_document_reports(req: ShareRequest, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Shares a set of reports from a source document with another user (colleague).

    Creates a share record and sends a notification to the recipient.
    - **Required Role**: Trainer or Super Admin.
    - **Raises 400**: If sharing rules are violated (e.g., recipient not in process, already shared).
    """
    try:
        # Log sharing activity
        recipient = db.query(User).filter(User.id == req.recipient_id).first()
        recipient_email = recipient.email if recipient else "Unknown"
        log_activity(db, user.id, "MODULE_SHARE", f"Sharing '{req.source_doc_name}' with {recipient_email}")
        
        result_message = _execute_share_reports(db=db, sharer_id=user.id, recipient_id=req.recipient_id, source_doc_name=req.source_doc_name, reason=req.reason)
        if "Successfully" in result_message:
            return {"message": result_message}
        else:
            raise HTTPException(status_code=400, detail=result_message)
    except HTTPException:
        # Re-raise HTTP exceptions (like 400 errors) without wrapping them
        raise
    except Exception as e:
        logger.error('Error in /kb/share endpoint: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred.")

@router.post("/shared/courses", response_model=dict, tags=["Collaboration & Sharing"])
def share_course(req: ShareCourseRequest, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Shares an entire course with another trainer/super admin in the same process.
    
    Creates a course share record and sends a notification to the recipient.
    - **Required Role**: Trainer or Super Admin.
    - **Raises 400**: If sharing rules are violated (e.g., recipient not in process, already shared).
    """
    try:
        # Verify course ownership
        course = db.query(Course).filter(
            Course.id == req.course_id,
            Course.author_id == user.id
        ).first()
        
        if not course:
            raise HTTPException(status_code=404, detail="Course not found or you don't have permission to share it")
        
        # Verify recipient exists and is in same process
        recipient = db.query(User).filter(User.id == req.recipient_id).first()
        if not recipient:
            raise HTTPException(status_code=404, detail="Recipient not found")
        
        if recipient.process_id != user.process_id:
            raise HTTPException(status_code=400, detail="Can only share courses with users in your process")
        
        if recipient.role not in ['trainer', 'super_admin']:
            raise HTTPException(status_code=400, detail="Can only share courses with trainers or super admins")
        
        if recipient.id == user.id:
            raise HTTPException(status_code=400, detail="Cannot share course with yourself")
        
        # Check if already shared
        existing_share = db.query(SharedCourse).filter(
            SharedCourse.course_id == req.course_id,
            SharedCourse.recipient_id == req.recipient_id
        ).first()
        
        if existing_share:
            raise HTTPException(status_code=400, detail="Course already shared with this user")
        
        # Create share record
        shared_course = SharedCourse(
            course_id=req.course_id,
            sharer_id=user.id,
            recipient_id=req.recipient_id,
            reason=req.reason  # NEW: Store the reason
        )
        db.add(shared_course)
        
        # Send notification to recipient
        reason_text = f" Reason: {req.reason}" if req.reason else ""
        course_display_name = course_display_label(course)
        notification = Notification(
            user_id=req.recipient_id,
            message=f"{user.email} shared the course '{course_display_name}' with you.{reason_text}",
            related_link="/shared",
            course_id=course_notification_id(course),
        )
        db.add(notification)
        
        # Log activity
        log_activity(db, user.id, "COURSE_SHARE", f"Shared course '{course_display_name}' with {recipient.email}")
        
        db.commit()
        
        return {"message": f"Course '{course_display_name}' shared successfully with {recipient.email}"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error sharing course: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred while sharing the course")

@router.get("/shared/courses", response_model=List[SharedCourseInfo], tags=["Collaboration & Sharing"])
def get_shared_courses(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves a list of courses that have been shared with the current user.
    """
    shares = db.query(SharedCourse).join(
        User, SharedCourse.sharer_id == User.id
    ).join(
        Course, SharedCourse.course_id == Course.id
    ).filter(
        SharedCourse.recipient_id == user.id
    ).order_by(SharedCourse.shared_at.desc()).all()
    
    result = []
    for s in shares:
        # Falls back to the uploaded document's own name - source_doc_name is a randomized
        # storage name (VAPT #4), so cleaning it up still left a bare UUID on screen.
        course_title = course_display_label(s.course)
        
        result.append(SharedCourseInfo(
            id=s.id,
            course_id=s.course_id,
            course_name=s.course.source_doc_name,
            course_title=course_title,
            sharer_email=s.sharer.email,
            shared_at=s.shared_at,
            status=s.status,
            feedback=s.feedback,
            reason=s.reason,  # NEW: Include reason
            author_id=s.sharer_id
        ))
    
    return result

@router.get("/shared/courses/{share_id}/content", tags=["Collaboration & Sharing"])
def get_shared_course_content(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves the full content of a shared course, including all modules from all source documents and assessment questions.
    
    Also marks the share status as 'viewed' if it was 'pending'.
    - **share_id**: The UUID of the course share record.
    - **Raises 404**: If the share is not found or the user is not the recipient.
    """
    share = db.query(SharedCourse).filter(SharedCourse.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared course not found or you are not authorized to view it")
    
    course = share.course
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Get all course modules using existing function
    doc_reports = _get_grouped_reports(share.sharer_id, db, include_content=True, requesting_user_id=user.id, validate_images=False)
    
    # Get all source documents for this course
    source_docs = []
    if course.source_documents:
        try:
            source_docs = json.loads(course.source_documents)
            logger.info('[SHARED COURSE] Course %s has %s source documents: %s', ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")), len(source_docs), ("" if source_docs is None else str(source_docs).replace("\n", "").replace("\r", "")))
        except Exception as e:
            logger.warning('[SHARED COURSE] Failed to parse source_documents for course %s: %s', ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")), e)
            source_docs = [course.source_doc_name]
    else:
        logger.info('[SHARED COURSE] Course %s has no source_documents field, using source_doc_name: %s', ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")), ("" if course.source_doc_name is None else str(course.source_doc_name).replace("\n", "").replace("\r", "")))
        source_docs = [course.source_doc_name]
    
    # CRITICAL FIX: _get_grouped_reports returns ONE group per course (keyed by source_doc_name)
    # All modules from all documents in a multi-document course are in that single group
    # So we just need to find the group for this course by its source_doc_name
    doc_report = next((g for g in doc_reports if g['source_doc'] == course.source_doc_name), None)
    
    if not doc_report or not doc_report.get('reports'):
        logger.error('[SHARED COURSE] No modules found for course %s (source_doc_name: %s)', ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")), ("" if course.source_doc_name is None else str(course.source_doc_name).replace("\n", "").replace("\r", "")))
        raise HTTPException(status_code=404, detail="Course modules not found")
    
    all_reports = doc_report['reports']
    logger.info("[SHARED COURSE] Found %s total modules for shared course %s from group '%s'", len(all_reports), ("" if share_id is None else str(share_id).replace("\n", "").replace("\r", "")), ("" if course.source_doc_name is None else str(course.source_doc_name).replace("\n", "").replace("\r", "")))
    
    # Get assessment configuration and questions
    assessment_config = db.query(AssessmentConfig).filter(
        AssessmentConfig.course_id == course.id
    ).first()
    
    assessment_questions = []
    if assessment_config:
        questions = db.query(AssessmentQuestion).filter(
            AssessmentQuestion.course_id == course.id
        ).order_by(AssessmentQuestion.created_at).all()
        
        assessment_questions = [
            {
                "id": q.id,
                "question_text": q.question_text,
                "question_type": q.question_type,
                "options": [q.option_a, q.option_b, q.option_c, q.option_d] if q.question_type == 'mcq' else [],
                "correct_answer": q.correct_answer,
                "module_index": q.module_index,
                "is_approved": q.is_approved,
                "created_at": q.created_at.isoformat() if q.created_at else None
            } for q in questions
        ]
    
    # Build response with all modules from all documents
    response = {
        "source_doc": course.source_doc_name,
        "source_documents": source_docs,
        "course_title": course_display_label(course),
        "is_published": course.is_live,
        "reports": all_reports,
        "course_id": course.id,
        "question_count": len(assessment_questions),
        "author_id": share.sharer_id,
        "is_shared_course": True,
        "share_id": share_id,
        "can_edit": True,  # Recipients can propose edits
        "hide_review_questions": False,  # Show assessment questions for shared courses
        "assessment_config": {
            "id": assessment_config.id if assessment_config else None,
            "assessment_scope": assessment_config.assessment_scope if assessment_config else None,
            "question_type": assessment_config.question_type if assessment_config else None,
            "total_questions": assessment_config.total_questions if assessment_config else 0,
            "pass_percentage": assessment_config.pass_percentage if assessment_config else None,
            "max_attempts": assessment_config.max_attempts if assessment_config else None,
            "is_approved": assessment_config.is_approved if assessment_config else False
        } if assessment_config else None,
        "assessment_questions": assessment_questions
    }
    
    # Mark as viewed if pending
    if share.status == 'pending':
        share.status = 'viewed'
        course_display_name = course_display_label(course)
        notification = Notification(
            user_id=share.sharer_id,
            message=f"{user.email} viewed the shared course '{course_display_name}'.",
            course_id=course_notification_id(course),
        )
        db.add(notification)
        db.commit()
    
    return response

@router.get("/shared/courses/{share_id}/documents", tags=["Collaboration & Sharing"])
def get_shared_course_documents(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves all source documents associated with a shared course.
    Returns list of document names and their download URLs.
    Works for both complete course shares and module-only shares.
    """
    # Try to find in SharedCourse first
    share = db.query(SharedCourse).filter(SharedCourse.id == share_id).first()
    
    if share:
        # This is a shared course
        if share.recipient_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="Access denied")
        
        course = share.course
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        author_id = share.sharer_id
        display_map = course_document_display_map(course)
        course_title = course.course_title or friendly_document_name(
            course.source_doc_name, display_map
        )

        # Parse source documents (STORAGE names - see get_course_documents)
        source_docs = []
        if course.source_documents:
            try:
                source_docs = json.loads(course.source_documents)
            except (ValueError, TypeError):
                source_docs = [course.source_doc_name]
        else:
            source_docs = [course.source_doc_name]
    else:
        # Try SharedReport (module-only share)
        report_share = db.query(SharedReport).filter(SharedReport.id == share_id).first()
        
        if not report_share:
            raise HTTPException(status_code=404, detail="Shared content not found")
        
        if report_share.recipient_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="Access denied")
        
        author_id = report_share.sharer_id
        source_doc_name = report_share.source_doc_name
        
        # For module-only shares, try to find the associated course
        course = db.query(Course).filter(
            Course.author_id == author_id,
            Course.source_doc_name == source_doc_name
        ).first()
        
        if course:
            display_map = course_document_display_map(course)
            course_title = course.course_title or friendly_document_name(
                source_doc_name, display_map
            )
            # Parse source documents (STORAGE names - see get_course_documents)
            if course.source_documents:
                try:
                    source_docs = json.loads(course.source_documents)
                except (ValueError, TypeError):
                    source_docs = [source_doc_name]
            else:
                source_docs = [source_doc_name]
        else:
            # No course found, just use the single source doc
            display_map = {}
            course_title = friendly_document_name(source_doc_name)
            source_docs = [source_doc_name]

    # Build document list with metadata
    documents = []
    seen_filenames = {}  # Track how many times we've seen each display name

    for doc_name in source_docs:
        friendly = friendly_document_name(doc_name, display_map)
        # Track duplicate names so two uploads of the same file stay distinguishable
        if friendly in seen_filenames:
            seen_filenames[friendly] += 1
            display_name = f"{friendly} (copy {seen_filenames[friendly]})"
        else:
            seen_filenames[friendly] = 1
            display_name = friendly

        s3_key = f"{author_id}/uploaded_docs/{doc_name}"
        # File type from the STORAGE name (validated extension), not the display name
        file_ext = doc_name.lower().rsplit('.', 1)[-1] if '.' in doc_name else 'unknown'
        file_type = 'PDF' if file_ext == 'pdf' else 'Image' if file_ext in ['png', 'jpg', 'jpeg'] else file_ext.upper()
        try:
            # Check if document exists in S3
            s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)

            documents.append({
                'filename': display_name,
                'original_filename': doc_name,
                'file_type': file_type,
                'download_url': f"/shared/view-document?author_id={author_id}&filename={doc_name}"
            })
        except ClientError:
            # Even if document doesn't exist in S3, still show it in the list
            logger.warning('Document %s not found in S3 for shared content %s, but including in list', ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")), ("" if share_id is None else str(share_id).replace("\n", "").replace("\r", "")))
            documents.append({
                'filename': f"{display_name} (not found)",
                'original_filename': doc_name,
                'file_type': file_type,
                'download_url': None
            })

    return {
        'success': True,
        'share_id': share_id,
        'course_title': course_title,
        'documents': documents
    }

@router.get("/shared/view-document", tags=["Collaboration & Sharing"])
def view_shared_document(author_id: str, filename: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Serves a document from a shared course or module for viewing in the browser.
    
    - **author_id**: The ID of the content owner (sharer).
    - **filename**: The filename of the source document.
    - **Raises 404**: If the document is not found.
    - **Raises 403**: If the user doesn't have access to this shared content.
    """
    if ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid document name.")
    
    # Verify user has access to content from this author
    # Check if there's any shared course or report from this author to this user
    has_access = False
    
    # Check SharedCourse
    shared_course = db.query(SharedCourse).join(Course).filter(
        SharedCourse.recipient_id == user.id,
        SharedCourse.sharer_id == author_id
    ).first()
    
    owning_course = None
    if shared_course:
        # Verify this filename is one of the course's source documents. The comparison goes
        # through _course_contains_document so it matches whether the caller passes the
        # randomized storage name (VAPT #4) or the original filename recorded by an older
        # course - a plain "in source_documents" test denied access to valid documents.
        course = shared_course.course
        if course and _course_contains_document(course, filename):
            has_access = True
            owning_course = course


    # Check SharedReport if not found in SharedCourse
    if not has_access:
        shared_report = db.query(SharedReport).filter(
            SharedReport.recipient_id == user.id,
            SharedReport.sharer_id == author_id
        ).first()
        
        if shared_report:
            # Find the course associated with this shared report
            course = db.query(Course).filter(
                Course.author_id == author_id,
                Course.source_doc_name == shared_report.source_doc_name
            ).first()
            
            if course and _course_contains_document(course, filename):
                has_access = True
                owning_course = course


    # Super admins can view any shared document
    if user.role == 'super_admin':
        has_access = True
    
    if not has_access:
        raise HTTPException(status_code=403, detail="Access denied. You don't have permission to view this document.")
    
    # Retrieve document from S3
    s3_key = f"{author_id}/uploaded_docs/{filename}"
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        # VAPT: Double Extension File Upload — never echo the stored name verbatim.
        # The S3 key stays the randomized storage name; the browser gets the uploaded
        # filename, which safe_content_disposition() sanitizes to ASCII alnum/._- anyway.
        display_map = (
            course_document_display_map(owning_course) if owning_course is not None else {}
        )
        download_name = friendly_document_name(filename, display_map)
        return Response(
            content=obj['Body'].read(),
            media_type=obj.get('ContentType') or "application/octet-stream",
            headers={"Content-Disposition": safe_content_disposition(download_name, disposition="inline")},
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise HTTPException(status_code=404, detail="Document not found.")
        raise

@router.post("/shared/courses/{share_id}/propose-edit", tags=["Collaboration & Sharing"])
def propose_edit_on_shared_course(share_id: str, req: EditProposalRequest, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Submits a proposed edit on a shared course module for the owner to review.
    
    - **share_id**: The UUID of the course share record.
    - **Body**: Contains the filename of the report and the proposed new content.
    - **Raises 404**: If the share or original report is not found.
    """
    share = db.query(SharedCourse).filter(SharedCourse.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared course not found or you are not authorized")
    
    course = share.course
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Get original content from S3
    report_key = f"{share.sharer_id}/generated_reports/{req.report_filename}"
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=report_key)
        original_content = obj['Body'].read().decode('utf-8')
    except ClientError:
        raise HTTPException(status_code=404, detail="Original report file not found.")
    
    # Create edit proposal
    new_edit = SharedCourseEdit(
        shared_course_id=share.id,
        report_filename=req.report_filename,
        editor_id=user.id,
        original_content=original_content,
        proposed_content=req.proposed_content,
        edit_type='module'
    )
    db.add(new_edit)
    db.flush()
    
    # Notify course owner. The notification names the MODULE, not its storage filename -
    # report filenames are randomized keys ("<uuid>_seg2.md") and meant nothing to the owner.
    course_display_name = course_display_label(course)
    module_label = module_display_label(
        share.sharer_id, course.source_doc_name, req.report_filename, db
    )
    owner_notif = Notification(
        user_id=share.sharer_id,
        message=f"{user.email} proposed edits to '{module_label}' in shared course '{course_display_name}'.",
        related_link=f"/review/{new_edit.id}",
        course_id=course_notification_id(course),
    )
    db.add(owner_notif)
    db.commit()
    
    return {"message": "Edit proposal submitted for review."}

@router.post("/shared/courses/{share_id}/propose-question-edit", tags=["Collaboration & Sharing"])
def propose_question_edit_on_shared_course(share_id: str, req: dict, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Submits a proposed edit to an assessment question in a shared course for the owner to review.
    
    - **share_id**: The UUID of the course share record.
    - **Body**: Contains the question_id and the proposed changes.
    - **Raises 404**: If the share or question is not found.
    """
    share = db.query(SharedCourse).filter(SharedCourse.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared course not found or you are not authorized")
    
    course = share.course
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    question_id = req.get('question_id')
    proposed_changes = req.get('proposed_changes', {})
    
    if not question_id:
        raise HTTPException(status_code=400, detail="question_id is required")
    
    # Verify the question exists and belongs to this course
    question = db.query(AssessmentQuestion).filter(
        AssessmentQuestion.id == question_id,
        AssessmentQuestion.course_id == course.id
    ).first()
    
    if not question:
        raise HTTPException(status_code=404, detail="Assessment question not found")
    
    # Create edit proposal for the question
    original_question_data = {
        "question_text": question.question_text,
        "question_type": question.question_type,
        "options": [question.option_a, question.option_b, question.option_c, question.option_d] if question.question_type == 'mcq' else [],
        "correct_answer": question.correct_answer,
        "module_index": question.module_index
    }
    
    new_edit = SharedCourseEdit(
        shared_course_id=share.id,
        report_filename=f"question_{question_id}",
        editor_id=user.id,
        original_content=json.dumps(original_question_data),
        proposed_content=json.dumps(proposed_changes),
        edit_type='question'
    )
    db.add(new_edit)
    db.flush()
    
    # Notify course owner
    course_display_name = course_display_label(course)
    owner_notif = Notification(
        user_id=share.sharer_id,
        message=f"{user.email} proposed edits to an assessment question in shared course '{course_display_name}'.",
        related_link=f"/review/{new_edit.id}",
        course_id=course_notification_id(course),
    )
    db.add(owner_notif)
    db.commit()
    
    return {"message": "Question edit proposal submitted for review."}


@router.get("/shared/courses/{share_id}/questions", tags=["Collaboration & Sharing"])
def get_shared_course_questions(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves assessment questions for a shared course.
    """
    share = db.query(SharedCourse).filter(SharedCourse.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared course not found or you are not authorized to view it")
    
    course = share.course
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    # Get assessment questions
    questions = db.query(AssessmentQuestion).filter(
        AssessmentQuestion.course_id == course.id
    ).order_by(AssessmentQuestion.created_at).all()
    
    return {
        "questions": [
            {
                "id": q.id,
                "question_text": q.question_text,
                "question_type": q.question_type,
                "options": [q.option_a, q.option_b, q.option_c, q.option_d] if q.question_type == 'mcq' else [],
                "correct_answer": q.correct_answer,
                "module_index": q.module_index,
                "is_approved": q.is_approved,
                "created_at": q.created_at.isoformat() if q.created_at else None
            } for q in questions
        ],
        "total_questions": len(questions),
        "course_name": course.source_doc_name,
        "is_shared": True,
        "can_propose_edits": True
    }

@router.get("/sharing/history", tags=["Collaboration & Sharing"])
def get_sharing_history(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Get sharing history - courses and reports shared by the current user and shared with them.
    """
    try:
        # The rows below are only ever read through their relationships, so load those with the
        # row instead of letting each one lazy-load inside the response loops.
        from sqlalchemy.orm import joinedload

        # Courses shared by current user
        courses_shared_by_me = db.query(SharedCourse).join(
            Course, SharedCourse.course_id == Course.id
        ).join(
            User, SharedCourse.recipient_id == User.id
        ).options(
            joinedload(SharedCourse.course),
            joinedload(SharedCourse.recipient),
        ).filter(
            SharedCourse.sharer_id == user.id
        ).order_by(SharedCourse.shared_at.desc()).all()

        # Reports shared by current user
        reports_shared_by_me = db.query(SharedReport).join(
            User, SharedReport.recipient_id == User.id
        ).options(
            joinedload(SharedReport.recipient),
        ).filter(
            SharedReport.sharer_id == user.id
        ).order_by(SharedReport.shared_at.desc()).all()

        # Courses shared with current user
        courses_shared_with_me = db.query(SharedCourse).join(
            Course, SharedCourse.course_id == Course.id
        ).join(
            User, SharedCourse.sharer_id == User.id
        ).options(
            joinedload(SharedCourse.course),
            joinedload(SharedCourse.sharer),
        ).filter(
            SharedCourse.recipient_id == user.id
        ).order_by(SharedCourse.shared_at.desc()).all()

        # Reports shared with current user
        reports_shared_with_me = db.query(SharedReport).join(
            User, SharedReport.sharer_id == User.id
        ).options(
            joinedload(SharedReport.sharer),
        ).filter(
            SharedReport.recipient_id == user.id
        ).order_by(SharedReport.shared_at.desc()).all()

        # A shared report carries only its source document name, so its course title comes from
        # a (author_id, source_doc_name) lookup. Resolve every report's course in one query -
        # both report loops below read this map, and `setdefault` keeps the first row the query
        # returns, which is exactly what the per-report `.first()` it replaces returned.
        report_course_by_key = {}
        report_shares = list(reports_shared_by_me) + list(reports_shared_with_me)
        if report_shares:
            report_sharer_ids = {s.sharer_id for s in report_shares if s.sharer_id}
            report_doc_names = {s.source_doc_name for s in report_shares if s.source_doc_name}
            if report_sharer_ids and report_doc_names:
                for course_row in db.query(Course).filter(
                    Course.author_id.in_(report_sharer_ids),
                    Course.source_doc_name.in_(report_doc_names),
                ).all():
                    report_course_by_key.setdefault(
                        (course_row.author_id, course_row.source_doc_name), course_row
                    )

        logger.info('Sharing history for user %s: courses_by_me=%s, reports_by_me=%s, courses_with_me=%s, reports_with_me=%s', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")), len(courses_shared_by_me), len(reports_shared_by_me), len(courses_shared_with_me), len(reports_shared_with_me))
        
        # Combine courses and reports shared by me
        shared_by_me = []
        
        # Add courses
        for s in courses_shared_by_me:
            shared_by_me.append({
                "id": s.id,
                "course_name": s.course.source_doc_name,
                "course_title": s.course.course_title,
                "recipient_email": s.recipient.email,
                "shared_at": s.shared_at.isoformat(),
                "status": s.status,
                "feedback": s.feedback,
                "reason": s.reason,  # NEW: Include reason
                "type": "course"
            })
        
        # Add reports
        for s in reports_shared_by_me:
            # Course title for this report's source document, from the map built above
            course = report_course_by_key.get((s.sharer_id, s.source_doc_name))

            course_title = course.course_title if course and course.course_title else None

            shared_by_me.append({
                "id": s.id,
                "course_name": s.source_doc_name,
                "course_title": course_title,
                "recipient_email": s.recipient.email,
                "shared_at": s.shared_at.isoformat(),
                "status": s.status,
                "feedback": s.feedback,
                "reason": s.reason,  # NEW: Include reason
                "type": "report"
            })
        
        # Combine courses and reports shared with me
        shared_with_me = []
        
        # Add courses
        for s in courses_shared_with_me:
            shared_with_me.append({
                "id": s.id,
                "course_name": s.course.source_doc_name,
                "course_title": s.course.course_title,
                "sharer_email": s.sharer.email,
                "shared_at": s.shared_at.isoformat(),
                "status": s.status,
                "feedback": s.feedback,
                "reason": s.reason,  # NEW: Include reason
                "type": "course"
            })
        
        # Add reports
        for s in reports_shared_with_me:
            # Course title for this report's source document, from the map built above
            course = report_course_by_key.get((s.sharer_id, s.source_doc_name))

            course_title = course.course_title if course and course.course_title else None

            shared_with_me.append({
                "id": s.id,
                "course_name": s.source_doc_name,
                "course_title": course_title,
                "sharer_email": s.sharer.email,
                "shared_at": s.shared_at.isoformat(),
                "status": s.status,
                "feedback": s.feedback,
                "reason": s.reason,  # NEW: Include reason
                "type": "report"
            })
        
        # Sort by shared_at date (most recent first)
        shared_by_me.sort(key=lambda x: x['shared_at'], reverse=True)
        shared_with_me.sort(key=lambda x: x['shared_at'], reverse=True)
        
        return {
            "shared_by_me": shared_by_me,
            "shared_with_me": shared_with_me
        }
    except Exception as e:
        logger.error('Error getting sharing history for user %s: %s', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load sharing history")

@router.post("/sharing/{share_id}/revoke/delete", tags=["Collaboration & Sharing"])
def revoke_sharing(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Revoke sharing access for a course. Only the original sharer can revoke access.
    """
    share = db.query(SharedCourse).filter(SharedCourse.id == share_id).first()
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    
    if share.sharer_id != user.id and user.role != 'super_admin':
        raise HTTPException(status_code=403, detail="Only the original sharer can revoke access")
    
    # Notify the recipient
    course_display_name = course_display_label(share.course)
    notification = Notification(
        user_id=share.recipient_id,
        message=f"Access to shared course '{course_display_name}' has been revoked by {user.email}.",
        course_id=course_notification_id(share.course),
    )
    db.add(notification)
    
    # Delete the share record
    db.delete(share)
    db.commit()
    
    return {"message": "Sharing access revoked successfully"}

@router.post("/shared/reports/{share_id}/revoke/delete", tags=["Collaboration & Sharing"])
def revoke_report_sharing(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Revoke sharing access for a report. Only the original sharer can revoke access.
    """
    share = db.query(SharedReport).filter(SharedReport.id == share_id).first()
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    
    if share.sharer_id != user.id and user.role != 'super_admin':
        raise HTTPException(status_code=403, detail="Only the original sharer can revoke access")
    
    # Notify the recipient
    # Get course title for better notification
    course = db.query(Course).filter(
        Course.source_doc_name == share.source_doc_name,
        Course.author_id == share.sharer_id
    ).first()
    course_display_name = shared_source_display_label(db, share.sharer_id, share.source_doc_name, course)
    
    notification = Notification(
        user_id=share.recipient_id,
        message=f"Access to shared modules '{course_display_name}' has been revoked by {user.email}.",
        course_id=course_notification_id(course),
    )
    db.add(notification)
    
    # Delete the share record
    db.delete(share)
    db.commit()
    
    return {"message": "Report sharing access revoked successfully"}

@router.post("/kb/share-course", tags=["Collaboration & Sharing"])
def share_course_by_source_doc(req: dict, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Shares a course by source document name with another user.
    
    This endpoint finds the course by source_doc_name and shares it.
    """
    try:
        source_doc_name = req.get('source_doc_name')
        recipient_id = req.get('recipient_id')
        reason = req.get('reason')  # NEW: Get reason
        
        if not source_doc_name or not recipient_id:
            raise HTTPException(status_code=400, detail="source_doc_name and recipient_id are required")
        
        # Find the course
        course = db.query(Course).filter(
            Course.source_doc_name == source_doc_name,
            Course.author_id == user.id
        ).first()
        
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Use the existing share_course logic
        share_req = ShareCourseRequest(course_id=course.id, recipient_id=recipient_id, reason=reason)  # NEW: Pass reason
        return share_course(share_req, user, db)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error sharing course by source doc: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred while sharing the course")

@router.get("/shared/reports", tags=["Collaboration & Sharing"])
def get_shared_reports(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves a list of reports that have been shared with the current user.
    """
    from sqlalchemy.orm import joinedload

    shares = db.query(SharedReport).join(User, SharedReport.sharer_id == User.id).options(joinedload(SharedReport.sharer)).filter(SharedReport.recipient_id == user.id).order_by(SharedReport.shared_at.desc()).all()

    # Every share's course in one query instead of one lookup per row; `setdefault` keeps the
    # first row returned, matching the `.first()` this replaces.
    course_by_key = {}
    if shares:
        sharer_ids = {s.sharer_id for s in shares if s.sharer_id}
        doc_names = {s.source_doc_name for s in shares if s.source_doc_name}
        if sharer_ids and doc_names:
            for course_row in db.query(Course).filter(
                Course.author_id.in_(sharer_ids),
                Course.source_doc_name.in_(doc_names),
            ).all():
                course_by_key.setdefault((course_row.author_id, course_row.source_doc_name), course_row)

    result = []
    for s in shares:
        # Course title for this report's source document, from the map built above
        course = course_by_key.get((s.sharer_id, s.source_doc_name))

        # Falls back to the uploaded document's own name: source_doc_name is a randomized
        # storage name (VAPT #4), so cleaning it up still left a bare UUID on screen.
        course_title = shared_source_display_label(db, s.sharer_id, s.source_doc_name, course)

        result.append({
            "id": s.id,
            "source_doc_name": s.source_doc_name,
            "course_title": course_title,
            "sharer_email": s.sharer.email,
            "shared_at": s.shared_at,
            "status": s.status,
            "feedback": s.feedback,
            "reason": s.reason,  # Include reason field
            "author_id": s.sharer_id
        })
    
    return result

@router.get("/shared/reports/{share_id}/content", tags=["Collaboration & Sharing"])
def get_shared_report_content(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves the full content of a shared report, including modules.

    Also marks the share status as 'viewed' if it was 'pending'.
    - **share_id**: The UUID of the share record.
    - **Raises 404**: If the share is not found or the user is not the recipient.
    """
    share = db.query(SharedReport).filter(SharedReport.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared item not found or you are not authorized to view it")
    doc_reports = _get_grouped_reports(share.sharer_id, db, include_content=True, requesting_user_id=user.id, validate_images=False)
    doc_report = next((g for g in doc_reports if g['source_doc'] == share.source_doc_name), None)
    if not doc_report: raise HTTPException(status_code=404, detail="Source document's reports not found")
    doc_report['author_id'] = share.sharer_id
    if share.status == 'pending':
        share.status = 'viewed'
        # Get course title for better notification message
        course = db.query(Course).filter(
            Course.source_doc_name == share.source_doc_name,
            Course.author_id == share.sharer_id
        ).first()
        course_display_name = shared_source_display_label(db, share.sharer_id, share.source_doc_name, course)
        notification = Notification(
            user_id=share.sharer_id,
            message=f"{user.email} viewed the modules for '{course_display_name}'.",
            course_id=course_notification_id(course),
        )
        db.add(notification); db.commit()
    return doc_report

@router.get("/shared/reports/{share_id}/source-pdf", tags=["Collaboration & Sharing"])
def get_shared_report_pdf(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves the original document associated with a shared report.

    - **share_id**: The UUID of the share record.
    - **Raises 404**: If the share or the document is not found.
    """
    share = db.query(SharedReport).filter(SharedReport.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared item not found or you are not authorized.")
    s3_key = f"{share.sharer_id}/uploaded_docs/{share.source_doc_name}"
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        # VAPT: Double Extension File Upload — safe header name, single extension.
        return Response(
            content=obj['Body'].read(),
            media_type=obj.get('ContentType') or "application/octet-stream",
            headers={
                "Content-Disposition": safe_content_disposition(
                    share.source_doc_name, disposition="inline"
                )
            },
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Original document not found on sharer's account.")
        raise

@router.get("/shared/courses/{share_id}/source-pdf", tags=["Collaboration & Sharing"])
def get_shared_course_pdf(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves the original document associated with a shared course.

    - **share_id**: The UUID of the shared course record.
    - **Raises 404**: If the share or the document is not found.
    """
    share = db.query(SharedCourse).filter(SharedCourse.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared item not found or you are not authorized.")
    
    # Get the course to find the source document
    course = db.query(Course).filter(Course.id == share.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    
    s3_key = f"{share.sharer_id}/uploaded_docs/{course.source_doc_name}"
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        # VAPT: Double Extension File Upload — safe header name, single extension.
        return Response(
            content=obj['Body'].read(),
            media_type=obj.get('ContentType') or "application/octet-stream",
            headers={
                "Content-Disposition": safe_content_disposition(
                    course.source_doc_name, disposition="inline"
                )
            },
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise HTTPException(status_code=404, detail="Original document not found on sharer's account.")
        raise

@router.get("/shared/courses/{share_id}/modules", tags=["Collaboration & Sharing"])
def get_shared_course_modules(share_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves the modules for a shared course for tutor functionality.

    - **share_id**: The UUID of the shared course record.
    - **Raises 404**: If the share is not found.
    - **Raises 403**: If the user is not authorized.
    """
    share = db.query(SharedCourse).filter(SharedCourse.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared course not found or you are not authorized.")
    
    # Get the course
    course = db.query(Course).filter(Course.id == share.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    
    # Use the same approach as the regular reports-content endpoint
    # Get all reports for the sharer's document using _get_grouped_reports
    all_reports = _get_grouped_reports(share.sharer_id, db, include_content=True, requesting_user_id=user.id, validate_images=False)
    target_group = next((g for g in all_reports if g['source_doc'] == course.source_doc_name), None)
    
    if not target_group or not target_group['reports']:
        raise HTTPException(status_code=404, detail="No modules found for this shared course.")
    
    # Format the response for tutor compatibility
    modules_data = []
    for report in target_group['reports']:
        modules_data.append({
            "id": f"shared_{share_id}_{report['seg_index']}",  # Create a unique ID
            "title": report['title'],
            "content": report.get('content', ''),
            "seg_index": report['seg_index'],
            "seg_idx": report['seg_index'],  # Keep both for compatibility
            "course_id": course.id,
            "is_completed": False,  # Recipients don't have completion status
            "filename": report['filename']
        })
    
    # Return modules with course_title for display in tutor interface
    return {
        "modules": modules_data,
        "course_title": target_group.get('course_title') or course_display_label(course),
        "source_doc": course.source_doc_name
    }

@router.post("/shared/reports/{share_id}/propose-edit", tags=["Collaboration & Sharing"])
def propose_edit_on_shared_report(share_id: str, req: EditProposalRequest, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Submits a proposed edit on a shared module for the owner to review.

    - **share_id**: The UUID of the share record.
    - **Body**: Contains the filename of the report and the proposed new content.
    - **Raises 404**: If the share or original report is not found.
    """
    share = db.query(SharedReport).filter(SharedReport.id == share_id).first()
    if not share or (share.recipient_id != user.id and user.role != 'super_admin'):
        raise HTTPException(status_code=404, detail="Shared item not found or you are not authorized")
    report_key = f"{share.sharer_id}/generated_reports/{req.report_filename}"
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=report_key)
        original_content = obj['Body'].read().decode('utf-8')
    except ClientError: raise HTTPException(status_code=404, detail="Original report file not found.")
    new_edit = SharedReportEdit( shared_report_id=share.id, report_filename=req.report_filename, editor_id=user.id, original_content=original_content, proposed_content=req.proposed_content, edit_type='module' )
    db.add(new_edit); db.flush()
    
    # Get course title for better notification message
    course = db.query(Course).filter(
        Course.source_doc_name == share.source_doc_name,
        Course.author_id == share.sharer_id
    ).first()
    course_display_name = shared_source_display_label(db, share.sharer_id, share.source_doc_name, course)
    
    # Name the MODULE, not its storage filename - report filenames are randomized keys.
    module_label = module_display_label(share.sharer_id, share.source_doc_name, req.report_filename, db)
    owner_notif = Notification(
        user_id=share.sharer_id,
        message=f"{user.email} proposed edits to '{module_label}' in '{course_display_name}'.",
        related_link=f"/review/{new_edit.id}",
        course_id=course_notification_id(course),
    )
    db.add(owner_notif); db.commit()
    return {"message": "Edit proposal submitted for review."}

@router.get("/review/edits/{edit_id}", response_model=EditProposalInfo, tags=["Collaboration & Sharing"])
def get_edit_for_review(edit_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves the details of a specific edit proposal for review.

    Shows a diff of the original vs. proposed content with actual module titles from the course.
    - **edit_id**: The UUID of the edit proposal.
    - **Raises 404**: If the edit is not found.
    - **Raises 403**: If the user is not the owner of the content.
    """
    # Try to find the edit in SharedReportEdit first
    edit = db.query(SharedReportEdit).filter(SharedReportEdit.id == edit_id).first()

    if edit:
        # This is a report edit
        is_owner = user.id == edit.shared_report.sharer_id
        is_admin = user.role == 'super_admin'
        if not is_owner and not is_admin:
            raise HTTPException(status_code=403, detail="You are not authorized to review this edit.")

        # Show the module TITLE, never the randomized report filename.
        module_title = module_display_label(
            edit.shared_report.sharer_id,
            edit.shared_report.source_doc_name,
            edit.report_filename,
            db,
            default="Module",
        )

        return EditProposalInfo(
            id=edit.id,
            report_filename=edit.report_filename,
            module_title=module_title,
            editor_email=edit.editor.email,
            created_at=edit.created_at,
            status=edit.status,
            original_content=edit.original_content,
            proposed_content=edit.proposed_content,
            edit_type=edit.edit_type
        )

    # Try to find the edit in SharedCourseEdit
    course_edit = db.query(SharedCourseEdit).filter(SharedCourseEdit.id == edit_id).first()

    if course_edit:
        # This is a course edit
        is_owner = user.id == course_edit.shared_course.sharer_id
        is_admin = user.role == 'super_admin'
        if not is_owner and not is_admin:
            raise HTTPException(status_code=403, detail="You are not authorized to review this edit.")

        # Get the actual module/question title - never the randomized report filename.
        if course_edit.edit_type == 'question':
            try:
                question_data = json.loads(course_edit.original_content)
                module_title = question_data.get('question_text', 'Question')[:50]
            except (ValueError, TypeError, AttributeError):
                module_title = 'Question'
        else:
            module_title = module_display_label(
                course_edit.shared_course.sharer_id,
                course_edit.shared_course.course.source_doc_name,
                course_edit.report_filename,
                db,
                default="Module",
            )

        return EditProposalInfo(
            id=course_edit.id,
            report_filename=course_edit.report_filename,
            module_title=module_title,
            editor_email=course_edit.editor.email,
            created_at=course_edit.created_at,
            status=course_edit.status,
            original_content=course_edit.original_content,
            proposed_content=course_edit.proposed_content,
            edit_type=course_edit.edit_type
        )

    # If neither found, return 404
    raise HTTPException(status_code=404, detail="Edit proposal not found")


@router.post("/review/edits/{edit_id}/{action}", tags=["Collaboration & Sharing"])
def action_on_edit(edit_id: str, action: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Approves or rejects a pending edit proposal.

    If approved, the original content in S3 is updated and re-indexed.
    A notification is sent to the editor with the outcome.
    - **edit_id**: The UUID of the edit proposal.
    - **action**: Must be either `approve` or `reject`.
    - **Raises 404**: If the pending edit is not found.
    - **Raises 403**: If the user is not the owner of the content.
    """
    if action not in ['approve', 'reject']: 
        raise HTTPException(status_code=400, detail="Invalid action")
    
    # Try to find the edit in SharedReportEdit first
    edit = db.query(SharedReportEdit).filter(SharedReportEdit.id == edit_id, SharedReportEdit.status == 'pending').first()
    
    if edit:
        # Handle report edit
        is_owner = user.id == edit.shared_report.sharer_id
        is_admin = user.role == 'super_admin'
        if not is_owner and not is_admin: 
            raise HTTPException(status_code=403, detail="You are not authorized to review this edit.")
        
        # Name the MODULE in the notification: report_filename is a randomized storage key.
        module_label = module_display_label(
            edit.shared_report.sharer_id,
            edit.shared_report.source_doc_name,
            edit.report_filename,
            db,
        )
        if action == 'approve':
            edit.status = 'approved'
            msg = f"Your edits to '{module_label}' were approved by {user.email}."
            if edit.edit_type == 'module':
                report_key = f"{edit.shared_report.sharer_id}/generated_reports/{edit.report_filename}"
                s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=report_key, Body=edit.proposed_content.encode('utf-8'), ServerSideEncryption='AES256')
                _reindex_source_document(edit.shared_report.sharer_id, edit.shared_report.source_doc_name)
        else:
            edit.status = 'rejected'
            msg = f"Your edits to '{module_label}' were rejected by {user.email}."

        # A module share records a source document rather than a course, so the course is
        # resolved here: without it, deleting the course would leave this notification behind
        # pointing at a module that no longer exists.
        edited_course = db.query(Course).filter(
            Course.source_doc_name == edit.shared_report.source_doc_name,
            Course.author_id == edit.shared_report.sharer_id,
        ).first()
        notification = Notification(
            user_id=edit.editor_id,
            message=msg,
            course_id=course_notification_id(edited_course),
        )
        db.add(notification)
        db.commit()
        return {"message": f"Edit has been {action}d."}
    
    # Try to find the edit in SharedCourseEdit
    course_edit = db.query(SharedCourseEdit).filter(SharedCourseEdit.id == edit_id, SharedCourseEdit.status == 'pending').first()
    
    if course_edit:
        # Handle course edit
        is_owner = user.id == course_edit.shared_course.sharer_id
        is_admin = user.role == 'super_admin'
        if not is_owner and not is_admin:
            raise HTTPException(status_code=403, detail="You are not authorized to review this edit.")
        
        # Course/module names for the notification, never the randomized storage keys.
        edited_course = course_edit.shared_course.course
        course_display_name = course_display_label(edited_course)
        module_label = module_display_label(
            course_edit.shared_course.sharer_id,
            edited_course.source_doc_name,
            course_edit.report_filename,
            db,
        )
        # Default so an unexpected edit_type still produces a notification instead of a 500.
        msg = f"Your edit in course '{course_display_name}' was {action}d by {user.email}."
        if action == 'approve':
            course_edit.status = 'approved'

            if course_edit.edit_type == 'module':
                # Update module content
                report_key = f"{course_edit.shared_course.sharer_id}/generated_reports/{course_edit.report_filename}"
                s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=report_key, Body=course_edit.proposed_content.encode('utf-8'), ServerSideEncryption='AES256')
                _reindex_source_document(course_edit.shared_course.sharer_id, edited_course.source_doc_name)
                msg = f"Your edits to '{module_label}' in course '{course_display_name}' were approved by {user.email}."

            elif course_edit.edit_type == 'question':
                # Update assessment question
                try:
                    proposed_changes = json.loads(course_edit.proposed_content)
                    question_id = course_edit.report_filename.replace('question_', '')
                    
                    question = db.query(AssessmentQuestion).filter(AssessmentQuestion.id == question_id).first()
                    if question:
                        # Update question fields
                        if 'question_text' in proposed_changes:
                            question.question_text = proposed_changes['question_text']
                        if 'question_type' in proposed_changes:
                            question.question_type = proposed_changes['question_type']
                        if 'correct_answer' in proposed_changes:
                            question.correct_answer = proposed_changes['correct_answer']
                        if 'options' in proposed_changes and len(proposed_changes['options']) >= 4:
                            question.option_a = proposed_changes['options'][0]
                            question.option_b = proposed_changes['options'][1]
                            question.option_c = proposed_changes['options'][2]
                            question.option_d = proposed_changes['options'][3]
                        
                        msg = f"Your edits to an assessment question in course '{course_display_name}' were approved by {user.email}."
                    else:
                        msg = "Your question edit was approved but the question could not be updated."
                except Exception as e:
                    logger.error('Error updating question: %s', e)
                    msg = "Your question edit was approved but there was an error updating the question."
        else:
            course_edit.status = 'rejected'
            content_type = 'question' if course_edit.edit_type == 'question' else 'module'
            msg = f"Your edits to a {content_type} in course '{course_display_name}' were rejected by {user.email}."
        
        notification = Notification(
            user_id=course_edit.editor_id,
            message=msg,
            course_id=course_notification_id(edited_course),
        )
        db.add(notification)
        db.commit()
        return {"message": f"Edit has been {action}d."}
    
    # If neither found, return 404
    raise HTTPException(status_code=404, detail="Pending edit proposal not found")

@router.get("/review/pending-edits", tags=["Collaboration & Sharing"])
def get_pending_edits(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Get all pending edit proposals that the current user can review.
    Returns both SharedReportEdit and SharedCourseEdit proposals with actual module titles from the course.
    """
    try:
        pending_edits = []

        # Get pending report edits where user is the sharer
        report_edits = db.query(SharedReportEdit).join(
            SharedReport, SharedReportEdit.shared_report_id == SharedReport.id
        ).join(
            User, SharedReportEdit.editor_id == User.id
        ).filter(
            SharedReport.sharer_id == user.id,
            SharedReportEdit.status == 'pending'
        ).order_by(SharedReportEdit.created_at.desc()).all()

        for edit in report_edits:
            # Get the actual module title from the course's modules - never the
            # randomized report filename, which means nothing to the reviewer.
            module_title = module_display_label(
                edit.shared_report.sharer_id,
                edit.shared_report.source_doc_name,
                edit.report_filename,
                db,
                default="Module",
            )

            pending_edits.append({
                "id": edit.id,
                "type": "report",
                "title": f"Module Edit: {module_title}",
                "source_name": shared_source_display_label(
                    db, edit.shared_report.sharer_id, edit.shared_report.source_doc_name
                ),
                "editor_email": edit.editor.email,
                "created_at": edit.created_at.isoformat(),
                "edit_type": edit.edit_type,
                "module_title": module_title
            })

        # Get pending course edits where user is the sharer
        course_edits = db.query(SharedCourseEdit).join(
            SharedCourse, SharedCourseEdit.shared_course_id == SharedCourse.id
        ).join(
            Course, SharedCourse.course_id == Course.id
        ).join(
            User, SharedCourseEdit.editor_id == User.id
        ).filter(
            SharedCourse.sharer_id == user.id,
            SharedCourseEdit.status == 'pending'
        ).order_by(SharedCourseEdit.created_at.desc()).all()

        for edit in course_edits:
            if edit.edit_type == 'question':
                # For questions, try to extract the question text
                try:
                    question_data = json.loads(edit.original_content)
                    module_title = question_data.get('question_text', 'Question')[:50]
                except (ValueError, TypeError, AttributeError):
                    module_title = 'Question'
                title = f"Question Edit: {module_title}"
            else:
                # For module edits, get the actual module title from the course's modules
                module_title = module_display_label(
                    edit.shared_course.sharer_id,
                    edit.shared_course.course.source_doc_name,
                    edit.report_filename,
                    db,
                    default="Module",
                )
                title = f"Module Edit: {module_title}"

            pending_edits.append({
                "id": edit.id,
                "type": "course",
                "title": title,
                "source_name": course_display_label(edit.shared_course.course),
                "editor_email": edit.editor.email,
                "created_at": edit.created_at.isoformat(),
                "edit_type": edit.edit_type,
                "module_title": module_title
            })

        # Sort all edits by creation date (newest first)
        pending_edits.sort(key=lambda x: x['created_at'], reverse=True)

        return {
            "pending_edits": pending_edits,
            "total_count": len(pending_edits)
        }

    except Exception as e:
        logger.error('Error getting pending edits: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load pending edits")


class _AgentStreamFailure(Exception):
    """Placed on the async queue when the sync Bedrock/agent thread fails; carries user-visible text."""
    def __init__(self, user_message: str):
        self.user_message = user_message
        super().__init__(user_message)


def _user_message_for_agent_stream_error(exc: BaseException) -> str:
    """Map Bedrock/auth errors to actionable text for chat and tutor (raise-hand) streams."""
    text = str(exc)
    low = text.lower()
    if (
        "UnrecognizedClientException" in text
        or "InvalidClientTokenId" in text
        or "ExpiredTokenException" in text
        or "expiredtoken" in low
        or "security token" in low
        or "invalididentitytoken" in low
    ):
        return (
            "AWS authentication failed: Bedrock rejected the request (invalid or expired credentials). "
            "If you use temporary keys, set AWS_SESSION_TOKEN; if you use SSO, run `aws sso login`; "
            "otherwise update AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or your default AWS profile, then restart the server."
        )
    if "AccessDeniedException" in text:
        return (
            "AWS access denied for Bedrock. Check that this IAM user/role is allowed to invoke the configured model "
            f"in region {AWS_REGION}."
        )
    return "Sorry, I encountered an error. Please try again."

def _extract_json_object(text: str) -> Optional[dict]:
    """Best-effort JSON extraction from raw model output."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    json_blob = _find_outer_json_object(cleaned)
    if not json_blob:
        return None
    try:
        return json.loads(json_blob)
    except Exception:
        return None

def _tokenize_for_relevance(text: str) -> set:
    if not text:
        return set()
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    stop_words = {
        "the", "and", "for", "with", "this", "that", "from", "into", "about", "what", "when",
        "where", "which", "while", "your", "have", "has", "had", "can", "could", "would",
        "should", "will", "shall", "their", "there", "then", "than", "been", "being", "also",
        "table", "format", "give", "show", "explain", "topic", "module", "current", "question",
    }
    return {t for t in tokens if t not in stop_words}

def _lexical_relevance_override(query: str, module_context: str, module_title: Optional[str] = None) -> bool:
    """
    Non-hardcoded lexical safety net:
    if the query strongly overlaps with module title/content terms, treat as relevant.
    """
    q_tokens = _tokenize_for_relevance(query)
    if not q_tokens:
        return False
    context_tokens = _tokenize_for_relevance((module_title or "") + " " + (module_context or "")[:15000])
    if not context_tokens:
        return False
    overlap = q_tokens.intersection(context_tokens)
    # Require meaningful overlap ratio or at least two aligned topic tokens.
    return len(overlap) >= 2 or (len(overlap) / max(1, len(q_tokens))) >= 0.4

def _normalize_question_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())).strip()

def _token_jaccard_similarity(a: str, b: str) -> float:
    a_tokens = set(_normalize_question_text(a).split())
    b_tokens = set(_normalize_question_text(b).split())
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = len(a_tokens.intersection(b_tokens))
    union = len(a_tokens.union(b_tokens))
    return intersection / max(1, union)


_IMAGE_TAG_PREFIX = "[IMAGE:"
_IMAGE_URL_TAG_PREFIX = "[IMAGE_URL:"
_VISION_ANALYSIS_MARKERS = (
    "**VISION ANALYSIS FOR THIS IMAGE:**",
    "VISION ANALYSIS FOR THIS IMAGE:",
)
_VISION_ANALYSIS_STOP_PREFIXES = (
    "\n#",
    "\n[IMAGE_URL:",
    "\n[INTERACTIVE",
    "\n[MODULE_OVERVIEW]",
)


def _extract_image_tags(content: str) -> List[str]:
    """Extract S3 keys from [IMAGE: ...] tags (linear scan)."""
    if not content:
        return []
    keys: List[str] = []
    i = 0
    prefix = _IMAGE_TAG_PREFIX
    plen = len(prefix)
    while True:
        start = content.find(prefix, i)
        if start < 0:
            break
        j = start + plen
        while j < len(content) and content[j] in " \t":
            j += 1
        end = content.find("]", j)
        if end < 0:
            break
        keys.append(content[j:end].strip())
        i = end + 1
    return keys


def _replace_image_tags(content: str, build_replacement) -> str:
    """Replace each [IMAGE: key] tag using build_replacement(key) -> new tag text."""
    if not content:
        return content
    parts: List[str] = []
    i = 0
    prefix = _IMAGE_TAG_PREFIX
    plen = len(prefix)
    while True:
        start = content.find(prefix, i)
        if start < 0:
            parts.append(content[i:])
            break
        parts.append(content[i:start])
        j = start + plen
        while j < len(content) and content[j] in " \t":
            j += 1
        end = content.find("]", j)
        if end < 0:
            parts.append(content[start:])
            break
        key = content[j:end].strip()
        parts.append(build_replacement(key))
        i = end + 1
    return "".join(parts)


def _extract_markdown_image_urls(content: str) -> List[str]:
    """Extract URLs from markdown ![alt](url) tags (linear scan)."""
    if not content:
        return []
    urls: List[str] = []
    i = 0
    while True:
        start = content.find("![", i)
        if start < 0:
            break
        alt_end = content.find("](", start + 2)
        if alt_end < 0:
            break
        url_start = alt_end + 2
        url_end = content.find(")", url_start)
        if url_end < 0:
            break
        urls.append(content[url_start:url_end].strip())
        i = url_end + 1
    return urls


def _transform_markdown_images(content: str, transform) -> str:
    """transform(alt, src) -> replacement markdown image string."""
    if not content:
        return content
    parts: List[str] = []
    i = 0
    while True:
        start = content.find("![", i)
        if start < 0:
            parts.append(content[i:])
            break
        parts.append(content[i:start])
        alt_end = content.find("](", start + 2)
        if alt_end < 0:
            parts.append(content[start:])
            break
        alt = content[start + 2 : alt_end]
        url_start = alt_end + 2
        url_end = content.find(")", url_start)
        if url_end < 0:
            parts.append(content[start:])
            break
        src = content[url_start:url_end].strip()
        parts.append(transform(alt, src))
        i = url_end + 1
    return "".join(parts)


def _strip_non_api_markdown_images(content: str) -> str:
    """Remove markdown images whose URL is not under /api/images/."""
    def _keep_or_drop(alt: str, src: str) -> str:
        if src.startswith("/api/images/"):
            return f"![{alt}]({src})"
        return ""

    return _transform_markdown_images(content, _keep_or_drop)


def _strip_vision_analysis_blocks(content: str) -> str:
    """Remove internal VISION ANALYSIS sections without backtracking regex."""
    if not content:
        return content
    result = content
    for marker in _VISION_ANALYSIS_MARKERS:
        while True:
            start = result.find(marker)
            if start < 0:
                break
            end = len(result)
            for prefix in _VISION_ANALYSIS_STOP_PREFIXES:
                pos = result.find(prefix, start + len(marker))
                if pos >= 0:
                    end = min(end, pos)
            result = result[:start] + result[end:]
    return result


def _parse_kb_search_snippets(tool_output: str) -> List[dict]:
    """Parse knowledge-base tool output into content/source pairs."""
    if not tool_output:
        return []
    if tool_output.startswith("Found the following information"):
        newline = tool_output.find("\n")
        tool_output = tool_output[newline + 1 :] if newline >= 0 else ""
    snippets: List[dict] = []
    content_marker = "Content:"
    source_marker = "Source:"
    i = 0
    while i < len(tool_output):
        content_pos = tool_output.find(content_marker, i)
        if content_pos < 0:
            break
        content_start = content_pos + len(content_marker)
        source_pos = tool_output.find(source_marker, content_start)
        if source_pos < 0:
            snippets.append(
                {"content": tool_output[content_start:].strip(), "source": "Unknown Source"}
            )
            break
        content = tool_output[content_start:source_pos].strip()
        source_start = source_pos + len(source_marker)
        next_content = tool_output.find(content_marker, source_start)
        next_sep = tool_output.find("\n\n---", source_start)
        end_positions = [p for p in (next_content, next_sep) if p >= 0]
        source_end = min(end_positions) if end_positions else len(tool_output)
        source = tool_output[source_start:source_end].strip()
        snippets.append({"content": content, "source": source or "Unknown Source"})
        i = source_end
    return snippets


def _extract_quoted_phrases(text: str, min_len: int = 2, max_len: int = 80) -> List[str]:
    phrases: List[str] = []
    for quote in ('"', "'"):
        i = 0
        while i < len(text):
            start = text.find(quote, i)
            if start < 0:
                break
            end = text.find(quote, start + 1)
            if end < 0:
                break
            phrase = text[start + 1 : end]
            if min_len <= len(phrase) <= max_len:
                phrases.append(phrase)
            i = end + 1
    return phrases


def _extract_uppercase_tokens(text: str, min_tail: int = 1, max_tail: int = 19) -> List[str]:
    tokens: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if not ch.isupper():
            i += 1
            continue
        j = i + 1
        while j < n and (text[j].isupper() or text[j].isdigit() or text[j] in "_-"):
            j += 1
        if text[i].isupper() and min_tail <= (j - i - 1) <= max_tail:
            token = text[i:j]
            if j >= n or not text[j].isalnum():
                tokens.append(token)
        i = j
    return tokens


def _extract_focus_phrase_after_preposition(query: str, max_len: int = 80) -> List[str]:
    prepositions = ("about", "on", "regarding", "for")
    phrases: List[str] = []
    lower = query.lower()
    for prep in prepositions:
        needle = f" {prep} "
        start = 0
        while True:
            pos = lower.find(needle, start)
            if pos < 0:
                break
            i = pos + len(needle)
            j = i
            while j < len(query) and j - i < max_len:
                ch = query[j]
                if ch.isalpha() or ch.isdigit() or ch in "_/- ":
                    j += 1
                else:
                    break
            phrase = query[i:j].strip()
            if len(phrase) >= 2:
                for sep in (" with ", " and ", " but ", " if ", " when ", " where ", " why ", " how "):
                    idx = phrase.lower().find(sep)
                    if idx >= 0:
                        phrase = phrase[:idx]
                for ch in "?.!,;:":
                    if ch in phrase:
                        phrase = phrase.split(ch, 1)[0]
                phrase = phrase.strip()
                if len(phrase) >= 2:
                    phrases.append(phrase)
            start = pos + 1
    return phrases


def _split_sentences(text: str) -> List[str]:
    chunks: List[str] = []
    start = 0
    for idx, ch in enumerate(text):
        if ch in ".!?" and (idx + 1 >= len(text) or text[idx + 1].isspace()):
            segment = text[start : idx + 1].strip()
            if segment:
                chunks.append(segment)
            start = idx + 1
    tail = text[start:].strip()
    if tail:
        chunks.append(tail)
    return chunks


def _find_outer_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for idx in range(start, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _extract_image_url_tags(content: str) -> List[str]:
    """Extract URLs from [IMAGE_URL: ...] tags (linear scan, no regex backtracking)."""
    if not content:
        return []
    urls: List[str] = []
    i = 0
    prefix = _IMAGE_URL_TAG_PREFIX
    plen = len(prefix)
    while True:
        start = content.find(prefix, i)
        if start < 0:
            break
        j = start + plen
        while j < len(content) and content[j] in " \t":
            j += 1
        end = content.find("]", j)
        if end < 0:
            break
        urls.append(content[j:end].strip())
        i = end + 1
    return urls


def _strip_invalid_image_url_tags(content: str, valid_urls: set[str]) -> str:
    """Remove [IMAGE_URL: ...] tags whose URL is not in valid_urls; keep others unchanged."""
    if not content:
        return content
    parts: List[str] = []
    i = 0
    prefix = _IMAGE_URL_TAG_PREFIX
    plen = len(prefix)
    while True:
        start = content.find(prefix, i)
        if start < 0:
            parts.append(content[i:])
            break
        parts.append(content[i:start])
        j = start + plen
        while j < len(content) and content[j] in " \t":
            j += 1
        end = content.find("]", j)
        if end < 0:
            parts.append(content[start:])
            break
        url = content[j:end].strip()
        if url in valid_urls:
            parts.append(content[start : end + 1])
        else:
            logger.warning('[TUTOR] Removing fake IMAGE_URL tag: %s', ("" if url is None else str(url).replace("\n", "").replace("\r", "")))
        i = end + 1
    return "".join(parts)


def _prepare_raise_hand_context(module_context: Optional[str]) -> dict:
    """
    Normalize tutor raise-hand module context by converting image references
    into frontend-renderable [IMAGE_URL: /api/images/s3/... ] tags.
    """
    text = module_context or ""
    if not text:
        return {"context": "", "image_urls": []}

    image_urls: List[str] = []
    seen_urls = set()

    def _to_image_url(raw_key: str) -> str:
        from utils.security_helpers import strip_surrounding_quotes

        key = strip_surrounding_quotes((raw_key or "").strip())
        key = key.replace("\\", "/")
        if key.startswith("/api/images/s3/"):
            return key
        if key.startswith("/"):
            key = key[1:]
        return f"/api/images/s3/{key}"

    def _register(url: str):
        if url and url not in seen_urls:
            seen_urls.add(url)
            image_urls.append(url)

    def _image_tag_to_url(s3_key: str) -> str:
        image_url = _to_image_url(s3_key)
        _register(image_url)
        return f"[IMAGE_URL: {image_url}]"

    transformed = _replace_image_tags(text, _image_tag_to_url)

    def _markdown_image_to_api(alt: str, src: str) -> str:
        src = (src or "").strip()
        if src.startswith("http://") or src.startswith("https://"):
            return f"![{alt}]({src})"
        image_url = _to_image_url(src)
        _register(image_url)
        return f"![{alt}]({image_url})"

    transformed = _transform_markdown_images(transformed, _markdown_image_to_api)

    # Register any existing IMAGE_URL tags too, so we can pass the valid list in prompt instructions.
    existing = _extract_image_url_tags(transformed)
    for url in existing:
        _register(url.strip())

    return {"context": transformed, "image_urls": image_urls}

def _is_module_guided_question(query: str, module_context: str, module_title: Optional[str] = None) -> bool:
    """
    Accept common tutor-style guided questions to avoid false negatives after suggestions are shown.
    """
    cleaned_query = _normalize_question_text(query)
    if not cleaned_query:
        return False

    stable_suggestions = _build_dynamic_module_suggestions(
        module_title,
        module_context,
        query=None,  # stable baseline suggestions independent of user phrasing
        max_suggestions=10
    )

    # Direct or near-direct match with any guided suggestion.
    for suggested in stable_suggestions:
        if cleaned_query == _normalize_question_text(suggested):
            return True
        if _token_jaccard_similarity(cleaned_query, suggested) >= 0.5:
            return True

    # Allow pedagogical "how/why/when/can" style questions with at least minimal module overlap.
    pedagogical_intent = bool(re.search(r"\b(how|why|when|where|what|can|should|explain|apply|example|use case)\b", cleaned_query))
    if pedagogical_intent:
        q_tokens = _tokenize_for_relevance(query)
        context_tokens = _tokenize_for_relevance((module_title or "") + " " + (module_context or "")[:15000])
        overlap = q_tokens.intersection(context_tokens)
        if len(overlap) >= 1:
            return True
    return False

def _extract_query_focus_anchors(query: str) -> List[str]:
    """Extract likely focus anchors from learner question (generic, no hardcoding)."""
    q = (query or "").strip()
    if not q:
        return []
    anchors: List[str] = []
    seen = set()

    def _add_anchor(value: str):
        clean = re.sub(r"\s+", " ", (value or "").strip(" \"'`.,:;!?()[]{}"))
        if not clean:
            return
        key = clean.lower()
        if key in seen:
            return
        seen.add(key)
        anchors.append(clean)

    for phrase in _extract_quoted_phrases(q, min_len=2, max_len=80):
        _add_anchor(phrase)

    for token in _extract_uppercase_tokens(q, min_tail=1, max_tail=12):
        _add_anchor(token)

    for phrase in _extract_focus_phrase_after_preposition(q, max_len=80):
        _add_anchor(phrase)

    return anchors

def _has_strong_focus_anchor_match(query: str, module_context: str, module_title: Optional[str] = None) -> bool:
    """
    Deterministic mismatch gate for raise-hand:
    If query contains strong anchors (acronyms/entities/quoted terms), at least one must
    appear in module title/context; otherwise treat as off-topic.
    """
    anchors = _extract_query_focus_anchors(query)
    if not anchors:
        return True

    combined = f"{module_title or ''} {module_context or ''}".lower()
    strong_anchors: List[str] = []
    for anchor in anchors:
        a = anchor.strip()
        # Strong anchors: compact entity-like tokens (e.g., MCP, OAuth2, gRPC, REST),
        # or explicit quoted phrases from the query.
        is_compact_entity = bool(re.fullmatch(r"[A-Za-z0-9_-]{2,16}", a))
        has_upper_or_digit = any(ch.isupper() for ch in a) or any(ch.isdigit() for ch in a)
        if (is_compact_entity and has_upper_or_digit) or (len(a.split()) <= 4 and " " in a):
            strong_anchors.append(a)

    if not strong_anchors:
        return True

    for anchor in strong_anchors:
        pattern = rf"\b{re.escape(anchor.lower())}\b"
        if re.search(pattern, combined):
            return True
    return False

def _extract_module_focus_anchors(module_context: str, module_title: Optional[str] = None, max_anchors: int = 5) -> List[str]:
    """
    Extract dominant module focus anchors in a generic way:
    - Prioritize compact entities/acronyms from title and section headings.
    - Backfill with high-frequency compact entities in module text.
    """
    title = (module_title or "").strip()
    context = module_context or ""
    combined = f"{title}\n{context}"

    # Capture heading lines and short title-like lines as stronger focus signals.
    heading_candidates = []
    for raw_line in combined.splitlines()[:240]:
        line = (raw_line or "").strip()
        if not line:
            continue
        if line.startswith("#") or line.endswith(":") or len(line.split()) <= 8:
            heading_candidates.append(line)

    scored: Dict[str, float] = {}

    def _score_anchor(anchor: str, boost: float):
        key = (anchor or "").strip()
        if not key:
            return
        # Keep compact technical entities only.
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,20}", key):
            return
        if not (any(ch.isupper() for ch in key) or any(ch.isdigit() for ch in key)):
            return
        scored[key.upper()] = scored.get(key.upper(), 0.0) + boost

    for token in _extract_uppercase_tokens(title, min_tail=1, max_tail=19):
        _score_anchor(token, 8.0)

    for line in heading_candidates:
        for token in _extract_uppercase_tokens(line, min_tail=1, max_tail=19):
            _score_anchor(token, 4.0)

    freq: Dict[str, int] = {}
    for token in _extract_uppercase_tokens(context, min_tail=1, max_tail=19):
        key = token.upper()
        freq[key] = freq.get(key, 0) + 1
    for token, count in freq.items():
        _score_anchor(token, min(3.0, float(count)))

    ranked = sorted(scored.keys(), key=lambda t: (-scored[t], t))
    return ranked[:max_anchors]

def _is_primary_focus_mismatch(query: str, module_context: str, module_title: Optional[str] = None) -> dict:
    """
    Detect mismatch when learner asks for a specific entity/topic that is not part
    of the module's dominant focus anchors (generic, no hardcoded terms).
    """
    query_anchors = _extract_query_focus_anchors(query)
    if not query_anchors:
        return {"is_mismatch": False, "requested": [], "module_focus": []}

    strong_query: List[str] = []
    for anchor in query_anchors:
        a = (anchor or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{2,20}", a) and (any(ch.isupper() for ch in a) or any(ch.isdigit() for ch in a)):
            strong_query.append(a.upper())

    if not strong_query:
        return {"is_mismatch": False, "requested": [], "module_focus": []}

    module_focus = _extract_module_focus_anchors(module_context, module_title, max_anchors=6)
    if not module_focus:
        return {"is_mismatch": False, "requested": strong_query, "module_focus": []}

    module_focus_set = set(module_focus)
    requested_in_primary_focus = [a for a in strong_query if a in module_focus_set]
    requested_in_context = []
    combined_lower = f"{module_title or ''} {module_context or ''}".lower()
    for anchor in strong_query:
        if re.search(rf"\b{re.escape(anchor.lower())}\b", combined_lower):
            requested_in_context.append(anchor)

    # Mismatch rule:
    # - learner requested explicit strong anchors
    # - none are in module primary focus
    # - and module has at least one different primary focus anchor
    if not requested_in_primary_focus and module_focus:
        return {
            "is_mismatch": True,
            "requested": strong_query,
            "requested_in_context": requested_in_context,
            "module_focus": module_focus,
        }
    return {
        "is_mismatch": False,
        "requested": strong_query,
        "requested_in_context": requested_in_context,
        "module_focus": module_focus,
    }

def _build_query_focus_instruction(query: str) -> str:
    """
    Build a generic focus guard from the learner's question (no hardcoded terms).
    It extracts likely intent anchors (quoted phrases, acronyms, key entities) and
    tells the tutor to stay on those anchors unless comparison is explicitly asked.
    """
    anchors = _extract_query_focus_anchors(query)
    if not anchors:
        return ""

    # Keep instruction concise and stable.
    anchors = anchors[:4]
    focus = ", ".join(anchors)
    return (
        "CRITICAL FOCUS RULE:\n"
        f"- The learner explicitly asked about: {focus}.\n"
        "- Keep the answer centered on these requested topics/entities.\n"
        "- Do NOT switch to a different primary topic unless the learner asks to compare/expand.\n"
        "- If module context is ambiguous, state that briefly and still answer for the requested focus.\n\n"
    )

def _extract_module_topics(module_context: str, module_title: Optional[str] = None, max_topics: int = 12) -> List[str]:
    """
    Extract high-signal topic terms from module title/content for dynamic suggestions.
    """
    combined_text = f"{(module_title or '')} {(module_context or '')}".lower()
    tokens = [
        word
        for word in re.split(r"[^a-z0-9_-]+", combined_text)
        if len(word) >= 4 and word[0].isalpha()
    ]
    stop_words = {
        "this", "that", "with", "from", "into", "about", "where", "when", "which", "while",
        "what", "your", "have", "been", "being", "will", "would", "should", "could", "also",
        "module", "current", "question", "content", "using", "used", "between", "through",
        "their", "there", "such", "than", "then", "more", "most", "some", "many", "other",
        "into", "across", "under", "over", "within", "without", "each", "both", "make",
        "made", "does", "did", "done", "what", "why", "how", "can", "our", "out", "are",
        "the", "and", "for", "not", "you", "they", "them", "his", "her",
        "phase", "phases", "section", "sections", "chapter", "chapters", "module", "modules",
        "blueprint", "toc", "introduction", "overview", "classification",
    }
    counts: Dict[str, int] = {}
    first_seen: Dict[str, int] = {}
    for idx, token in enumerate(tokens):
        if token in stop_words or token.isdigit():
            continue
        if _is_structural_topic_phrase(token):
            continue
        if len(token) <= 3:
            continue
        counts[token] = counts.get(token, 0) + 1
        if token not in first_seen:
            first_seen[token] = idx
    ranked = sorted(counts.keys(), key=lambda t: (-counts[t], first_seen[t], t))
    return ranked[:max_topics]

def _split_instructional_line_chunks(text: str) -> List[str]:
    """Split instructional lines on |, /, ;, spaced dashes, or bullets (no regex)."""
    parts = [text]
    for separator in (" \u2022 ", " - ", "|", "/", ";"):
        next_parts: List[str] = []
        for part in parts:
            if separator in part:
                next_parts.extend(part.split(separator))
            else:
                next_parts.append(part)
        parts = next_parts
    return [part.strip() for part in parts if part.strip()]


def _is_structural_numbered_label(text: str) -> bool:
    """Match labels like 'section 2', 'phase-1', 'module:3' without regex."""
    prefixes = ("phase", "section", "chapter", "part", "module")
    for prefix in prefixes:
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix) :].lstrip()
        if not rest:
            continue
        if rest[0] in "-:":
            rest = rest[1:].lstrip()
        number = ""
        for ch in rest:
            if ch.isdigit():
                number += ch
            else:
                break
        if number and (len(rest) == len(number) or not rest[len(number)].isalnum()):
            return True
    return False


def _is_structural_topic_phrase(value: str) -> bool:
    """
    Filter non-technical scaffolding terms so suggestions stay technical.
    """
    text = (value or "").strip().lower()
    if not text:
        return True
    structural_terms = {
        "module", "modules", "section", "sections", "chapter", "chapters", "part", "parts",
        "phase", "phases", "blueprint", "toc", "table of contents", "introduction", "overview",
        "summary", "conclusion", "roadmap", "agenda", "index", "lifecycle stage",
        "module classification", "architecture diagram", "challenge table", "final thoughts"
    }
    if text in structural_terms:
        return True
    if _is_structural_numbered_label(text):
        return True
    if any(term in text for term in ("table of contents", "toc", "blueprint")):
        return True
    if any(
        phrase in text
        for phrase in ("module classification", "architecture diagram", "challenge table")
    ):
        return True
    if text.startswith(("introduction", "overview", "summary", "conclusion")):
        return True
    return False

def _clean_suggestion_text(value: str) -> str:
    from utils.security_helpers import strip_surrounding_quotes

    text = strip_surrounding_quotes((value or "").strip(), quote_chars="\"'`")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def _extract_module_key_phrases(module_context: str, module_title: Optional[str] = None, max_phrases: int = 10) -> List[str]:
    """
    Pull concise, high-signal phrases from title + module headings/content lines.
    This keeps suggestions grounded in the actual module wording.
    """
    title = (module_title or "").strip()
    context = module_context or ""
    raw_lines = [title] if title else []
    raw_lines.extend(context.splitlines())

    phrases: List[str] = []
    seen = set()

    def _add_phrase(value: str):
        phrase = re.sub(r"\s+", " ", (value or "").strip(" -:;,.[](){}\"'`"))
        phrase = re.sub(r"^\s*(?:\d+[\.\):\-]\s*|[ivxlcdm]+[\.\):\-]\s*)", "", phrase, flags=re.IGNORECASE)
        if len(phrase) < 4 or len(phrase) > 80:
            return
        # Exclude lines that are mostly punctuation or numbers.
        if not re.search(r"[A-Za-z]", phrase):
            return
        if _is_structural_topic_phrase(phrase):
            return
        key = phrase.lower()
        if key in seen:
            return
        seen.add(key)
        phrases.append(phrase)

    for line in raw_lines:
        clean = (line or "").strip()
        if not clean:
            continue
        # Prefer headings, short semantic lines, and list lead-ins.
        if clean.startswith("#") or clean.endswith(":") or len(clean.split()) <= 9:
            _add_phrase(clean.lstrip("#").strip())
            continue
        # Capture phrase chunks around separators from longer instructional lines.
        chunks = _split_instructional_line_chunks(clean)
        for chunk in chunks[:3]:
            chunk = chunk.strip()
            if 3 <= len(chunk.split()) <= 10:
                _add_phrase(chunk)
        if len(phrases) >= max_phrases:
            break

    return phrases[:max_phrases]

def _build_dynamic_module_suggestions(
    module_title: Optional[str],
    module_context: str,
    query: Optional[str] = None,
    max_suggestions: int = 3
) -> List[str]:
    """
    Build rotating, module-grounded suggestion prompts.
    """
    safe_title = (module_title or "this module").strip()
    topics = _extract_module_topics(module_context, module_title, max_topics=16)
    key_phrases = _extract_module_key_phrases(module_context, module_title, max_phrases=12)

    seed_source = f"{safe_title}|{(query or '').strip()}|{len(module_context or '')}"
    seed_int = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:8], 16)

    topic_pool = topics[:8] if topics else []
    if topic_pool:
        topic_offset = seed_int % len(topic_pool)
        topic_pool = topic_pool[topic_offset:] + topic_pool[:topic_offset]

    generic_templates = [
        f"What are the most important technical concepts in {safe_title}?",
        f"How do the core mechanisms in {safe_title} work end-to-end?",
        f"What practical trade-offs should I consider when implementing ideas from {safe_title}?",
    ]
    topic_templates = [
        "How does {topic} work technically?",
        "Can you explain {topic} with a practical example?",
        "What are common implementation challenges or mistakes with {topic}?",
        "How does {topic} interact with related components in the system?",
        "When should we apply {topic} and what constraints matter most?",
    ]
    phrase_templates = [
        "Can you explain the technical details of {phrase}?",
        "How is {phrase} implemented and validated in practice?",
        "What are the key engineering trade-offs in {phrase}?",
        "Can you compare {phrase} with closely related technical approaches?",
    ]

    candidates: List[str] = []
    if key_phrases:
        for idx, phrase in enumerate(key_phrases[:max_suggestions * 3]):
            template = phrase_templates[(seed_int + idx) % len(phrase_templates)]
            candidates.append(template.format(phrase=phrase))
    if topic_pool:
        for idx, topic in enumerate(topic_pool[:max_suggestions * 3]):
            template = topic_templates[(seed_int + idx) % len(topic_templates)]
            candidates.append(template.format(topic=topic.replace("_", " ")))
    candidates.extend(generic_templates)

    seen = set()
    deduped: List[str] = []
    for item in candidates:
        normalized = _clean_suggestion_text(item)
        if not normalized:
            continue
        if _is_structural_topic_phrase(normalized):
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
        if len(deduped) >= max_suggestions:
            break
    return deduped

def validate_module_question_relevance(query: str, module_context: str, module_title: Optional[str] = None) -> dict:
    """
    Validate whether a tutor raise-hand question matches the current module.
    Returns dict: {is_relevant: bool, message: str, suggestions: List[str]}
    """
    safe_title = (module_title or "this module").strip()
    context_snippet = (module_context or "")
    relevance_prompt = f"""You are a strict tutor relevance checker.

Task:
- Decide if the student's question is about the CURRENT module content.
- If not relevant, provide a short polite redirection and 3 suggested module-specific questions.

Output format (JSON only):
{{
  "is_relevant": true or false,
  "message": "short message",
  "suggestions": ["q1", "q2", "q3"]
}}

Rules:
- Mark as relevant only when the question is clearly connected to the module's concepts.
- If ambiguous but likely related, mark relevant.
- If unrelated to module topics, mark not relevant and suggestions must be grounded in module content.
- Keep suggestions concise and practical.

Module title: {safe_title}
Student question: {query}

Module content:
---
{context_snippet}
---
"""
    try:
        raw = tutor_agent.run(relevance_prompt).content or ""
        parsed = _extract_json_object(raw) or {}
        is_relevant = bool(parsed.get("is_relevant", True))
        message = (parsed.get("message") or "").strip()
        suggestions = parsed.get("suggestions") if isinstance(parsed.get("suggestions"), list) else []
        normalized_suggestions = [str(s).strip() for s in suggestions if str(s).strip()]
        dynamic_suggestions = _build_dynamic_module_suggestions(module_title, module_context, query=query, max_suggestions=5)
        # Keep model suggestions if useful, but always prioritize dynamic module-grounded prompts.
        merged_suggestions: List[str] = []
        seen = set()
        for suggestion in dynamic_suggestions + normalized_suggestions:
            key = suggestion.lower()
            if key in seen:
                continue
            seen.add(key)
            merged_suggestions.append(suggestion)
            if len(merged_suggestions) >= 5:
                break
        result = {
            "is_relevant": is_relevant,
            "message": message,
            "suggestions": merged_suggestions,
        }
        # Stronger deterministic guard:
        # if learner asks for a specific explicit entity but module's dominant focus
        # is different, return context mismatch suggestions.
        primary_focus_check = _is_primary_focus_mismatch(query, module_context, module_title)
        if primary_focus_check.get("is_mismatch"):
            requested = primary_focus_check.get("requested", [])[:2]
            module_focus = primary_focus_check.get("module_focus", [])[:2]
            requested_text = ", ".join(requested) if requested else "the requested topic"
            module_focus_text = ", ".join(module_focus) if module_focus else safe_title
            logger.info(
                "[TUTOR RELEVANCE] Primary focus mismatch detected. requested=%s module_focus=%s",
                requested_text,
                module_focus_text
            )
            result["is_relevant"] = False
            result["message"] = (
                f"Context mismatch: your question focuses on {requested_text}, "
                f"but this module is primarily about {module_focus_text}."
            )
            if not result["suggestions"]:
                result["suggestions"] = _build_dynamic_module_suggestions(module_title, module_context, query=query, max_suggestions=5)
            return result
        # Deterministic guard: if strong query anchors are absent in current module context,
        # treat as mismatch and return suggestions instead of generating wrong-topic answers.
        if not _has_strong_focus_anchor_match(query, module_context, module_title):
            logger.info("[TUTOR RELEVANCE] Strong focus anchor mismatch detected; redirecting to suggestions")
            result["is_relevant"] = False
            result["message"] = f"Your question appears to be outside the current module focus ({safe_title})."
            if not result["suggestions"]:
                result["suggestions"] = _build_dynamic_module_suggestions(module_title, module_context, query=query, max_suggestions=5)
            return result
        if not result["is_relevant"] and _lexical_relevance_override(query, module_context, module_title):
            logger.info("[TUTOR RELEVANCE] Overriding to relevant based on lexical overlap")
            result["is_relevant"] = True
            result["message"] = ""
            result["suggestions"] = []
        if not result["is_relevant"] and _is_module_guided_question(query, module_context, module_title):
            logger.info("[TUTOR RELEVANCE] Overriding to relevant for guided module-style question")
            result["is_relevant"] = True
            result["message"] = ""
            result["suggestions"] = []
        return result
    except Exception as e:
        logger.warning('[TUTOR RELEVANCE] Relevance check failed, allowing query: %s', e)
        return {"is_relevant": True, "message": "", "suggestions": []}

def build_module_mismatch_response(
    module_title: Optional[str],
    suggestions: List[str],
    custom_message: Optional[str] = None,
    module_context: str = "",
    query: Optional[str] = None
) -> str:
    safe_title = (module_title or "current module").strip()
    lead = (custom_message or f"Please ask questions related to the current module: {safe_title}.").strip()
    if not lead.endswith("."):
        lead += "."
    final_suggestions = [s for s in (suggestions or []) if str(s).strip()]
    if not final_suggestions:
        final_suggestions = _build_dynamic_module_suggestions(
            module_title,
            module_context,
            query=query,
            max_suggestions=5
        )
    bullet_block = "\n".join([f"- {q}" for q in final_suggestions[:5]])
    return f"{lead}\n\nYou can ask questions like:\n{bullet_block}"


# --- Chat & AI Agent Endpoints ---
async def stream_agent_response(
    agent: Agent,
    full_prompt: str,
    history: List[Dict],
    tools: List,
    session_id: str,
    sources_list_ref: List,
    chat_type: Optional[str] = None,
    image_namespace_user_id: Optional[str] = None,
) -> AsyncGenerator[bytes, None]:
    """
    An async generator to stream agent responses and handle post-processing.

    Takes no session. It used to be handed the caller's ``Depends(get_db)`` session, which it
    only ever used in the ``finally`` block below - after the last chunk had been streamed - yet
    that alone kept a pooled connection checked out for the whole model answer, because FastAPI
    releases a yield-dependency only once the response is finished. With the streaming chat
    endpoints being `async def`, nothing capped how many of those ran at once, so a class using
    the Chat Agent together drained the pool and unrelated requests failed with "QueuePool limit
    of size 20 overflow 10 reached". The two short writes at the end now take a connection of
    their own and give it straight back.
    """
    full_response_content = ""
    streamed_chunks = []  # Collect all chunks for post-processing
    stream_state = {"failed": False}
    q = asyncio.Queue()
    # One agent per request. Agno's run path forwards run() kwargs into get_run_messages, so tools=
    # cannot be passed to run() without duplicating the keyword and raising TypeError - the tools
    # have to live on the agent. Every chat entry point passes the same shared smart_tutor_agent
    # here, so assigning them onto it needed a global lock, and that lock made the whole chat
    # feature single-file: a trainee's question waited for every earlier question to finish
    # generating, which at cohort size overran the browser's fetch deadline and was reported as a
    # timeout. The copy is per-request state instead, so the runs no longer wait for each other.
    run_agent = clone_agent_for_request(agent, tools)
    def run_sync_stream():
        """This function will run in a separate thread."""
        try:
            for chunk in run_agent.run(full_prompt, history=history, stream=True):
                asyncio.run(q.put(chunk))
        except Exception as e:
            stream_state["failed"] = True
            logger.error("Error in agent's synchronous stream thread: %s", e, exc_info=True)
            asyncio.run(q.put(_AgentStreamFailure(_user_message_for_agent_stream_error(e))))
        finally:
            asyncio.run(q.put(None))
    loop = asyncio.get_event_loop()
    # Run the agent in a copy of *this* request's context. run_in_executor() hands the callable to a
    # bare worker thread, which starts with an empty context, so without this copy the tools would
    # read an empty tool_context.user_id and search nobody's namespace. copy_context() is evaluated
    # here, inside the request, so the copy carries the identity begin_agent_tool_context() set.
    loop.run_in_executor(None, copy_context().run, run_sync_stream)
    try:
        while True:
            chunk = await q.get()
            if chunk is None: break
            if isinstance(chunk, _AgentStreamFailure):
                stream_state["failed"] = True
                yield chunk.user_message.encode('utf-8')
                continue
            if hasattr(chunk, 'content') and chunk.content:
                full_response_content += chunk.content
                streamed_chunks.append(chunk.content)
                yield chunk.content.encode('utf-8')
            if hasattr(chunk, 'tool_calls') and chunk.tool_calls:
                for call in chunk.tool_calls:
                    if call.tool_name in ['search_knowledge_base', 'search_video_analysis_reports']:
                        tool_output = call.output or ""
                        if call.tool_name == 'search_video_analysis_reports':
                            tool_output = re.sub(
                                r"^Found the following information[^\n]*\n+",
                                "",
                                tool_output,
                                count=1,
                            )
                        for snippet in _parse_kb_search_snippets(tool_output):
                            sources_list_ref.append(snippet)
    except Exception as e:
        stream_state["failed"] = True
        logger.error('Error processing agent stream from queue: %s', e, exc_info=True)
        yield "Sorry, I encountered an error. Please try again.".encode('utf-8')
    finally:
        try:
            # CRITICAL FIX: Filter out non-existent images from final response before saving
            # This ensures the database doesn't store invalid image references
            # Note: We filter after streaming to avoid blocking the stream, but this means
            # invalid images may briefly appear to the user before being filtered on reload
            if chat_type == "video_analysis" and image_namespace_user_id:
                from services.video_analysis_kb import (
                    ensure_visual_images_in_response,
                    sanitize_video_analysis_response,
                    video_analysis_tool_context,
                )
                from utils.video_analysis.chat_helpers import (
                    is_video_analysis_no_match_reply,
                    normalize_video_analysis_no_match_reply,
                )

                if not sources_list_ref and getattr(
                    video_analysis_tool_context, "last_search_sources", None
                ):
                    sources_list_ref.extend(
                        video_analysis_tool_context.last_search_sources
                    )

                filtered_response = sanitize_video_analysis_response(
                    full_response_content, image_namespace_user_id
                )
                filtered_response = ensure_visual_images_in_response(filtered_response)
                if is_video_analysis_no_match_reply(filtered_response):
                    sources_list_ref.clear()
                    video_analysis_tool_context.last_search_sources = []
                    video_analysis_tool_context.valid_image_keys = []
                    filtered_response = normalize_video_analysis_no_match_reply(
                        filtered_response
                    )
            else:
                filtered_response = filter_non_existent_images(full_response_content)
            # Phani | 25-Jun-2026 | DF_27 | Trainee_study | Low
            # Fix: Save and serve chat answers without agent meta preamble text.
            filtered_response = strip_agent_meta_commentary(filtered_response)
            if stream_state.get("failed") and not filtered_response.strip():
                return

            # CRITICAL FIX: Track chat agent token usage for analytics
            # Estimate tokens from prompt + response for cost tracking
            estimated_tokens = len(full_prompt.split()) + len(filtered_response.split())

            # A connection is taken only here, for the turn's two short writes, and handed back
            # on the way out of the block. Rollback on failure is session_scope's job.
            with session_scope() as db:
                # Get user_id from session to track tokens
                session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
                if session:
                    # Estimate input/output token distribution for chat
                    # Use full_prompt instead of req.query since req is not available in this function
                    estimated_input_tokens = len(full_prompt.split()) + 500  # Query + context
                    # Calculate output tokens with proper bounds checking
                    if estimated_tokens > estimated_input_tokens:
                        estimated_output_tokens = estimated_tokens - estimated_input_tokens
                    else:
                        estimated_output_tokens = estimated_tokens // 3

                    try:
                        track_token_usage(db, session.user_id, "chat_agent", estimated_input_tokens, estimated_output_tokens, None, BEDROCK_GENERATION_MODEL)
                        logger.info('[CHAT AGENT] 📊 Tracked %s tokens for user %s', ("" if estimated_tokens is None else str(estimated_tokens).replace("\n", "").replace("\r", "")), ("" if session.user_id is None else str(session.user_id).replace("\n", "").replace("\r", "")))
                    except Exception as track_error:
                        logger.error('[CHAT AGENT] ❌ Failed to track token usage: %s', ("" if track_error is None else str(track_error).replace("\n", "").replace("\r", "")), exc_info=True)
                else:
                    logger.warning('[CHAT AGENT] ⚠️ Could not find session %s for token tracking', ("" if session_id is None else str(session_id).replace("\n", "").replace("\r", "")))

                db.add(ChatMessage(session_id=session_id, role="ai", content=filtered_response, sources=json.dumps(sources_list_ref) if sources_list_ref else None))
                db.commit()
        except Exception as finally_error:
            logger.error('[CHAT AGENT] ❌ Error in finally block: %s', ("" if finally_error is None else str(finally_error).replace("\n", "").replace("\r", "")), exc_info=True)

@router.post("/chat/query", tags=["Chat & AI Agents"])
async def query_kb_agent(
    req: ChatRequest,
    caller: AuthenticatedCaller = Depends(authenticated_caller_without_holding_db),
):
    """
    Main endpoint for interacting with the conversational AI agent via text (STREAMING).

    Handles text-based queries, manages chat sessions, and streams the agent's response
    back to the client. Can optionally use internal knowledge, external internet search,
    or a strictly provided context.

    Deliberately has no ``Depends(get_db)``. Every read and write below happens inside a
    ``session_scope()`` that closes before the agent is asked anything, so the connection is
    back in the pool for however long the answer takes. Holding one across the stream is what
    exhausted the pool in UAT while other trainees were only trying to load their modules.
    """
    try:
        with session_scope() as db:
            target_user_id = caller.id
            if req.target_user_id and req.target_user_id != caller.id:
                if db.query(User.id).filter(User.id == req.target_user_id).first():
                    target_user_id = req.target_user_id
            session = db.query(ChatSession).filter(ChatSession.id == req.session_id, ChatSession.user_id == caller.id).first()
            if not session:
                session_name = req.session_name or f"Chat: {req.query[:30]}..."
                if "tutor-" in req.session_id: session_name = f"Tutor Session: {req.session_name}"
                session = ChatSession(id=req.session_id, user_id=caller.id, session_name=session_name)
                db.add(session); db.commit(); db.refresh(session)
            # Copied out while the session is still open: everything below runs without one.
            chat_session_id = session.id

            # Build conversation history with smart truncation for long conversations
            all_messages = session.messages
            # Keep last 20 message pairs (40 messages total) to maintain context while avoiding token limits
            # For tutor sessions, keep more history as context is crucial
            max_history_messages = 40 if "tutor-" in req.session_id else 20
            recent_messages = all_messages[-max_history_messages:] if len(all_messages) > max_history_messages else all_messages

            # CRITICAL FIX: Filter out non-existent images from history before building context
            # This prevents the agent from seeing and repeating invalid image references
            history = []
            for msg in recent_messages:
                content = msg.content
                if content and '[IMAGE:' in content:
                    content = filter_non_existent_images(content)
                history.append({"role": ("assistant" if msg.role == "ai" else "user"), "content": content})

            db.add(ChatMessage(session_id=chat_session_id, role="user", content=req.query))
            db.commit()

        # Choose system prompt based on user role
        # Trainers and admins can answer general questions; trainees are restricted to KB only
        system_prompt = TRAINER_ADMIN_SYSTEM_PROMPT if caller.role in ('trainer', 'super_admin') else AGENT_SYSTEM_PROMPT

        # Build context-aware prompt that references conversation history
        if len(history) > 0:
            # Create a brief summary of recent conversation for context
            recent_topics = []
            for i in range(max(0, len(history) - 6), len(history)):  # Last 3 exchanges
                msg = history[i]
                if msg["role"] == "user":
                    # Extract key topics from user messages
                    content_preview = msg["content"][:100]
                    recent_topics.append(f"- User asked: {content_preview}")
            
            context_summary = "\n".join(recent_topics) if recent_topics else "This is the start of the conversation."
            
            full_prompt = f"""{system_prompt}

**CONVERSATION CONTEXT:**
You are continuing an ongoing conversation. Here are the most recent topics discussed:
{context_summary}

**IMPORTANT:** The user's current query may reference previous messages using pronouns (it, that, this, etc.) or implicit context. Use the conversation history to understand what they're referring to.

**Current user query:** {req.query}"""
        else:
            full_prompt = f"{system_prompt}\n\nHere is the user's query:\n{req.query}"
        
        active_tools = [search_knowledge_base, list_my_reports, get_report_content, list_users_in_my_process, share_reports_with_colleague]
        sources_ref = []
        if "Based strictly on the following context" in req.query:
            # When context is provided, do not use internet search
            context_tools = []
            sources_ref.append({"content": req.context or "", "source": "Provided Context"})
            return StreamingResponse( stream_agent_response(smart_tutor_agent, full_prompt, history, context_tools, chat_session_id, sources_ref), media_type="text/plain" )
        elif req.context:
            prepared = _prepare_raise_hand_context(req.context)
            context_for_tutor = prepared["context"]
            valid_raise_hand_image_urls = prepared["image_urls"]
            relevance_result = await asyncio.to_thread(
                validate_module_question_relevance,
                req.query,
                context_for_tutor,
                req.module_title
            )
            if not relevance_result.get("is_relevant", True):
                mismatch_response = build_module_mismatch_response(
                    req.module_title,
                    relevance_result.get("suggestions", []),
                    relevance_result.get("message"),
                    module_context=context_for_tutor or "",
                    query=req.query
                )
                with session_scope() as db:
                    db.add(ChatMessage(session_id=chat_session_id, role="ai", content=mismatch_response))
                    db.commit()
                async def single_chunk_stream():
                    yield mismatch_response.encode('utf-8')
                return StreamingResponse(single_chunk_stream(), media_type="text/plain")
            # Relevant tutor query: allow normal agent/tooling, but anchor answer to current module context.
            # The context is installed here, in the request's own task, so the streaming generator
            # and the worker thread it dispatches both inherit this learner's identity instead of
            # whichever request wrote the shared object last.
            #
            # tool_context.db is deliberately left unset. Every agent tool opens and closes its own
            # session through _open_tool_session(), nothing reads this field, and publishing a
            # session that is already closed by the time a tool runs would only be a trap.
            begin_agent_tool_context(target_user_id)
            full_prompt = (
                "You are in Tutor Raise-Hand mode for a specific module.\n"
                f"Current module title: {req.module_title or 'Current Module'}\n\n"
                "RESPONSE FORMAT: Start directly with the answer. Never restate the question or describe your plan.\n"
                "Use the provided module context as your primary grounding. "
                "If the learner asks for structured output (e.g., table, bullets, code examples), provide it when helpful. "
                "If details are missing from module text, you may use your available tools/knowledge, "
                "but keep the answer tightly focused on this module's topic.\n"
                "If the learner asks about an architecture/diagram/image, use the exact [IMAGE_URL: ...] tags already present "
                "in the module context. Do not invent or rewrite image paths.\n"
                f"{_build_query_focus_instruction(req.query)}"
                f"Valid IMAGE_URL tags: {valid_raise_hand_image_urls}\n\n"
                f"MODULE CONTEXT:\n---\n{context_for_tutor}\n---\n\nQUESTION: {req.query}"
            )
            sources_ref.append({"content": context_for_tutor, "source": "Provided Context"})
            return StreamingResponse(
                stream_agent_response(smart_tutor_agent, full_prompt, history, active_tools, chat_session_id, sources_ref),
                media_type="text/plain"
            )
        else:
            begin_agent_tool_context(target_user_id)
            return StreamingResponse( stream_agent_response(smart_tutor_agent, full_prompt, history, active_tools, chat_session_id, sources_ref), media_type="text/plain" )
    except Exception as e:
        logger.error('Error in /chat/query (agent): %s', e, exc_info=True)
        # No rollback here: session_scope() above already rolled back and returned the
        # connection, and there is no request-scoped session left to roll back.
        raise HTTPException(status_code=500, detail="Agent error.")

@router.post("/chat/voice-query", tags=["Chat & AI Agents"])
async def voice_query_agent(
    audio: UploadFile = File(...),
    session_id: str = Form(...),
    session_name: Optional[str] = Form(None),
    context: Optional[str] = Form(None),
    target_user_id: Optional[str] = Form(None),
    module_title: Optional[str] = Form(None),
    browser_transcript: Optional[str] = Form(None),
    caller: AuthenticatedCaller = Depends(authenticated_caller_without_holding_db),
):
    """
    Handles voice-based queries for the conversational AI agent (STREAMING).

    Transcribes user's speech with AWS Transcribe, sends it to the agent, and streams back
    the text response. The transcript is emitted as the first line of the stream so the UI
    can show the user's own words before the agent starts answering.

    Like the typed path, this holds no pooled connection across the slow work - and there is
    more of it here, because AWS Transcribe runs before the agent is even asked. The database
    work happens in short ``session_scope()`` blocks inside the stream.

    ``browser_transcript`` is what the browser's Web Speech API heard while recording. It is
    the fallback: if AWS Transcribe is unavailable (not configured, no IAM permission, or it
    times out) that text is used instead of failing the request, so the microphone keeps
    working for every role. It is untrusted user input and is treated exactly like typed
    chat input - length-capped and control-stripped, never logged.
    """
    # Multipart fields bypass the `ChatRequest` validators, so the session key is gated here
    # instead. Same helper as the typed path, so a key that works for a typed raise-hand
    # question works for the spoken one and vice versa.
    try:
        session_id = safe_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    audio_content = await audio.read()
    fallback_text = normalize_voice_transcript(browser_transcript)
    query_text = ""

    filename = (audio.filename or "voice.webm").strip()
    name_ok, _name_err = assert_safe_upload_filename(filename, VOICE_AUDIO_EXTENSIONS)
    transcribe_svc = VoiceTranscribeService(s3_client, transcribe_client, S3_BUCKET_NAME)

    if audio_content and name_ok and is_audio_filename(filename) and transcribe_svc.is_configured:
        try:
            query_text = normalize_voice_transcript(
                await transcribe_svc.transcribe_reply_bytes_async(
                    caller.id,
                    audio_content,
                    filename,
                    language_code=CHAT_TRANSCRIBE_LANGUAGE,
                )
            )
        except Exception as exc:
            # Never surface AWS internals to the caller, and never log the audio or the
            # transcript. Fall through to the browser transcript below.
            logger.warning("Chat voice transcription failed, using browser fallback: %s", log_safe(exc))

    if not query_text:
        query_text = fallback_text
    if not query_text:
        raise HTTPException(
            status_code=400,
            detail="Could not understand the audio. Please speak clearly or type your question.",
        )
    async def combined_stream():
        yield f"transcribed_query:{query_text}\n".encode('utf-8')
        try:
            with session_scope() as db:
                effective_target_user_id = caller.id
                if target_user_id and target_user_id != caller.id:
                    if db.query(User.id).filter(User.id == target_user_id).first():
                        effective_target_user_id = target_user_id
                session = db.query(ChatSession).filter(ChatSession.id == session_id, ChatSession.user_id == caller.id).first()
                if not session:
                    name = session_name or f"Voice Chat: {query_text[:25]}..."
                    if "tutor-" in session_id: name = f"Tutor Session: {session_name}"
                    session = ChatSession(id=session_id, user_id=caller.id, session_name=name)
                    db.add(session); db.commit(); db.refresh(session)
                # Copied out while the session is still open: everything below runs without one.
                chat_session_id = session.id

                # Build conversation history with smart truncation for long conversations
                all_messages = session.messages
                # Keep last 20 message pairs (40 messages total) for tutor, 20 for regular chat
                max_history_messages = 40 if "tutor-" in session_id else 20
                recent_messages = all_messages[-max_history_messages:] if len(all_messages) > max_history_messages else all_messages
                history = [{"role": ("assistant" if msg.role == "ai" else "user"), "content": msg.content} for msg in recent_messages]

                db.add(ChatMessage(session_id=chat_session_id, role="user", content=query_text))
                db.commit()

            # Choose system prompt based on user role
            voice_system_prompt = TRAINER_ADMIN_SYSTEM_PROMPT if caller.role in ('trainer', 'super_admin') else AGENT_SYSTEM_PROMPT

            # Build context-aware prompt that references conversation history
            if len(history) > 0:
                # Create a brief summary of recent conversation for context
                recent_topics = []
                for i in range(max(0, len(history) - 6), len(history)):  # Last 3 exchanges
                    msg = history[i]
                    if msg["role"] == "user":
                        content_preview = msg["content"][:100]
                        recent_topics.append(f"- User asked: {content_preview}")
                
                context_summary = "\n".join(recent_topics) if recent_topics else "This is the start of the conversation."
                
                full_prompt = f"""{voice_system_prompt}

**CONVERSATION CONTEXT:**
You are continuing an ongoing conversation. Here are the most recent topics discussed:
{context_summary}

**IMPORTANT:** The user's current query may reference previous messages using pronouns (it, that, this, etc.) or implicit context. Use the conversation history to understand what they're referring to.

**Current user query:** {query_text}"""
            else:
                full_prompt = f"{voice_system_prompt}\n\nHere is the user's query:\n{query_text}"
            
            sources_ref = []
            active_tools = [search_knowledge_base, list_my_reports, get_report_content, list_users_in_my_process, share_reports_with_colleague]

            if context:
                prepared = _prepare_raise_hand_context(context)
                context_for_tutor = prepared["context"]
                valid_raise_hand_image_urls = prepared["image_urls"]
                relevance_result = await asyncio.to_thread(
                    validate_module_question_relevance,
                    query_text,
                    context_for_tutor,
                    module_title
                )
                if not relevance_result.get("is_relevant", True):
                    mismatch_response = build_module_mismatch_response(
                        module_title,
                        relevance_result.get("suggestions", []),
                        relevance_result.get("message"),
                        module_context=context_for_tutor or "",
                        query=query_text
                    )
                    with session_scope() as db:
                        db.add(ChatMessage(session_id=chat_session_id, role="ai", content=mismatch_response))
                        db.commit()
                    yield mismatch_response.encode('utf-8')
                    return
                # Relevant tutor voice query: allow normal tools while grounding on module context.
                # tool_context.db is not set - see the typed path for why.
                begin_agent_tool_context(effective_target_user_id)
                full_prompt = (
                    "You are in Tutor Raise-Hand mode for a specific module.\n"
                    f"Current module title: {module_title or 'Current Module'}\n\n"
                    "RESPONSE FORMAT: Start directly with the answer. Never restate the question or describe your plan.\n"
                    "Use the provided module context as your primary grounding. "
                    "If the learner asks for structured output (e.g., table, bullets, code examples), provide it when helpful. "
                    "If details are missing from module text, you may use your available tools/knowledge, "
                    "but keep the answer tightly focused on this module's topic.\n"
                    "If the learner asks about an architecture/diagram/image, use the exact [IMAGE_URL: ...] tags already present "
                    "in the module context. Do not invent or rewrite image paths.\n"
                    f"{_build_query_focus_instruction(query_text)}"
                    f"Valid IMAGE_URL tags: {valid_raise_hand_image_urls}\n\n"
                    f"MODULE CONTEXT:\n---\n{context_for_tutor}\n---\n\nQUESTION: {query_text}"
                )
                sources_ref.append({"content": context_for_tutor, "source": "Provided Context"})
                stream_generator = stream_agent_response(smart_tutor_agent, full_prompt, history, active_tools, chat_session_id, sources_ref)
            else:
                begin_agent_tool_context(effective_target_user_id)
                stream_generator = stream_agent_response(smart_tutor_agent, full_prompt, history, active_tools, chat_session_id, sources_ref)
            async for chunk in stream_generator:
                yield chunk
        except Exception as e:
            logger.error('Error in voice query agent stream: %s', e, exc_info=True)
            yield "Sorry, I encountered an error.".encode('utf-8')
            # No rollback: session_scope() owns that, and no session is open here.
    return StreamingResponse(combined_stream(), media_type="text/plain")

# Amazon Polly accepts at most 3000 billed characters in one synthesis request (6000 counting
# SSML markup). Long text used to be cut to 2400 characters with an "..." appended, so the tutor
# simply stopped reading part way through a paragraph the screen was still showing in full.
# It is now split into sentence-aligned chunks that are synthesised in order and concatenated.
# The budget is per chunk of PLAIN text and is kept well below 3000 because
# enhance_text_for_natural_speech() adds prosody/break markup on top of it.
SPEECH_CHUNK_MAX_CHARS = 1200


def _split_oversized_sentence(sentence: str, limit: int) -> List[str]:
    """
    One sentence longer than `limit` broken on word boundaries, never mid-word.

    Nothing is discarded: the pieces concatenate back to the same words in the same order.
    """
    if len(sentence) <= limit:
        return [sentence]
    pieces: List[str] = []
    current = ""
    for word in sentence.split():
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        # A single "word" longer than the whole budget (a pasted URL, a hash) has no boundary
        # to break on, so it is cut - still read out in order, still nothing dropped.
        while len(word) > limit:
            pieces.append(word[:limit])
            word = word[limit:]
        current = word
    if current:
        pieces.append(current)
    return pieces


def split_text_for_synthesis(text: str, limit: int = SPEECH_CHUNK_MAX_CHARS) -> List[str]:
    """
    Split text into chunks of at most `limit` characters WITHOUT losing a word.

    Sentence boundaries are preferred so a chunk break lands where a speaker would pause;
    _split_sentences() is a linear character scan, so this stays cheap on model-generated text
    of any shape (no regex, hence no backtracking - see strip_ssml_tags for the same reasoning).
    """
    source = (text or "").strip()
    if not source:
        return []
    if len(source) <= limit:
        return [source]

    chunks: List[str] = []
    current = ""
    for sentence in (_split_sentences(source) or [source]):
        for piece in _split_oversized_sentence(sentence, limit):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= limit:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


# Amazon Polly errors that mean "ask again", not "this text cannot be spoken". The generative
# engine's request quota is far lower than the standard engines', and the tutor asks for the
# current paragraph plus the prefetched ones at nearly the same instant, so a throttle here is an
# ordinary event rather than a fault. It used to surface as a 429 the browser did not retry, and
# the lecture stopped with "Error generating audio." part way through the module.
POLLY_RETRYABLE_ERROR_CODES = frozenset({
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestThrottled",
    "SlowDown",
    "ServiceUnavailable",
    "InternalServiceError",
    "InternalFailure",
    "RequestTimeout",
})
# Bounded on purpose: worst case is 0.5s + 1.0s per payload per chunk, which stays well inside the
# client's own speech timeout even for a paragraph that splits into several chunks. A longer
# backoff would just leave the learner watching a spinner.
POLLY_MAX_RETRIES_PER_PAYLOAD = 2
POLLY_RETRY_BASE_DELAY_SECONDS = 0.5


def _synthesize_speech_chunk(text_to_speak: str, voice_id: str) -> bytes:
    """
    Synthesise one chunk with Amazon Polly and return the COMPLETE mp3 body.

    The audio stream is read to the end here instead of being handed to StreamingResponse. A
    streamed Polly handle that fails half way through arrives at the browser as a short but
    perfectly successful download - narration that stops mid-sentence with no error logged
    anywhere. Read in full, a failure is an HTTP failure and the client's retry can fix it,
    and the response carries a Content-Length so a cut-off download is detectable.
    """
    enhanced_text = enhance_text_for_natural_speech(text_to_speak)
    ssml_text = f"<speak>{enhanced_text}</speak>"
    engine = POLLY_ENGINE

    # Polly's generative engine rejects SSML outright ("InvalidSsmlException: Unsupported
    # Generative feature"), so asking for it burned a failed synthesis call on every single
    # request before the plain-text retry. Offer SSML only to an engine that accepts it, and
    # give the generative engine the same enhanced wording with the tags stripped - that keeps
    # the markdown clean-up, which is not SSML.
    if engine in POLLY_SSML_ENGINES:
        synthesis_attempts = [(ssml_text, 'ssml'), (text_to_speak, 'text')]
    else:
        synthesis_attempts = [(strip_ssml_tags(enhanced_text), 'text'), (text_to_speak, 'text')]
    # Retrying the identical payload would only repeat the same failure.
    seen_payloads = set()
    synthesis_attempts = [
        attempt for attempt in synthesis_attempts
        if not (attempt in seen_payloads or seen_payloads.add(attempt))
    ]
    last_client_error: Optional[ClientError] = None

    for text_payload, text_type in synthesis_attempts:
        for retry_index in range(POLLY_MAX_RETRIES_PER_PAYLOAD + 1):
            try:
                response = polly_client.synthesize_speech(
                    Text=text_payload,
                    TextType=text_type,
                    OutputFormat='mp3',
                    VoiceId=voice_id,
                    Engine=engine
                )
                audio_stream = response['AudioStream']
                try:
                    audio_bytes = audio_stream.read()
                finally:
                    close_stream = getattr(audio_stream, "close", None)
                    if callable(close_stream):
                        close_stream()
                logger.info(
                    'Speech synthesis successful with engine=%s, text_type=%s, chars=%s, bytes=%s',
                    engine,
                    ("" if text_type is None else str(text_type).replace("\n", "").replace("\r", "")),
                    len(text_to_speak),
                    len(audio_bytes),
                )
                return audio_bytes
            except ClientError as attempt_error:
                last_client_error = attempt_error
                error_code = attempt_error.response.get('Error', {}).get('Code', 'Unknown')
                logger.warning(
                    "Speech synthesis attempt failed: engine=%s, text_type=%s, error_code=%s, message=%s",
                    engine,
                    ("" if text_type is None else str(text_type).replace("\n", "").replace("\r", "")),
                    ("" if error_code is None else str(error_code).replace("\n", "").replace("\r", "")),
                    ("" if attempt_error is None else str(attempt_error).replace("\n", "").replace("\r", "")),
                )
                # A throttle means "ask again"; a bad voice or bad SSML means "ask differently",
                # which is what the next payload in synthesis_attempts is for.
                if error_code not in POLLY_RETRYABLE_ERROR_CODES:
                    break
                if retry_index >= POLLY_MAX_RETRIES_PER_PAYLOAD:
                    break
                time.sleep(POLLY_RETRY_BASE_DELAY_SECONDS * (2 ** retry_index))

    if last_client_error:
        raise last_client_error
    raise HTTPException(status_code=500, detail="Speech synthesis failed for all engine fallbacks.")


# --- Server-side cache of finished narration audio ---
#
# This is what makes a shared lecture scale. /tutor/explain-module is already cached in
# TutorResponseCache, so every trainee studying the same module is shown byte-identical
# paragraph text - and each one then asked Amazon Polly to synthesise that identical text
# again. Fifty trainees on one module meant fifty calls per paragraph to produce fifty copies
# of the same mp3, spent against the generative engine's request quota, which is far lower than
# the standard engines' (see POLLY_RETRYABLE_ERROR_CODES above). Caching the finished audio by
# the text that produced it turns that into one call per worker per paragraph.
#
# Cached by CONTENT, so a hit can only ever return audio for text the caller itself supplied -
# there is nothing user-specific in the key and nothing of one user's to leak to another. The
# key carries the voice and the engine because both change the audio, and a version prefix so a
# future change to the chunking or the SSML wording cannot be served stale audio from a
# still-warm process. Bounds come from configuration (SPEECH_AUDIO_CACHE_MAX_MB /
# SPEECH_AUDIO_CACHE_MAX_ENTRIES); an unbounded cache on a container this size is a memory leak
# with a friendly name.
SPEECH_AUDIO_CACHE_VERSION = "v1"
_speech_audio_cache: "OrderedDict[Tuple[str, str, str, str], bytes]" = OrderedDict()
_speech_audio_cache_bytes = 0
# The endpoint is `def`, not `async def`, so FastAPI runs it in the threadpool and several
# trainees are genuinely inside it at once. The lock is not optional.
_speech_audio_cache_lock = threading.Lock()


def _speech_audio_cache_limit_bytes() -> int:
    return SPEECH_AUDIO_CACHE_MAX_MB * 1024 * 1024


def _speech_audio_cache_get(key: "Tuple[str, str, str, str]") -> Optional[bytes]:
    """Return cached audio for this exact text/voice/engine, or None."""
    if _speech_audio_cache_limit_bytes() <= 0:
        return None
    with _speech_audio_cache_lock:
        audio = _speech_audio_cache.get(key)
        if audio is None:
            return None
        # Least-recently-used, so the paragraphs of the lecture people are actually in stay warm.
        _speech_audio_cache.move_to_end(key)
        return audio


def _speech_audio_cache_put(key: "Tuple[str, str, str, str]", audio: bytes) -> None:
    """Store finished audio, evicting the least recently used until both bounds hold."""
    global _speech_audio_cache_bytes
    limit_bytes = _speech_audio_cache_limit_bytes()
    if limit_bytes <= 0 or not audio:
        return
    # One item larger than the whole budget would evict everything else and then itself.
    if len(audio) > limit_bytes:
        return
    with _speech_audio_cache_lock:
        previous = _speech_audio_cache.pop(key, None)
        if previous is not None:
            _speech_audio_cache_bytes -= len(previous)
        _speech_audio_cache[key] = audio
        _speech_audio_cache_bytes += len(audio)
        while _speech_audio_cache and (
            _speech_audio_cache_bytes > limit_bytes
            or len(_speech_audio_cache) > SPEECH_AUDIO_CACHE_MAX_ENTRIES
        ):
            _, evicted = _speech_audio_cache.popitem(last=False)
            _speech_audio_cache_bytes -= len(evicted)


# The cache above only helps the trainee who arrives second. A class that starts a module
# together arrives at paragraph one at the same instant, with the cache still cold, and every one
# of them would synthesise it - the exact throttling storm the cache exists to prevent. So one
# request per distinct paragraph does the work and the rest wait for it and then read the cache.
#
# The wait is bounded, and by Polly's own configured budget rather than a number invented here:
# if the request doing the work has not finished in that time it is in trouble, and a learner
# waiting on it should stop waiting and ask for their own audio rather than be held behind it.
# Waiting costs the same request thread that synthesising would have cost, so this never occupies
# more of AnyIO's worker-thread pool than the old behaviour did - it just makes far fewer Polly
# calls.
_SPEECH_AUDIO_SINGLE_FLIGHT_WAIT_SECONDS = POLLY_CONNECT_TIMEOUT_SECONDS + POLLY_READ_TIMEOUT_SECONDS
# key -> [lock, holders]. Refcounted so the registry cannot grow one entry per paragraph ever
# narrated; the entry is dropped as soon as the last interested request lets go of it.
_speech_audio_inflight: "Dict[Tuple[str, str, str, str], list]" = {}


def _speech_audio_claim_synthesis(key: "Tuple[str, str, str, str]") -> Optional[Any]:
    """
    Take the right to synthesise this text, waiting briefly for whoever already holds it.

    Returns the lock to release afterwards, or None if the wait expired - in which case the
    caller simply does the work itself, unshared. Never raises, and never blocks indefinitely.

    Interest in the key is registered either way, so the matching release below is called exactly
    once per claim whatever the outcome, and only the last one out removes the key.
    """
    if _speech_audio_cache_limit_bytes() <= 0:
        return None
    with _speech_audio_cache_lock:
        entry = _speech_audio_inflight.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _speech_audio_inflight[key] = entry
        entry[1] += 1
    lock = entry[0]
    if lock.acquire(timeout=_SPEECH_AUDIO_SINGLE_FLIGHT_WAIT_SECONDS):
        return lock
    # Waited long enough: go it alone rather than leave the learner staring at a spinner.
    return None


def _speech_audio_release_synthesis(key: "Tuple[str, str, str, str]", lock: Optional[Any]) -> None:
    """
    Release the synthesis right, forgetting the key once nobody is waiting on it.

    Safe to call with lock=None, which is both the "caching is off" case (nothing was registered,
    and the get below simply finds nothing) and the "wait expired" case.
    """
    if lock is not None:
        lock.release()
    with _speech_audio_cache_lock:
        entry = _speech_audio_inflight.get(key)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] <= 0:
            _speech_audio_inflight.pop(key, None)


def _speech_audio_for(text_to_speak: str, voice_id: str) -> bytes:
    """
    Return the complete mp3 for this paragraph, reusing a cached one if there is one.

    Everything about sharing the audio lives here so the endpoint stays a thin HTTP wrapper:
    ask the cache, otherwise take the right to synthesise (which is also a chance for the
    cache to have been filled while waiting), synthesise, publish. Raises exactly what
    _synthesize_speech_chunk raises, so the endpoint's status mapping is unchanged.
    """
    cache_key = (SPEECH_AUDIO_CACHE_VERSION, text_to_speak, voice_id, POLLY_ENGINE)
    # Asked before any Polly work, on the cleaned text, so every trainee after the first to reach
    # a given paragraph is served without a call at all.
    cached_audio = _speech_audio_cache_get(cache_key)
    if cached_audio is not None:
        logger.info(
            "Speech synthesis served from cache: text_length=%s, bytes=%s",
            len(text_to_speak),
            len(cached_audio),
        )
        return cached_audio

    synthesis_lock = _speech_audio_claim_synthesis(cache_key)
    try:
        # Somebody may have finished this very paragraph while we were waiting for the right to
        # synthesise it, which is the whole point of waiting.
        cached_audio = _speech_audio_cache_get(cache_key) if synthesis_lock is not None else None
        if cached_audio is not None:
            logger.info(
                "Speech synthesis served from cache filled by a concurrent request: "
                "text_length=%s, bytes=%s",
                len(text_to_speak),
                len(cached_audio),
            )
            return cached_audio

        chunks = split_text_for_synthesis(text_to_speak)
        logger.info(
            "Speech synthesis request: voice=%s, text_length=%s, chunks=%s, engine=%s",
            ("" if voice_id is None else str(voice_id).replace("\n", "").replace("\r", "")),
            len(text_to_speak),
            len(chunks),
            POLLY_ENGINE,
        )
        # mp3 frames are self-contained, so the chunk bodies concatenate into one playable file
        # and the browser gets a single audio element for the whole paragraph, exactly as before.
        audio_body = b"".join(_synthesize_speech_chunk(chunk, voice_id) for chunk in chunks)
        # Only a complete body is stored (the put is a no-op for empty audio), so a partial or
        # failed synthesis can never be replayed to the next learner as the finished paragraph.
        _speech_audio_cache_put(cache_key, audio_body)
        return audio_body
    finally:
        _speech_audio_release_synthesis(cache_key, synthesis_lock)


@router.post("/speech/synthesize", tags=["Chat & AI Agents"])
def synthesize_speech(
    req: SynthesizeRequest,
    # Authenticated, but WITHOUT holding a pooled DB connection across the Polly call - this is
    # the tutor's most frequent endpoint and it touches no table. See the dependency's docstring.
    _user_id: str = Depends(authenticated_user_id_without_holding_db),
):
    """
    Converts text to speech using Amazon Polly for the AI Tutor.

    - **Body**: A JSON object with `text` to synthesize and an optional `voiceId`.
    - **Returns**: The complete MP3 audio for the whole of `text`.

    The audio always covers ALL of the text supplied. The tutor shows a paragraph on screen and
    asks for that same paragraph here, so anything dropped between the two would be text the
    learner can read but never hears.
    """
    try:
        text_to_speak = req.text.strip()
        text_to_speak = re.sub(r'[&<>]', '', text_to_speak)
        if not text_to_speak:
            logger.warning("Empty text provided for speech synthesis")
            return Response(status_code=204)

        audio_body = _speech_audio_for(text_to_speak, req.voiceId)
        if not audio_body:
            logger.warning('Speech synthesis produced no audio for %s characters', len(text_to_speak))
            return Response(status_code=204)
        return Response(content=audio_body, media_type='audio/mpeg')

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        logger.error('Amazon Polly client error [%s]: %s', ("" if error_code is None else str(error_code).replace("\n", "").replace("\r", "")), ("" if error_message is None else str(error_message).replace("\n", "").replace("\r", "")), exc_info=True)
        
        # Provide more specific error messages
        if error_code == 'ThrottlingException':
            raise HTTPException(status_code=429, detail="Speech synthesis service is temporarily busy. Please try again.")
        elif error_code == 'InvalidParameterValueException':
            raise HTTPException(status_code=400, detail="Invalid voice or text parameters provided.")
        elif error_code == 'EngineNotSupportedException':
            raise HTTPException(status_code=400, detail="Selected voice does not support the configured Amazon Polly engine.")
        else:
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)
            
    except Exception as e:
        logger.error('Unexpected error in speech synthesis: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred during speech synthesis.")


GENERATIVE_VOICE_CACHE_TTL_SECONDS = 3600
_generative_voice_cache: Dict[str, Any] = {"timestamp": 0, "voices": []}


def get_generative_voice_catalog() -> List[Dict[str, str]]:
    """
    Returns cached list of Polly voices that support the configured engine (POLLY_ENGINE).

    The catalogue has to be asked for the same engine synthesis uses, or the picker offers
    voices that then fail with EngineNotSupportedException.
    """
    now = time.time()
    if (
        _generative_voice_cache.get("voices")
        and (now - _generative_voice_cache.get("timestamp", 0) < GENERATIVE_VOICE_CACHE_TTL_SECONDS)
    ):
        return _generative_voice_cache["voices"]

    voices: List[Dict[str, str]] = []
    next_token = None
    while True:
        kwargs = {"Engine": POLLY_ENGINE}
        if next_token:
            kwargs["NextToken"] = next_token
        response = polly_client.describe_voices(**kwargs)
        for voice in response.get("Voices", []):
            voice_id = voice.get("Id")
            if not voice_id:
                continue
            language_code = (voice.get("LanguageCode") or "").lower()
            language_name = (voice.get("LanguageName") or "").lower()
            # UI requirement: show only English voices.
            if not (language_code.startswith("en-") or language_name == "english"):
                continue
            label = f"{voice_id} ({voice.get('LanguageName', 'Unknown')} {voice.get('Gender', '')})".strip()
            voices.append({
                "id": voice_id,
                "name": label
            })
        next_token = response.get("NextToken")
        if not next_token:
            break

    # Stable alphabetical order for filtered English-only list.
    voices.sort(key=lambda v: v["id"])
    _generative_voice_cache["timestamp"] = now
    _generative_voice_cache["voices"] = voices
    return voices


@router.get("/speech/generative-voices", tags=["Chat & AI Agents"])
def list_generative_voices(_user_id: str = Depends(authenticated_user_id_without_holding_db)):
    """
    Returns Polly voices that support the generative engine for the current region.
    """
    try:
        voices = get_generative_voice_catalog()
        return {"voices": voices}
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        logger.error('Failed to list generative voices [%s]: %s', ("" if error_code is None else str(error_code).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load generative voices right now.")
    except Exception as e:
        logger.error('Unexpected error listing generative voices: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load generative voices right now.")


def strip_ssml_tags(ssml_or_text: str) -> str:
    r"""
    Render SSML-annotated text as plain text, for a Polly engine that rejects SSML.

    enhance_text_for_natural_speech() does two jobs: it adds SSML prosody/breaks, and it also
    does plain-text work worth keeping - the markdown clean-up, so "**MCP**" is not read as
    "star star M C P". The generative engine cannot take the tags, so they are removed and the
    wording kept instead of falling all the way back to the raw text.

    Deliberately built from str.find / str.split / str.replace instead of regular expressions.
    The input is model-generated lecture text, so it counts as uncontrolled data, and every
    regex that would express this naturally is ambiguous enough to backtrack quadratically
    (CodeQL py/polynomial-redos): `<[^>]*>` rescans from every `<` in a run of `<<<<<`, and
    `\s+` before a character class can start matching anywhere inside a whitespace run. The
    scan below visits each character a fixed number of times, so a pathological paragraph
    costs the same as an ordinary one.
    """
    source = ssml_or_text or ""
    pieces = []
    # Walk forward with a cursor rather than re-slicing the tail each time round: `source =
    # source[closed + 1:]` would copy the remainder once per tag, which is quadratic again for
    # a different reason. find() takes a start index, so each character is visited once.
    cursor = 0
    while cursor < len(source):
        opened = source.find("<", cursor)
        if opened == -1:
            pieces.append(source[cursor:])
            break
        closed = source.find(">", opened + 1)
        if closed == -1:
            # Unterminated "<" is not a tag; keep it as text, exactly as the pattern did.
            pieces.append(source[cursor:])
            break
        pieces.append(source[cursor:opened])
        # A tag becomes a space so "one.<break/>Two" does not run together as "one.Two".
        pieces.append(" ")
        cursor = closed + 1

    # str.split() with no argument splits on runs of any whitespace and drops the empties,
    # which is the collapse-and-strip the two regexes used to do.
    collapsed = " ".join("".join(pieces).split())

    # A removed <break/> can leave the sentence looking like "step ." - close that gap so the
    # pause is not read out as a separate word. Whitespace is already single spaces by now,
    # so a literal two-character replace is enough.
    for mark in ",.;:!?":
        collapsed = collapsed.replace(" " + mark, mark)
    return collapsed


def enhance_text_for_natural_speech(text: str) -> str:
    """
    Enhances text with SSML tags for natural, human-like speech patterns.
    Adds pauses after headings, natural expressions, and breathing patterns.
    Includes error handling to prevent SSML failures.
    """
    try:
        enhanced = text
        
        # Sanitize text first - remove problematic characters that could break SSML
        enhanced = re.sub(r'[<>&]', '', enhanced)  # Remove XML-breaking characters
        enhanced = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', enhanced)  # Remove control characters
        
        # This function speaks the caller's words and adds none of its own. A rotating closing
        # question ("Would you like a real-world example...?") used to be appended to every
        # paragraph here: the tutor asked it out loud, the lecture pane never showed it, and the
        # narration then moved on by itself - so what was heard and what was on screen disagreed
        # on every single paragraph. The tutor already offers the same interaction through Raise
        # Hand and the end-of-module actions, which are visible on screen.
        enhanced = enhanced.strip()

        # Early return for very short text.
        if len(enhanced) < 20:
            return enhanced

        # Convert markdown-ish content into speech-friendly plain text first.
        enhanced = re.sub(r'`([^`]{1,80})`', r'\1', enhanced)
        enhanced = re.sub(r'\*\*([^*]{1,120})\*\*', r'\1', enhanced)
        enhanced = re.sub(r'\*([^*]{1,120})\*', r'\1', enhanced)
        enhanced = re.sub(r'#{1,6}\s*([^\n]+)', r'\1.', enhanced)
        enhanced = re.sub(r'^\s*[-*]\s+', '- ', enhanced, flags=re.MULTILINE)
        enhanced = re.sub(r'^\s*(\d+)\.\s+', r'\1) ', enhanced, flags=re.MULTILINE)

        # Normalize line breaks first, then apply sentence-level contextual prosody.
        enhanced = re.sub(r'\n{2,}', ' ', enhanced)
        enhanced = re.sub(r'\n', ' ', enhanced)
        enhanced = re.sub(r'\s+', ' ', enhanced).strip()

        def _contextual_prosody(sentence: str, index: int) -> str:
            """Apply dynamic prosody based on sentence intent/content."""
            s = sentence.strip()
            if not s:
                return ""

            lower = s.lower()

            question_words = ('what', 'why', 'how', 'which', 'when', 'where', 'could', 'would', 'should')
            caution_words = ('important', 'critical', 'warning', 'careful', 'must', 'never', 'avoid', 'risk')
            step_words = ('first', 'next', 'then', 'finally', 'step', 'now', 'after that')
            emphasis_words = ('key', 'remember', 'note that', 'focus on')

            if s.endswith('?') or lower.startswith(question_words):
                # Energetic, inquisitive tone for direct interaction.
                return f'<prosody pitch="+16%" rate="91%" volume="+2dB">{s}</prosody><break time="680ms"/>'
            if any(word in lower for word in caution_words):
                # Professional/calm for critical statements.
                return f'<prosody pitch="-7%" rate="88%" volume="+2dB">{s}</prosody><break time="760ms"/>'
            if any(lower.startswith(word) for word in step_words):
                # Clear instructional rhythm.
                return f'<prosody pitch="+7%" rate="93%">{s}</prosody><break time="640ms"/>'
            if any(word in lower for word in emphasis_words):
                return f'<prosody pitch="+10%" rate="94%" volume="+2dB">{s}</prosody><break time="650ms"/>'
            if re.search(r'\d|%|x|times|seconds|minutes|hours', lower):
                # Numeric content should be slightly slower for clarity.
                return f'<prosody pitch="-2%" rate="89%">{s}</prosody><break time="700ms"/>'

            # Calm professional default with gentle sentence-to-sentence variation.
            if index % 2 == 0:
                return f'<prosody pitch="+4%" rate="95%">{s}</prosody><break time="580ms"/>'
            return f'<prosody pitch="-3%" rate="94%">{s}</prosody><break time="600ms"/>'

        # Split into sentence-like chunks while keeping punctuation attached.
        sentence_chunks = _split_sentences(enhanced)
        if sentence_chunks:
            enhanced = " ".join(_contextual_prosody(chunk, idx) for idx, chunk in enumerate(sentence_chunks)).strip()
        else:
            enhanced = f'<prosody pitch="+2%" rate="96%">{enhanced}</prosody><break time="600ms"/>'
        
        # Clean up repeated breaks and spacing.
        enhanced = re.sub(
            r'<break time="[^"]+"/>\s*<break time="[^"]+"/>',
            '<break time="900ms"/>',
            enhanced
        )
        enhanced = re.sub(r'\s+', ' ', enhanced)

        # NOTE: there is deliberately no "unwrap nested prosody" pass here any more, and it must
        # not come back. Nesting cannot occur: every "<" was removed from the text above, so a
        # sentence handed to _contextual_prosody() can never contain a tag, and each sentence
        # gets exactly one wrapper. The pass that used to sit here treated the CONSECUTIVE
        # blocks it saw as nested and rewrote a three-block span down to its middle block, in a
        # loop - a five-sentence paragraph came out as one sentence (the fourth), and that is why
        # the tutor read a fraction of what the screen showed, for every role. If a nesting guard
        # is ever needed again it has to match an opening tag that occurs BEFORE the matching
        # close of the block already open, and it must never drop a sentence.
        #
        # There is no length clamp here either. Callers size their text with
        # split_text_for_synthesis() so the SSML form stays inside Polly's limit; clipping the
        # markup at a fixed offset used to cut mid-tag, which both lost the tail and produced
        # the InvalidSsmlException seen in the logs.
        return enhanced.strip()
        
    except Exception as e:
        logger.warning('Error enhancing text for speech, returning original: %s', e)
        # If enhancement fails, return sanitized original text
        sanitized = re.sub(r'[<>&]', '', text)
        sanitized = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', sanitized)
        return sanitized.strip()

def analyze_image_with_vision_bytes(image_bytes: bytes, context_before: str, context_after: str, s3_key: str = "") -> str:
    """
    Uses Amazon Nova Pro with vision to analyze an image and provide detailed explanation.
    
    Args:
        image_bytes: Image data as bytes
        context_before: Text content before the image in the module
        context_after: Text content after the image in the module
        s3_key: S3 key for logging purposes
    
    Returns:
        Detailed explanation of what's in the image
    """
    from utils.aws_helper import invoke_nova_pro_vision, nova_image_format_from_path

    try:
        logger.info('[VISION] Analyzing image from bytes: %s', log_safe(s3_key))
        
        # CRITICAL FIX: Resize image if dimensions exceed Bedrock's limit (8000 pixels)
        try:
            image_bytes = resize_image_if_needed(image_bytes, max_dimension=7900)
        except Exception as resize_error:
            logger.error('[VISION] Image resize failed: %s', log_safe(resize_error))
            # Continue with original image and hope for the best
        
        image_format = nova_image_format_from_path(s3_key)
        
        # Create the vision analysis prompt
        vision_prompt = f"""You are an expert image analyst. Analyze this image in the context of the surrounding module content.

**CONTEXT BEFORE IMAGE:**
{context_before[-1000:] if len(context_before) > 1000 else context_before}

**CONTEXT AFTER IMAGE:**
{context_after[:1000] if len(context_after) > 1000 else context_after}

**YOUR TASK:**
Provide a detailed, specific description of what's in this image. Focus on:

1. **Type of Visualization**: What kind of image is this? (chart, diagram, document scan, photo, etc.)

2. **Specific Elements**: List ALL visible elements:
   - For charts/plots: ALL axis labels, data series names, values, legends
   - For diagrams: ALL component names, labels, connections, arrows
   - For documents: ALL visible text, fields, sections, stamps
   - For photos: ALL visible objects, text, features

3. **Visual Details**: Describe:
   - Colors and what they represent
   - Shapes and their meanings
   - Spatial layout (top, bottom, left, right, center)
   - Any text, numbers, or labels visible

4. **Data/Information**: Extract:
   - Specific values, scores, measurements
   - Categories, classifications, groupings
   - Relationships, connections, flows

5. **Key Insights**: What are the most important things to notice?

**CRITICAL RULES:**
- BE SPECIFIC: List actual labels, values, text you see (not "various categories")
- BE DETAILED: Describe colors, positions, shapes precisely
- BE ACCURATE: Only describe what you actually see in the image
- BE CONTEXTUAL: Connect your description to the surrounding module content

Provide your analysis in a structured format that can be used to explain this image to a student."""

        analysis, response_body = invoke_nova_pro_vision(
            sync_bedrock_client,
            image_bytes=image_bytes,
            prompt=vision_prompt,
            image_format=image_format,
            model_id=BEDROCK_IMAGE_MODEL,
            max_tokens=2000,
            temperature=0.3,
        )
        try:
            from services.token_tracking import record_vision_tokens_from_response

            record_vision_tokens_from_response(response_body)
        except Exception as track_exc:
            logger.debug("Vision token tracking skipped: %s", log_safe(track_exc))
        
        logger.info('[VISION] ✓ Analysis complete: %s chars', len(analysis))
        return analysis
        
    except Exception as exc:
        logger.error('[VISION] ✗ Error analyzing image: %s', log_safe(exc), exc_info=True)
        return "[Image analysis unavailable.]"

def analyze_image_with_vision(image_path: str, context_before: str, context_after: str) -> str:
    """
    DEPRECATED: Use analyze_image_with_vision_bytes instead.
    Uses Amazon Nova Pro with vision to analyze an image and provide detailed explanation.
    
    Args:
        image_path: Local path to the image file
        context_before: Text content before the image in the module
        context_after: Text content after the image in the module
    
    Returns:
        Detailed explanation of what's in the image
    """
    try:
        logger.info('[VISION] Analyzing image: %s', log_safe(image_path))
        
        # Read and encode the image
        with open(image_path, "rb") as image_file:
            image_bytes = image_file.read()
        
        # Use the bytes version
        return analyze_image_with_vision_bytes(image_bytes, context_before, context_after, image_path)
    except Exception as exc:
        logger.error('[VISION] Error: %s', log_safe(exc))
        return "[Image analysis unavailable.]"


@router.post("/tutor/explain-module", tags=["Chat & AI Agents"])
def get_tutor_explanation(req: TutorLectureRequest, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Generates a lecture script and prepares images for display by downloading them to a temporary local folder.
    Uses Amazon Nova Pro with vision to analyze images for accurate explanations.
    OPTIMIZED: Caches responses in S3 to save API costs.
    FIXED: Works for both published and unpublished courses.
    """
    try:
        # OPTIMIZATION: Check cache first using actual course_id and module_index.
        # Include a response-format marker so stale cache is invalidated when tutor output rules change.
        course_id = req.course_id or "generic"
        module_index = normalize_module_index(req.module_index if req.module_index is not None else 0)
        cache_module_content = build_tutor_cache_module_content(req.module_content or '')
        
        logger.info(
            '[TUTOR] User %s (%s) requesting tutor for course_id=%s, module_index=%s',
            log_safe(user.id),
            log_safe(user.role),
            log_safe(course_id),
            module_index,
        )        
        # Log tutor usage activity
        log_activity(db, user.id, "TUTOR_ACCESS", f"Accessing tutor for course_id={course_id}, module_index={module_index}")
        
        # FIXED: Enhanced access control for tutor functionality
        course = None  # Initialize course to None
        if course_id != "generic":
            course = db.query(Course).filter(Course.id == course_id).first()
            if course:
                # Check if user has access to this course
                if user.role == 'super_admin':
                    # Super admins can access any course
                    logger.info('[TUTOR] Super admin %s accessing course %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
                elif user.role == 'trainer':
                    if course.author_id == user.id:
                        # Trainers can access their own courses (published or unpublished)
                        logger.info('[TUTOR] Trainer %s accessing own course %s (published: %s)', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if course.is_live is None else str(course.is_live).replace("\n", "").replace("\r", "")))
                    else:
                        # Check if this course content has been shared with the current trainer
                        # First check for shared reports (individual module sharing)
                        shared_report = db.query(SharedReport).filter(
                            SharedReport.sharer_id == course.author_id,
                            SharedReport.recipient_id == user.id,
                            SharedReport.source_doc_name == course.source_doc_name
                        ).first()
                        
                        # Also check for shared courses (full course sharing)
                        shared_course = db.query(SharedCourse).filter(
                            SharedCourse.sharer_id == course.author_id,
                            SharedCourse.recipient_id == user.id,
                            SharedCourse.course_id == course_id
                        ).first()
                        
                        if not shared_report and not shared_course:
                            logger.warning('[TUTOR] Trainer %s attempted to access unshared course %s from %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
                            raise HTTPException(status_code=403, detail="Access denied. This course has not been shared with you.")
                        
                        if shared_course:
                            logger.info('[TUTOR] Trainer %s accessing shared course %s from %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
                        else:
                            logger.info('[TUTOR] Trainer %s accessing shared course content %s from %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if course.author_id is None else str(course.author_id).replace("\n", "").replace("\r", "")))
                elif user.role == 'trainee':
                    # CRITICAL FIX: Trainees can only access PUBLISHED courses they're enrolled in
                    enrollment = db.query(TraineeAttempt).join(Course).filter(
                        TraineeAttempt.trainee_id == user.id,
                        TraineeAttempt.course_id == course_id,
                        Course.is_live == True  # Only published courses
                    ).first()
                    
                    if not enrollment:
                        raise HTTPException(status_code=403, detail="You don't have access to this course or it's not published yet.")
                    
                    # SEQUENTIAL ACCESS CONTROL: Check if trainee has completed and passed assessments for previous modules
                    if module_index > 1:  # Only check for modules after the first one
                        # Check if assessments are configured for this course
                        assessment_config = db.query(AssessmentConfig).filter(
                            AssessmentConfig.course_id == course_id
                        ).first()
                        
                        if assessment_config and assessment_config.assessment_scope == 'end_of_module':
                            # If assessments are configured, check if previous module assessment was passed
                            previous_module = module_index - 1
                            passed_assessment = db.query(TraineeAssessment).filter(
                                TraineeAssessment.trainee_id == user.id,
                                TraineeAssessment.course_id == course_id,
                                TraineeAssessment.module_index == previous_module,
                                TraineeAssessment.passed == True
                            ).first()
                            
                            if not passed_assessment:
                                # Check if module is completed but assessment not passed
                                completed_module = db.query(CourseProgress).filter(
                                    CourseProgress.user_id == user.id,
                                    CourseProgress.course_id == course_id,
                                    CourseProgress.completed_module_index == previous_module
                                ).first()
                                
                                if completed_module:
                                    logger.warning('[TUTOR] Trainee %s attempted to access module %s without passing assessment for module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), ("" if previous_module is None else str(previous_module).replace("\n", "").replace("\r", "")))
                                    raise HTTPException(
                                        status_code=403, 
                                        detail=f"You must pass the assessment for Module {previous_module} before accessing Module {module_index}. Please complete the assessment first."
                                    )
                                else:
                                    logger.warning('[TUTOR] Trainee %s attempted to access module %s without completing module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), ("" if previous_module is None else str(previous_module).replace("\n", "").replace("\r", "")))
                                    raise HTTPException(
                                        status_code=403, 
                                        detail=f"You must complete Module {previous_module} before accessing Module {module_index}. Please complete the modules in order."
                                    )
                            
                            logger.info('[TUTOR] ✓ Trainee %s has passed assessment for module %s, allowing access to module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if previous_module is None else str(previous_module).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
                        else:
                            # No assessments configured, use old completion-based logic
                            completed_modules = db.query(CourseProgress.completed_module_index).filter(
                                CourseProgress.user_id == user.id,
                                CourseProgress.course_id == course_id
                            ).all()
                            
                            completed_module_indices = {record[0] for record in completed_modules}
                            previous_module = module_index - 1
                            
                            if previous_module not in completed_module_indices:
                                logger.warning('[TUTOR] Trainee %s attempted to access module %s without completing module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")), ("" if previous_module is None else str(previous_module).replace("\n", "").replace("\r", "")))
                                raise HTTPException(
                                    status_code=403, 
                                    detail=f"You must complete Module {previous_module} before accessing Module {module_index}. Please complete the modules in order."
                                )
                            
                            logger.info('[TUTOR] ✓ Trainee %s has completed previous modules, allowing access to module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
                    
                    logger.info('[TUTOR] Trainee %s accessing enrolled published course %s, module %s', ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
                else:
                    raise HTTPException(status_code=403, detail="Access denied.")
        
        cached_response = get_cached_tutor_response(db, course_id, module_index, "explain_module", cache_module_content)
        
        if cached_response:
            logger.info(
                "[TUTOR] CACHE HIT - course_id=%s, module_index=%s, user=%s role=%s (shared S3 cache for all roles)",
                ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
                ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")),
                ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")),
                ("" if user.role is None else str(user.role).replace("\n", "").replace("\r", "")),
            )
            return cached_response
        
        logger.info("[TUTOR] ⚠️ CACHE MISS - Generating new explanation")
        
        # FIXED: For unpublished courses, generate tutor response on-demand
        if course_id != "generic" and course:
            if not course.is_live:
                logger.info('[TUTOR] Generating on-demand tutor response for unpublished course %s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
        
        # --- FIX START ---
        # Debug: Log the first 500 chars of module content
        logger.info('[TUTOR] Module content preview (first 500 chars): %s', ("" if req.module_content[:500] is None else str(req.module_content[:500]).replace("\n", "").replace("\r", "")))
        
        # CRITICAL FIX: Ensure re module is available (fix for UnboundLocalError)
        import re
        
        # Working copy of module content — may be cleaned up during image validation
        module_content = req.module_content
        
        # 1. Identify images - check both custom format and markdown format
        # Custom format: [IMAGE: s3_key]
        image_tags = _extract_image_tags(module_content)
        
        # Markdown format: ![alt](s3_key) - fallback for existing modules
        if len(image_tags) == 0:
            markdown_images = _extract_markdown_image_urls(module_content)
            if len(markdown_images) > 0:
                logger.info('[TUTOR] No [IMAGE: ...] tags found, but found %s markdown images', len(markdown_images))
                image_tags = markdown_images
        
        s3_to_signed_url_map = {}

        logger.info('[TUTOR] Found %s image tags in module content for user %s', len(image_tags), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")))
        if len(image_tags) > 0:
            logger.info('[TUTOR] Image tags found: %s', ("" if image_tags is None else str(image_tags).replace("\n", "").replace("\r", "")))
        logger.info('[TUTOR] S3 Bucket: %s, AWS Region: %s', ("" if S3_BUCKET_NAME is None else str(S3_BUCKET_NAME).replace("\n", "").replace("\r", "")), ("" if AWS_REGION is None else str(AWS_REGION).replace("\n", "").replace("\r", "")))

        # MODULE_ID CONTEXT VALIDATION: Only strip images from the tutor prompt that clearly
        # belong to a completely different source document. Use lenient matching to avoid
        # false positives — never delete from S3 here (that's too destructive).
        if module_content and image_tags and course_id != "generic" and course and course.source_doc_name:
            # Build a normalised version of the expected doc name for comparison
            raw_doc = course.source_doc_name
            # Normalise: lowercase, replace spaces/hyphens with underscores, strip extension
            import re as _re2
            def _norm(s):
                s = s.lower().strip()
                s = _re2.sub(r'[\s\-]+', '_', s)
                s = _re2.sub(r'[^a-z0-9_.]', '', s)
                return s

            expected_norm = _norm(raw_doc)
            # Also try without extension
            expected_stem = _norm(raw_doc.rsplit('.', 1)[0]) if '.' in raw_doc else expected_norm

            mismatched_keys = []
            for s3_key in image_tags:
                if '/extracted_images/' in s3_key:
                    parts = s3_key.split('/extracted_images/')
                    if len(parts) > 1:
                        doc_segment = parts[1].split('/')[0]
                        doc_segment_norm = _norm(doc_segment)
                        doc_segment_stem = _norm(doc_segment.rsplit('.', 1)[0]) if '.' in doc_segment else doc_segment_norm

                        # Accept if either the full name or stem matches (lenient)
                        matches = (
                            doc_segment_norm == expected_norm or
                            doc_segment_stem == expected_stem or
                            expected_stem in doc_segment_norm or
                            doc_segment_stem in expected_norm
                        )
                        if not matches:
                            mismatched_keys.append(s3_key)
                            logger.warning("[TUTOR MODULE_ID] Image doc '%s' != expected '%s' — stripping from prompt only: %s", ("" if doc_segment is None else str(doc_segment).replace("\n", "").replace("\r", "")), ("" if raw_doc is None else str(raw_doc).replace("\n", "").replace("\r", "")), ("" if s3_key is None else str(s3_key).replace("\n", "").replace("\r", "")))

            for s3_key in mismatched_keys:
                image_tags = [k for k in image_tags if k != s3_key]
                module_content = module_content.replace(f'[IMAGE: {s3_key}]', '')
                module_content = re.sub(rf'\n\s*\[IMAGE:\s*{re.escape(s3_key)}\s*\]\s*\n', '\n', module_content)
                # NOTE: Do NOT delete from S3 — the image may belong to another module in the same course

            if mismatched_keys:
                logger.info('[TUTOR MODULE_ID] Stripped %s mismatched image(s) from tutor prompt (not deleted from S3)', len(mismatched_keys))

        author_user_id = course.author_id if course else user.id
        module_content_with_signed_urls, s3_to_signed_url_map = enrich_module_content_for_tutor(
            module_content,
            author_user_id,
            course,
        )
        logger.info(
            f"[TUTOR] Prepared module content with {len(s3_to_signed_url_map)} image URL(s) "
            f"(cached vision preferred, parallel vision for cache misses)"
        )

        # Debug: Log the final content to see what IMAGE_URL tags are being sent
        image_url_count = module_content_with_signed_urls.count('[IMAGE_URL:')
        logger.info('[TUTOR] Final content contains %s [IMAGE_URL:] tags', ("" if image_url_count is None else str(image_url_count).replace("\n", "").replace("\r", "")))
        if image_url_count > 0:
            sample_urls = _extract_image_url_tags(module_content_with_signed_urls)
            logger.info("[TUTOR] Input IMAGE_URL tags: %s", ("" if sample_urls is None else str(sample_urls).replace("\n", "").replace("\r", "")))  # Log all input URLs

        # 4. Call the agent with the MODIFIED module content that has signed URLs
        logger.info('[TUTOR] Calling tutor agent with %s image URLs from S3', len(s3_to_signed_url_map))
        
        # CRITICAL FIX: Add explicit instruction to prevent AI from generating new image filenames
        image_instruction = ""
        if len(s3_to_signed_url_map) > 0:
            valid_image_urls = list(s3_to_signed_url_map.values())
            url_lines = "\n".join(f"- [IMAGE_URL: {u}]" for u in valid_image_urls)
            image_instruction = (
                "\n\nIMPORTANT: You MUST include ALL of these IMAGE_URL tags in your lecture, "
                "each exactly once, in the same order as the module, placed immediately after "
                "the section that discusses that diagram or table. "
                "Do NOT create new image filenames or modify existing ones:\n"
                f"{url_lines}\n"
            )
        
        full_prompt = f"{TUTOR_AGENT_INSTRUCTIONS}{image_instruction}\n\nHere is the module content:\n\n{module_content_with_signed_urls}"
        lecture_script = tutor_agent.run(full_prompt).content
        
        # CRITICAL FIX: Strip ALL fake/invalid image tags from the AI response.
        # Only keep [IMAGE_URL: ...] tags that exactly match our valid signed URLs.
        # Remove anything else — no fallbacks, no substitutions.
        valid_urls = set(s3_to_signed_url_map.values())
        
        # 1. Remove any [IMAGE_URL: ...] tags that are NOT in our valid set
        lecture_script = _strip_invalid_image_url_tags(lecture_script, valid_urls)
        
        # 2. Remove any [IMAGE: ...] tags the AI may have generated (these are raw S3 keys, not signed URLs)
        # These should have been replaced already, but strip any remaining ones
        leftover_image_tags = _extract_image_tags(lecture_script)
        if leftover_image_tags:
            for tag in leftover_image_tags:
                logger.warning('[TUTOR] Removing leftover [IMAGE: ...] tag: %s', ("" if tag is None else str(tag).replace("\n", "").replace("\r", "")))
            lecture_script = _replace_image_tags(lecture_script, lambda _key: "")
        
        # 3. Remove any markdown image syntax the AI may have generated
        lecture_script = _strip_non_api_markdown_images(lecture_script)
        
        # 4. Strip any leaked VISION ANALYSIS blocks — these are internal context and must never reach the frontend
        lecture_script = _strip_vision_analysis_blocks(lecture_script)
        # 4b. Remove placeholder/meta lines that should never be visible in tutor output.
        lecture_script = re.sub(r'^\s*(?:loading|generating|rendering|displaying)\s+(?:diagram|image|visual|figure)\b[^\n]*$', '', lecture_script, flags=re.IGNORECASE | re.MULTILINE)
        lecture_script = re.sub(r'^\s*(?:please\s+wait|one\s+moment)\b[^\n]*(?:diagram|image|visual|figure)\b[^\n]*$', '', lecture_script, flags=re.IGNORECASE | re.MULTILINE)
        lecture_script = re.sub(r'^\s*(?:image|diagram|visual)\s+(?:is|will be)\s+(?:loading|generated|rendered)\b[^\n]*$', '', lecture_script, flags=re.IGNORECASE | re.MULTILINE)
        # Clean up any triple+ blank lines left by the removal
        lecture_script = re.sub(r'\n{3,}', '\n\n', lecture_script).strip()

        # 5. Inject any module IMAGE_URL tags the model omitted (keeps document order)
        module_url_order = _extract_image_url_tags(module_content_with_signed_urls)
        present_urls = set(_extract_image_url_tags(lecture_script))
        missing_urls = [u for u in module_url_order if u in valid_urls and u not in present_urls]
        total_missing = len(missing_urls)
        for miss_idx, url in enumerate(missing_urls):
            section_title = ""
            pos = module_content_with_signed_urls.find(f"[IMAGE_URL: {url}]")
            if pos > 0:
                for line in reversed(module_content_with_signed_urls[:pos].split("\n")[-50:]):
                    line = line.strip()
                    if re.match(r"^#{1,6}\s", line):
                        section_title = re.sub(r"^#{1,6}\s*\*?\*?", "", line).replace("*", "").strip()
                        break
                    num = re.match(r"^(\d+)[\.\)]\s+(.+)", line)
                    if num:
                        section_title = num.group(2).strip()
                        break
            insert_tag = f"\n\n[IMAGE_URL: {url}]\n\n"
            inserted = False
            if section_title:
                title_pat = re.escape(section_title[:72])
                match = re.search(
                    rf"(^|\n)(#{1,4}\s+[^\n]*{title_pat}[^\n]*|[^\n]*{title_pat}[^\n]*)",
                    lecture_script,
                    flags=re.IGNORECASE | re.MULTILINE,
                )
                if match:
                    line_end = lecture_script.find("\n", match.end())
                    insert_at = line_end if line_end >= 0 else len(lecture_script)
                    lecture_script = lecture_script[:insert_at] + insert_tag + lecture_script[insert_at:]
                    inserted = True
                    logger.info("[TUTOR] Injected missing IMAGE_URL after section '%s': %s", ("" if section_title is None else str(section_title).replace("\n", "").replace("\r", "")), ("" if url is None else str(url).replace("\n", "").replace("\r", "")))
            if not inserted and len(lecture_script) > 0 and total_missing > 0:
                ratio = miss_idx / max(total_missing, 1)
                insert_at = min(len(lecture_script), int(len(lecture_script) * ratio))
                boundary = lecture_script.find("\n\n", insert_at)
                insert_at = boundary if boundary >= 0 else insert_at
                lecture_script = lecture_script[:insert_at] + insert_tag + lecture_script[insert_at:]
                inserted = True
                logger.info('[TUTOR] Injected missing IMAGE_URL at proportional position %s/%s: %s', ("" if miss_idx + 1 is None else str(miss_idx + 1).replace("\n", "").replace("\r", "")), ("" if total_missing is None else str(total_missing).replace("\n", "").replace("\r", "")), ("" if url is None else str(url).replace("\n", "").replace("\r", "")))
            if not inserted:
                lecture_script += insert_tag
                logger.info('[TUTOR] Appended missing IMAGE_URL at end of lecture: %s', ("" if url is None else str(url).replace("\n", "").replace("\r", "")))
            present_urls.add(url)
        
        ai_generated_urls = _extract_image_url_tags(lecture_script)
        logger.info('[TUTOR] Final response has %s valid IMAGE_URL tags', len(ai_generated_urls))
        logger.info("[TUTOR] ✓ Agent completed. Images served from S3 (no temp files)")
        
        response_data = {"lecture_script": lecture_script, "image_urls": list(s3_to_signed_url_map.values())}
        
        # OPTIMIZATION: Track token usage and cache response
        estimated_tokens = len(full_prompt.split()) + len(lecture_script.split())  # Rough estimate
        # Track token usage with accurate pricing
        estimated_input_tokens = len(full_prompt.split())
        estimated_output_tokens = len(lecture_script.split())
        # Use course_id (which is already defined) instead of undefined actual_course_id
        actual_course_id = course_id if course_id != "generic" else None
        
        try:
            track_token_usage(db, user.id, "tutor_query", estimated_input_tokens, estimated_output_tokens, actual_course_id, BEDROCK_GENERATION_MODEL)
            logger.info('[TUTOR] 📊 Tracked %s tokens for user %s, course %s', ("" if estimated_tokens is None else str(estimated_tokens).replace("\n", "").replace("\r", "")), ("" if user.id is None else str(user.id).replace("\n", "").replace("\r", "")), ("" if actual_course_id is None else str(actual_course_id).replace("\n", "").replace("\r", "")))
        except Exception as track_error:
            logger.error('[TUTOR] ❌ Failed to track token usage: %s', ("" if track_error is None else str(track_error).replace("\n", "").replace("\r", "")), exc_info=True)
        
        # Cache the response for future use (use actual course_id if provided)
        try:
            cache_tutor_response(db, course_id, module_index, "explain_module", cache_module_content, response_data, estimated_tokens, user.id)
            logger.info('[TUTOR] 💾 Cached response to course_id=%s, module=%s', ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")), ("" if module_index is None else str(module_index).replace("\n", "").replace("\r", "")))
        except Exception as cache_error:
            logger.error('[TUTOR] ⚠️ Failed to cache response: %s', ("" if cache_error is None else str(cache_error).replace("\n", "").replace("\r", "")), exc_info=True)
        
        return response_data
        
    except HTTPException:
        # Re-raise HTTPException as-is (don't convert to 500)
        raise
    except Exception as e:
        logger.error('[TUTOR] ✗ Fatal error in get_tutor_explanation: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate tutor explanation.")

@router.post("/tutor/clear-cache", tags=["Chat & AI Agents"])
def clear_tutor_cache(course_id: Optional[str] = None, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Clear tutor response cache for a specific course or all courses.
    Useful when course content is updated or image paths change.
    """
    try:
        if course_id:
            # Clear cache for specific course (both database and S3)
            cache_entries = db.query(TutorResponseCache).filter(TutorResponseCache.course_id == course_id).all()
            for cache_entry in cache_entries:
                try:
                    # Delete cached response from S3
                    s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
                    logger.info('[MANUAL CACHE CLEAR] Deleted S3 cache: %s', ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")))
                except Exception as e:
                    logger.warning('[MANUAL CACHE CLEAR] Failed to delete S3 cache %s: %s', ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")), e)
            deleted = db.query(TutorResponseCache).filter(TutorResponseCache.course_id == course_id).delete()
            db.commit()
            logger.info('[CACHE] Cleared %s tutor cache entries for course %s', ("" if deleted is None else str(deleted).replace("\n", "").replace("\r", "")), ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")))
            return {"message": f"Cleared {deleted} cache entries for course", "course_id": course_id}
        else:
            # Clear all cache for this user's courses (both database and S3)
            if user.role == 'trainer' or user.role == 'super_admin':
                # Get all courses by this trainer
                courses = db.query(Course).filter(Course.author_id == user.id).all()
                course_ids = [c.id for c in courses]
                # Delete S3 cache files first
                cache_entries = db.query(TutorResponseCache).filter(TutorResponseCache.course_id.in_(course_ids)).all()
                for cache_entry in cache_entries:
                    try:
                        # Delete cached response from S3
                        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
                        logger.info('[MANUAL CACHE CLEAR ALL] Deleted S3 cache: %s', ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")))
                    except Exception as e:
                        logger.warning('[MANUAL CACHE CLEAR ALL] Failed to delete S3 cache %s: %s', ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")), e)
                # Delete database entries
                deleted = db.query(TutorResponseCache).filter(TutorResponseCache.course_id.in_(course_ids)).delete(synchronize_session=False)
                db.commit()
                logger.info('[CACHE] Cleared %s tutor cache entries for %s courses', ("" if deleted is None else str(deleted).replace("\n", "").replace("\r", "")), len(course_ids))
                return {"message": f"Cleared {deleted} cache entries for {len(course_ids)} courses"}
            else:
                raise HTTPException(status_code=403, detail="Only trainers can clear cache")
    except Exception as e:
        logger.error('[CACHE] Error clearing cache: %s', e)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to clear cache")

@router.post("/tutor/cleanup", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat & AI Agents"])
def cleanup_temp_files(files: List[str], user: User = Depends(get_current_active_user)):
    """
    Deletes temporary image files created during a tutor session.
    """
    from utils.security_helpers import log_safe, unlink_file_under_base

    temp_root = Path(TEMP_DIR)
    for filename in files:
        try:
            if unlink_file_under_base(temp_root, filename):
                logger.info("Cleaned up temp file: %s", ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")))
        except ValueError:
            logger.warning(
                "Skipping potentially malicious filename in cleanup: %s",
                ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")),
            )
        except Exception as e:
            logger.error(
                "Error during temp file cleanup for %s: %s",
                ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")),
                e,
                exc_info=True,
            )
    return Response(status_code=204)


# REMOVED: Unused endpoints /tutor/generate-questions and /tutor/validate-answer
# These endpoints are no longer needed as assessment evaluation is handled by the assessment system

@router.get("/chat/sessions", response_model=List[ChatSessionModel], tags=["Chat & AI Agents"])
def get_sessions(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """Retrieves a list of all chat sessions for the currently authenticated user."""
    sessions = db.query(ChatSession).filter(ChatSession.user_id == user.id).order_by(ChatSession.created_at.desc()).all()
    return sessions

@router.post("/chat/sessions/clear-all/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat & AI Agents"])
def clear_all_sessions(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """Deletes all chat sessions for the currently authenticated user."""
    try:
        # First delete all chat messages for the user's sessions
        user_sessions = db.query(ChatSession).filter(ChatSession.user_id == user.id).all()
        session_ids = [session.id for session in user_sessions]
        
        if session_ids:
            # Delete messages first (foreign key constraint)
            db.query(ChatMessage).filter(ChatMessage.session_id.in_(session_ids)).delete(synchronize_session=False)
            
            # Then delete sessions
            db.query(ChatSession).filter(ChatSession.user_id == user.id).delete(synchronize_session=False)
        
        db.commit()
        logger.info('Cleared all chat sessions and messages for user %s', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")))
        return Response(status_code=204)
    except Exception as e:
        db.rollback()
        logger.error('Error clearing sessions for user %s: %s', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")), e)
        raise HTTPException(status_code=500, detail="Failed to clear sessions")

@router.get("/chat/sessions/{session_id}", response_model=ChatSessionWithHistory, tags=["Chat & AI Agents"])
def get_session(session_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves a specific chat session, including its full message history.

    - **Raises 404**: If the session is not found for the current user.
    """
    session = db.query(ChatSession).filter(ChatSession.id == session_id, ChatSession.user_id == user.id).first()
    if not session: raise HTTPException(status_code=404, detail="Chat session not found")
    
    # CRITICAL FIX: Filter out non-existent images from chat history
    # This prevents 404 errors when loading old messages with invalid image references
    for message in session.messages:
        if message.content:
            original_content = message.content
            if '[IMAGE:' in message.content:
                message.content = filter_non_existent_images(message.content)
            if message.role == "ai":
                message.content = strip_agent_meta_commentary(message.content)
            if original_content != message.content:
                logger.info(
                    '[CHAT HISTORY] Sanitized message %s in session %s',
                    log_safe(message.id),
                    log_safe(session_id),
                )
    
    return session

@router.post("/chat/sessions/{session_id}/update", response_model=ChatSessionModel, tags=["Chat & AI Agents"])
def update_session(session_id: str, req: SessionRenameRequest, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Renames a specific chat session.

    - **Raises 404**: If the session is not found for the current user.
    """
    session = db.query(ChatSession).filter(ChatSession.id == session_id, ChatSession.user_id == user.id).first()
    if not session: raise HTTPException(status_code=404, detail="Chat session not found")
    session.session_name = req.new_name
    db.commit()
    db.refresh(session)
    return session

@router.post("/chat/sessions/{session_id}/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat & AI Agents"])
def delete_session(session_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Deletes a specific chat session.

    - **Raises 404**: If the session is not found for the current user.
    """
    session = db.query(ChatSession).filter(ChatSession.id == session_id, ChatSession.user_id == user.id).first()
    if not session: raise HTTPException(status_code=404, detail="Chat session not found")
    db.delete(session)
    db.commit()
    return Response(status_code=204)
