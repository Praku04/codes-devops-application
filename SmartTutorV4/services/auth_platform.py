from __future__ import annotations

from dataclasses import dataclass

from core.runtime import *
from models.domain import JobStatus, Process, RateLimitSetting, User, UserSession
from services.jobs import cleanup_old_job_statuses
from utils.input_validation import normalize_email
from utils.jwt_access import create_access_token, decode_access_token
from utils.security_helpers import log_safe


def get_password_hash(password):
    return pwd_context.hash(password)


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def seed_initial_data():
    db = SessionLocal()
    try:
        default_processes = ["BPS", "RPA", "Finance", "HR", "IT Support"]
        for proc_name in default_processes:
            if not db.query(Process).filter(Process.name == proc_name).first():
                db.add(Process(name=proc_name))

        # Seeded from the one source of truth so the row a fresh environment starts with and the
        # fallback the middleware uses can never drift apart. They had: this seeded 100/minute for
        # trainees, which is what a UAT trainee tripped just loading their course list. Only the
        # three roles the Super Admin panel manages are seeded - "anonymous" is a pre-login
        # ceiling, not a setting, and giving it a row would surface it as an editable field.
        from services.rate_limits import DEFAULT_RATE_LIMITS

        default_rate_limits = {
            role: DEFAULT_RATE_LIMITS[role]
            for role in ("trainee", "trainer", "super_admin")
        }
        for role, limit in default_rate_limits.items():
            if not db.query(RateLimitSetting).filter(RateLimitSetting.role == role).first():
                db.add(RateLimitSetting(role=role, limit=limit))

        db.commit()

        # Seeded with the same canonical form as signup, and matched case-insensitively,
        # so an operator typing SUPER_ADMIN_EMAIL with different casing between releases
        # cannot create a second super admin row.
        super_admin_email = normalize_email(SUPER_ADMIN_EMAIL)
        super_admin_user = get_user_from_db(db, email=super_admin_email)
        if not super_admin_user:
            new_admin = User(
                email=super_admin_email,
                hashed_password=get_password_hash(SUPER_ADMIN_PASSWORD),
                role="super_admin",
                is_approved=True,
                process_id=None,
            )
            db.add(new_admin)
            db.commit()
            logger.info("Super admin user created successfully.")
    except Exception as e:
        logger.error("Could not seed initial data: %s", e, exc_info=True)
        db.rollback()
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    seed_initial_data()

    from services.rate_limits import warm_rate_limits_cache

    warm_rate_limits_cache()

    try:
        db = SessionLocal()
        try:
            active_jobs = db.query(JobStatus).filter(JobStatus.status.in_(["pending", "running"])).all()
            for job in active_jobs:
                job_statuses[job.job_id] = {
                    "status": job.status,
                    "progress_message": job.progress_message,
                    "result": job.result,
                    "error": job.error,
                    "token_usage": job.token_usage,
                    "user_id": job.user_id,
                    "cleanup_scheduled": job.cleanup_scheduled,
                }
            if active_jobs:
                logger.info("Loaded %s active job statuses from database", len(active_jobs))
            cleanup_old_job_statuses()
        finally:
            db.close()
    except Exception as e:
        logger.error("Failed to load job statuses on startup: %s", e)


def get_user_from_db(db: Session, email: str):
    """
    Look up a user by email, case-insensitively.

    See the note on the second definition of this function further down the module -
    both copies are kept in sync deliberately, because which one wins depends on
    import order.
    """
    normalized = normalize_email(email)
    if not normalized:
        return None
    return db.query(User).filter(func.lower(User.email) == normalized).first()


async def get_current_user_for_limiter(request: Request) -> Optional[User]:
    try:
        token = await oauth2_scheme(request)
        db = SessionLocal()
        try:
            payload = decode_access_token(token)
            email: str = payload.get("sub")
            if email:
                return get_user_from_db(db, email=email)
            return None
        finally:
            db.close()
    except Exception:
        return None


def rate_limit_key_func(request: Request) -> str:
    """Per-caller key for the slowapi route limits; see the fuller copy below, which wins."""
    try:
        from services.rate_limits import resolve_request_rate_limit_identity

        role, identity = resolve_request_rate_limit_identity(request)
        return f"{role}:{identity}"
    except Exception as e:
        logger.warning("Rate limit key function error: %s", log_safe(e))
        try:
            return get_remote_address(request)
        except Exception:
            return "unknown"


limiter_with_db = Limiter(key_func=rate_limit_key_func, default_limits=["200/minute"])
app.state.limiter = limiter_with_db


async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        token_use = payload.get("token_use")
        if token_use is not None and token_use != "access":
            raise credentials_exception
    except Exception:
        raise credentials_exception

    user = get_user_from_db(db, email=email)
    if user is None:
        raise credentials_exception

    # Reject forged role/uid claims that do not match the database user.
    claim_role = payload.get("role")
    if claim_role is not None and claim_role != user.role:
        raise credentials_exception
    claim_uid = payload.get("uid")
    if claim_uid is not None and claim_uid != user.id:
        raise credentials_exception

    # JWT alone is not enough — session must still be active (logout / idle / absolute).
    validate_and_touch_session(db, token)
    return user


async def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_approved and current_user.role != "trainee":
        role_label = current_user.role.replace("_", " ").title()
        raise HTTPException(status_code=403, detail=f"Your {role_label} account is pending approval.")
    return current_user


async def trainer_required(current_user: User = Depends(get_current_active_user)) -> User:
    if current_user.role not in ["trainer", "super_admin"]:
        raise HTTPException(status_code=403, detail="Trainer access required")
    return current_user


async def super_admin_required(current_user: User = Depends(get_current_active_user)) -> User:
    if current_user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return current_user


def create_user_session(db: Session, user_id: str, session_token: str, request: Request = None) -> UserSession:
    old_sessions = (
        db.query(UserSession)
        .filter(UserSession.user_id == user_id, UserSession.is_active == True)
        .order_by(desc(UserSession.login_time))
        .offset(5)
        .all()
    )
    for old_session in old_sessions:
        old_session.is_active = False
        old_session.logout_time = datetime.now(timezone.utc)

    session = UserSession(
        user_id=user_id,
        session_token=session_token,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    db.add(session)
    db.commit()
    return session


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_and_touch_session(db: Session, session_token: str) -> UserSession:
    """
    Require an active server-side session for the JWT (VAPT token/session controls).

    Enforces:
    - Session must exist and is_active (logout / revoke immediately invalidates the token)
    - Absolute timeout from login_time
    - Idle timeout from last_activity

    Also accepts ``previous_session_token`` so a request that left the browser before
    ``/auth/refresh`` finished rotating the JWT is not rejected with 401.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    session_expired = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Session expired. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    session = (
        db.query(UserSession)
        .filter(
            UserSession.is_active == True,
            or_(
                UserSession.session_token == session_token,
                UserSession.previous_session_token == session_token,
            ),
        )
        .first()
    )
    if not session:
        raise credentials_exception

    now = datetime.now(timezone.utc)
    login_time = _as_utc(session.login_time)
    last_activity = _as_utc(session.last_activity) or login_time

    absolute_minutes = int(SESSION_ABSOLUTE_TIMEOUT_MINUTES)
    idle_minutes = int(SESSION_IDLE_TIMEOUT_MINUTES)

    if absolute_minutes > 0 and login_time and (now - login_time) > timedelta(minutes=absolute_minutes):
        session.is_active = False
        session.logout_time = now
        db.commit()
        raise session_expired

    if idle_minutes > 0 and last_activity and (now - last_activity) > timedelta(minutes=idle_minutes):
        session.is_active = False
        session.logout_time = now
        db.commit()
        raise session_expired

    # Refresh the activity stamp at most once per SESSION_ACTIVITY_WRITE_INTERVAL_SECONDS. Every
    # authenticated request used to write and commit here, which the idle window (measured in
    # minutes) never needed - and because a caller's concurrent requests all update the SAME
    # user_sessions row, each one took a row lock the others had to wait behind while holding a
    # pooled connection. A trainee's three in-flight /speech/synthesize calls therefore serialized
    # on bookkeeping. The read above is unchanged, so a revoked or timed-out session is still
    # rejected on the very next request.
    staleness_seconds = (now - last_activity).total_seconds() if last_activity else None
    if staleness_seconds is None or staleness_seconds >= SESSION_ACTIVITY_WRITE_INTERVAL_SECONDS:
        session.last_activity = now
        db.commit()
    return session


def update_session_activity(db: Session, session_token: str):
    session = (
        db.query(UserSession)
        .filter(
            UserSession.is_active == True,
            or_(
                UserSession.session_token == session_token,
                UserSession.previous_session_token == session_token,
            ),
        )
        .first()
    )
    if session:
        session.last_activity = datetime.now(timezone.utc)
        db.commit()


def end_user_session(db: Session, session_token: str):
    session = (
        db.query(UserSession)
        .filter(
            UserSession.is_active == True,
            or_(
                UserSession.session_token == session_token,
                UserSession.previous_session_token == session_token,
            ),
        )
        .first()
    )
    if session:
        session.is_active = False
        session.logout_time = datetime.now(timezone.utc)
        session.previous_session_token = None
        db.commit()


def get_active_users(db: Session):
    active_sessions = (
        db.query(UserSession)
        .filter(UserSession.is_active == True)
        .order_by(desc(UserSession.last_activity))
        .all()
    )

    user_sessions = {}
    for session in active_sessions:
        if session.user_id not in user_sessions:
            user_sessions[session.user_id] = {
                "user_id": session.user_id,
                "user_email": session.user.email,
                "user_role": session.user.role,
                "login_time": session.login_time,
                "last_activity": session.last_activity,
                "ip_address": session.ip_address,
                "session_count": 1,
            }
        else:
            if session.last_activity > user_sessions[session.user_id]["last_activity"]:
                user_sessions[session.user_id].update(
                    {
                        "login_time": session.login_time,
                        "last_activity": session.last_activity,
                        "ip_address": session.ip_address,
                    }
                )
            user_sessions[session.user_id]["session_count"] += 1

    return list(user_sessions.values())


__all__ = [
    "get_password_hash",
    "verify_password",
    "seed_initial_data",
    "on_startup",
    "create_access_token",
    "get_user_from_db",
    "get_current_user_for_limiter",
    "rate_limit_key_func",
    "limiter_with_db",
    "get_current_user",
    "get_current_active_user",
    "trainer_required",
    "super_admin_required",
    "create_user_session",
    "validate_and_touch_session",
    "update_session_activity",
    "end_user_session",
    "get_active_users",
    "AuthenticatedCaller",
    "authenticated_caller_without_holding_db",
    "authenticated_user_id_without_holding_db",
]
from core.runtime import *
from models.domain import *
from schemas.domain import *
from services.jobs import *

# Re-imported explicitly AFTER the star-imports above. Everything from here down is a
# second definition of the functions defined earlier in this file, and it is this copy that
# wins at import time. The star-imports happen to re-export these two names today, but
# relying on that would be silently broken by any change to the imported modules'
# `__all__` - and the failure mode would be a NameError inside an auth path.
from utils.input_validation import normalize_email
from utils.security_helpers import log_safe

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
def get_password_hash(password): return pwd_context.hash(password)

# Safety net, not tuning: seeding is a handful of single-row inserts.
_SEED_LOCK_WAIT_SECONDS = 60


def seed_initial_data():
    """
    Seed the default processes, rate limits and super admin, one process at a time.

    Every worker seeds at startup and each step is check-then-insert, so workers starting together
    all saw "no super admin yet" and raced to insert it: one won, the rest failed with a
    UniqueViolation on ix_users_email. Taking the schema lock makes them take turns, so the later
    ones find the rows already there.
    """
    from core.schema_lock import schema_migration_lock

    try:
        with schema_migration_lock(engine, _SEED_LOCK_WAIT_SECONDS) as acquired:
            if not acquired:
                logger.warning("Another process is still seeding initial data; skipping it here.")
                return
            _seed_initial_data_locked()
    except Exception as e:
        logger.error("Could not seed initial data: %s", log_safe(e), exc_info=True)


def _seed_initial_data_locked():
    db = SessionLocal()
    try:
        # Seed Processes
        default_processes = ["BPS", "RPA", "Finance", "HR", "IT Support"]
        for proc_name in default_processes:
            if not db.query(Process).filter(Process.name == proc_name).first():
                db.add(Process(name=proc_name))
        
        # Seed Rate Limits.
        # Taken from services.rate_limits.DEFAULT_RATE_LIMITS so the row a fresh environment starts
        # with matches the fallback the middleware applies when a role has no row at all. The old
        # literal seeded trainees at 100/minute, which a UAT trainee tripped just loading their
        # course list; "anonymous" is deliberately not seeded because it is a pre-login ceiling
        # rather than an operator setting. Existing rows are left alone by the check below, so an
        # environment where the limits were already tuned in the Super Admin panel keeps its values.
        from services.rate_limits import DEFAULT_RATE_LIMITS

        default_rate_limits = {
            role: DEFAULT_RATE_LIMITS[role]
            for role in ('trainee', 'trainer', 'super_admin')
        }
        for role, limit in default_rate_limits.items():
            if not db.query(RateLimitSetting).filter(RateLimitSetting.role == role).first():
                db.add(RateLimitSetting(role=role, limit=limit))

        db.commit()

        # Seed Super Admin - stored in the canonical (lower-cased) form and matched
        # case-insensitively, so re-deploying with different casing in SUPER_ADMIN_EMAIL
        # cannot create a duplicate super admin row.
        super_admin_email = normalize_email(SUPER_ADMIN_EMAIL)
        super_admin_user = get_user_from_db(db, email=super_admin_email)
        if not super_admin_user:
            new_admin = User(
                email=super_admin_email,
                hashed_password=get_password_hash(SUPER_ADMIN_PASSWORD),
                role='super_admin',
                is_approved=True,
                process_id=None
            )
            db.add(new_admin)
            db.commit()
            logger.info("Super admin user created successfully.")
    except Exception as e:
        logger.error("Could not seed initial data: %s", log_safe(e), exc_info=True)
        db.rollback()
    finally:
        db.close()

@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    # Note: Temp image cleanup removed - images now served directly from S3
    seed_initial_data()
    
    # Load existing job statuses from database into memory cache
    try:
        db = SessionLocal()
        try:
            # Load active jobs (not completed/failed) into memory cache
            active_jobs = db.query(JobStatus).filter(
                JobStatus.status.in_(['pending', 'running'])
            ).all()
            
            for job in active_jobs:
                job_statuses[job.job_id] = {
                    'status': job.status,
                    'progress_message': job.progress_message,
                    'result': job.result,
                    'error': job.error,
                    'token_usage': job.token_usage,
                    'user_id': job.user_id,
                    'cleanup_scheduled': job.cleanup_scheduled
                }
            
            if active_jobs:
                logger.info("Loaded %s active job statuses from database", len(active_jobs))
            
            # Clean up old completed jobs
            cleanup_old_job_statuses()
            
        finally:
            db.close()
    except Exception as e:
        logger.error("Failed to load job statuses on startup: %s", log_safe(e))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==============================================================================
# 3.7. LOGGING HELPER
# ==============================================================================
def log_activity(db: Session, user_id: str, action: str, details: Optional[str] = None):
    """
    Persist an activity log row.

    Uses a short-lived side session so a logging failure (and its rollback) cannot
    expire/detach objects on the caller's request session — that pattern was causing
    opaque 500s when endpoints returned ORM instances after logging.
    """
    try:
        log_db = SessionLocal()
        try:
            log_entry = ActivityLog(user_id=user_id, action=action, details=details)
            log_db.add(log_entry)
            log_db.commit()
        except Exception as e:
            log_db.rollback()
            logger.error("Failed to log activity: %s", log_safe(e), exc_info=True)
        finally:
            log_db.close()
    except Exception as e:
        logger.error("Failed to open activity-log session: %s", log_safe(e), exc_info=True)


from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")

def verify_password(plain_password, hashed_password): return pwd_context.verify(plain_password, hashed_password)
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    from utils.jwt_access import create_access_token as _create

    return _create(data, expires_delta=expires_delta)


def get_user_from_db(db: Session, email: str):
    """
    Look up a user by email, case-insensitively.

    Email is case-insensitive in practice, so ``Phani@st.com`` and ``phani@st.com`` must
    resolve to the same account in both directions. Two things are needed for that:

      1. New rows are stored lower-cased (``UserBase._normalize_email``).
      2. This lookup compares ``lower(email)`` on BOTH sides, so accounts that were
         already created with mixed case - before the normalization existed - keep
         working and can sign in with any casing. Normalizing only the input would
         have locked those existing users out.

    ``func.lower`` is emitted as SQL, so the comparison happens in PostgreSQL rather
    than by loading rows into Python.
    """
    normalized = normalize_email(email)
    if not normalized:
        return None
    return db.query(User).filter(func.lower(User.email) == normalized).first()


# --- Custom Limiter Key ---
async def get_current_user_for_limiter(request: Request) -> Optional[User]:
    try:
        token = await oauth2_scheme(request)
        db = SessionLocal()
        try:
            payload = decode_access_token(token)
            email: str = payload.get("sub")
            if email: return get_user_from_db(db, email=email)
            return None
        finally: db.close()
    except Exception: return None

def rate_limit_key_func(request: Request) -> str:
    """
    Key the per-route slowapi limits by the CALLER, falling back to their address.

    get_remote_address alone is the socket peer, and this app runs behind an ALB with
    TRUST_FORWARDED_FOR_HEADER off, so the peer is the proxy: every user shared one bucket.
    With `@limiter_with_db.limit("30/minute")` on /auth/refresh that meant a cohort of 50-70
    trainees shared thirty token refreshes a minute between them, and the rest had their
    session dropped mid-lecture - an ordinary-use outage, not a control.

    An authenticated caller carries a uid in their JWT and can be counted individually, which
    is both fairer and stricter than lumping them together. An anonymous caller still falls
    back to the address, so the brute-force ceiling on the credential endpoints stays per
    source as VAPT requires. Resolution is delegated to the role middleware's own helper so
    the two enforcement points cannot disagree about who a caller is.

    Imported lazily, matching how the rest of this module reaches services.rate_limits, to keep
    import order at startup unchanged.
    """
    try:
        from services.rate_limits import resolve_request_rate_limit_identity

        role, identity = resolve_request_rate_limit_identity(request)
        return f"{role}:{identity}"
    except Exception as e:
        logger.warning("Rate limit key function error: %s", log_safe(e))
        # Never fail open onto one shared bucket for everybody: the address is still a key.
        try:
            return get_remote_address(request)
        except Exception:
            return "unknown"

limiter_with_db = Limiter(key_func=rate_limit_key_func, default_limits=["200/minute"])
app.state.limiter = limiter_with_db

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    # Nothing derived from the token is logged. `sub` and the token itself are
    # caller-controlled, so echoing them would let a caller write arbitrary text into the
    # log (Sonar python:S5145 / CodeQL log injection).
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        email: str = payload.get("sub")
        if email is None:
            logger.warning("[AUTH] Token payload missing 'sub' field")
            raise credentials_exception
        token_use = payload.get("token_use")
        if token_use is not None and token_use != "access":
            raise credentials_exception
    # Catch Exception, not just JWTError: a malformed token can raise ValueError/TypeError
    # from the decoder, and letting that escape would turn a bad token into a 500 that
    # leaks a traceback instead of a clean 401.
    except Exception:
        logger.warning("[AUTH] Token could not be validated.")
        raise credentials_exception
    user = get_user_from_db(db, email=email)
    if user is None:
        logger.warning("[AUTH] Token subject does not match any user.")
        raise credentials_exception

    claim_role = payload.get("role")
    if claim_role is not None and claim_role != user.role:
        logger.warning("[AUTH] Token role claim does not match user record")
        raise credentials_exception
    claim_uid = payload.get("uid")
    if claim_uid is not None and claim_uid != user.id:
        logger.warning("[AUTH] Token uid claim does not match user record")
        raise credentials_exception
    
    # Reject revoked / idle / absolute-expired sessions even if JWT signature is still valid.
    validate_and_touch_session(db, token)

    return user


async def get_current_user_allowing_expired_for_refresh(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """
    Auth dependency for ``POST /auth/refresh`` only.

    Accepts a JWT whose ``exp`` has passed, as long as signature/claims are valid and the
    DB session is still inside idle + absolute windows. Background tabs often miss the
    silent-renewal timer (browser throttling); without this, a 30-minute JWT expiry forces
    logout even though the 2-hour idle policy still allows the session.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token, verify_exp=False)
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        token_use = payload.get("token_use")
        if token_use is not None and token_use != "access":
            raise credentials_exception
    except Exception:
        logger.warning("[AUTH] Refresh token could not be validated.")
        raise credentials_exception

    user = get_user_from_db(db, email=email)
    if user is None:
        raise credentials_exception

    claim_role = payload.get("role")
    if claim_role is not None and claim_role != user.role:
        raise credentials_exception
    claim_uid = payload.get("uid")
    if claim_uid is not None and claim_uid != user.id:
        raise credentials_exception

    validate_and_touch_session(db, token)
    return user


async def get_current_active_user_for_refresh(
    current_user: User = Depends(get_current_user_allowing_expired_for_refresh),
) -> User:
    if not current_user.is_approved and current_user.role != "trainee":
        role_label = current_user.role.replace("_", " ").title()
        raise HTTPException(
            status_code=403,
            detail=f"Your {role_label} account is pending approval.",
        )
    return current_user

# The 403 detail while an account is still on its starting login. The front end matches this exact
# value to show the change-password screen rather than an error.
FIRST_LOGIN_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"


async def get_current_active_user_pending_password_change(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    The approval gate alone, for the few routes a user must reach before replacing a starting
    login: who am I, log out, and the password change itself. Everything else uses
    get_current_active_user.
    """
    if not current_user.is_approved and current_user.role != 'trainee':
        role_label = current_user.role.replace('_', ' ').title()
        raise HTTPException(status_code=403, detail=f"Your {role_label} account is pending approval.")
    return current_user


async def get_current_active_user(
    current_user: User = Depends(get_current_active_user_pending_password_change),
) -> User:
    """
    Approval gate plus the first-login rule. An admin-created account starts on the predictable
    St@<EmployeeID>#2026 login, so until it is changed every route behind this dependency -
    which trainer_required, super_admin_required and both *_without_holding_db helpers all go
    through - refuses the request. Enforced here, not only in the UI, so it cannot be skipped
    by calling the API directly.
    """
    if current_user.must_change_password:
        raise HTTPException(status_code=403, detail=FIRST_LOGIN_CHANGE_REQUIRED)
    return current_user

async def authenticated_user_id_without_holding_db(
    token: str = Depends(oauth2_scheme),
) -> str:
    """
    Auth gate for slow endpoints that do no database work of their own.

    ``get_current_user`` takes its session from ``Depends(get_db)``, and FastAPI finalizes a
    yield-dependency only AFTER the response has been sent. The pooled connection is therefore
    held for the entire request - so /speech/synthesize was keeping one checked out across a
    multi-second Amazon Polly call purely because it had checked a token before starting. One
    narration in flight per trainee, plus SPEECH_PREFETCH_AHEAD more, is enough for a single
    lecture to tie up most of the pool while doing no database work at all. That is a pool
    exhausted by waiting, not by queries, and no amount of connection headroom on the instance
    fixes it.

    The validation is deliberately NOT reimplemented here - a second copy of an auth check is
    how one silently drifts from the real one. This delegates to get_current_user and
    get_current_active_user with a session it owns and closes before the endpoint body runs,
    and returns the user id rather than the ORM instance, so nothing downstream can lazy-load
    against a closed session.

    Only for endpoints that need the caller authenticated and nothing else. Anything that
    touches the database must keep ``db: Session = Depends(get_db)`` and the normal dependency.
    """
    db = SessionLocal()
    try:
        user = await get_current_user(token=token, db=db)
        # Same approval gate as the standard dependency, by calling it rather than restating it.
        await get_current_active_user(current_user=user)
        return user.id
    finally:
        db.close()


@dataclass(frozen=True)
class AuthenticatedCaller:
    """
    The caller's identity, copied out of the ORM instance so it outlives the session.

    Plain strings on purpose: a detached ``User`` would raise DetachedInstanceError the first
    time something touched an unloaded attribute, and that failure would only show up on the
    rows that happen not to be in the identity map - the worst kind of intermittent bug.
    """

    id: str
    role: str
    email: str


async def authenticated_caller_without_holding_db(
    token: str = Depends(oauth2_scheme),
) -> AuthenticatedCaller:
    """
    Like ``authenticated_user_id_without_holding_db``, but also carries the caller's role.

    The streaming chat endpoints need the role to choose a system prompt, which is the only
    reason they were taking the full ``get_current_active_user`` + ``Depends(get_db)`` pair and
    therefore holding a pooled connection for the entire model answer. Returning a snapshot lets
    them release the connection before the slow work starts - see core.runtime.session_scope for
    what that cost in UAT.

    The check itself is delegated, not restated, for the same reason as the id-only version: a
    second copy of an auth check is how one silently drifts from the real one.
    """
    db = SessionLocal()
    try:
        user = await get_current_user(token=token, db=db)
        await get_current_active_user(current_user=user)
        return AuthenticatedCaller(id=user.id, role=user.role, email=user.email)
    finally:
        db.close()


async def trainer_required(current_user: User = Depends(get_current_active_user)) -> User:
    if current_user.role not in ['trainer', 'super_admin']:
        raise HTTPException(status_code=403, detail="Trainer access required")
    return current_user

async def super_admin_required(current_user: User = Depends(get_current_active_user)) -> User:
    if current_user.role != 'super_admin':
        raise HTTPException(status_code=403, detail="Super admin access required")
    return current_user

# ==============================================================================
# 6.4. USER SESSION MANAGEMENT
# ==============================================================================

def create_user_session(db: Session, user_id: str, session_token: str, request: Request = None) -> UserSession:
    """Create a new user session record"""
    # Clean up old sessions for this user (keep only the most recent 5)
    old_sessions = db.query(UserSession).filter(
        UserSession.user_id == user_id,
        UserSession.is_active == True
    ).order_by(desc(UserSession.login_time)).offset(5).all()
    
    for old_session in old_sessions:
        old_session.is_active = False
        old_session.logout_time = datetime.now(timezone.utc)
    
    # Create new session
    session = UserSession(
        user_id=user_id,
        session_token=session_token,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get("user-agent") if request else None
    )
    db.add(session)
    db.commit()
    return session

def update_session_activity(db: Session, session_token: str):
    """Update the last activity time for a session"""
    session = db.query(UserSession).filter(
        UserSession.is_active == True,
        or_(
            UserSession.session_token == session_token,
            UserSession.previous_session_token == session_token,
        ),
    ).first()
    
    if session:
        session.last_activity = datetime.now(timezone.utc)
        db.commit()

def end_user_session(db: Session, session_token: str):
    """Mark a user session as ended"""
    session = db.query(UserSession).filter(
        UserSession.is_active == True,
        or_(
            UserSession.session_token == session_token,
            UserSession.previous_session_token == session_token,
        ),
    ).first()

    if session:
        session.is_active = False
        session.logout_time = datetime.now(timezone.utc)
        session.previous_session_token = None
        db.commit()


def rotate_session_token(db: Session, old_token: str, new_token: str) -> UserSession:
    """
    Point an existing session row at a freshly minted access token.

    The access token IS the session token in this app (``create_user_session`` is called
    with the JWT), so renewing the JWT without moving the session row would orphan the new
    token: ``validate_and_touch_session`` would find no row for it and 401 immediately.

    The previous JWT is retained on ``previous_session_token`` so in-flight API calls that
    still present the old Bearer value (e.g. a /notifications poll overlapping refresh)
    keep working instead of racing to 401.

    ``login_time`` is deliberately carried over unchanged. That is what keeps
    SESSION_ABSOLUTE_TIMEOUT_MINUTES meaningful - refreshing slides the idle window but can
    never extend the absolute ceiling, so a renewing client cannot hold a session open
    forever (VAPT: Improper Token Expiration).
    """
    session = db.query(UserSession).filter(
        UserSession.is_active == True,
        or_(
            UserSession.session_token == old_token,
            UserSession.previous_session_token == old_token,
        ),
    ).first()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Keep the token we are replacing so concurrent requests with it still match.
    if session.session_token != new_token:
        session.previous_session_token = session.session_token
    session.session_token = new_token
    session.last_activity = datetime.now(timezone.utc)
    db.commit()
    return session


def get_active_users(db: Session):
    """Get all currently logged-in users"""
    active_sessions = db.query(UserSession).filter(
        UserSession.is_active == True
    ).order_by(desc(UserSession.last_activity)).all()
    
    # Group by user to avoid duplicates
    user_sessions = {}
    for session in active_sessions:
        if session.user_id not in user_sessions:
            user_sessions[session.user_id] = {
                'user_id': session.user_id,
                'user_email': session.user.email,
                'user_role': session.user.role,
                'login_time': session.login_time,
                'last_activity': session.last_activity,
                'ip_address': session.ip_address,
                'session_count': 1
            }
        else:
            # Keep the most recent session info
            if session.last_activity > user_sessions[session.user_id]['last_activity']:
                user_sessions[session.user_id].update({
                    'login_time': session.login_time,
                    'last_activity': session.last_activity,
                    'ip_address': session.ip_address
                })
            user_sessions[session.user_id]['session_count'] += 1
    
    return list(user_sessions.values())



__all__ = [name for name in globals() if not name.startswith('_')]
