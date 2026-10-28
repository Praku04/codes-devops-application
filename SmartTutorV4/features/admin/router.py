from __future__ import annotations

from fastapi import APIRouter

from core.runtime import *
from models.domain import *
from schemas.domain import *
from services.jobs import *
from services.auth_platform import *
from services.rate_limits import invalidate_rate_limits_cache, validate_rate_limit_string
from services.token_analytics import (
    build_admin_user_stat,
    get_user_token_analytics,
)
from services.tutor_cache import *
from services.documents import *
from services.knowledge_base import *
from services.agents import *
from services.agents import _execute_share_reports
from services.bulk_users import (
    MAX_UPLOAD_BYTES,
    TEMPLATE_FILENAME,
    XLSX_MEDIA_TYPE,
    AccountExistsError,
    BulkRow,
    build_template,
    check_rows,
    check_upload,
    create_admin_account,
    rows_from_payload,
    run_bulk_creation,
    summarize,
)
from services.knowledge_base import (
    _delete_data_for_source_document,
    _get_grouped_reports,
    _reindex_source_document,
    author_document_display_map,
    course_display_label,
    course_notification_id,
    course_notification_id_for_document,
    delete_source_document_completely,
    display_name_for_stored_document,
    display_name_from_map,
    friendly_document_name,
    sanitize_notification_message,
    shared_source_display_label,
)
from services.batch_voice_analytics import list_scenarios_published_to_batch

from features.assessment.router import (
    _adjudicate_mcq_correct_options,
    _assessment_display_percentage,
    _build_course_assessment_progress,
    _build_course_assessment_results_data,
    _build_course_assessment_attempt_history,
    _build_module_assessment_attempt_history,
    _build_trainee_assessment_question_results,
    _canonical_attempt_number,
    _count_modules_for_course,
    _get_modules_for_course,
    _get_trainee_course_assessment_state,
    _load_module_content_for_grading,
    _module_attempt_limits,
    _normalize_mcq_selections,
    _pick_latest_module_assessment,
    _question_to_dict,
    _resolve_assessment_display_attempts,
    _resolve_detail_attempt,
    build_module_assessment_attempts,
    build_s3_module_counts_for_authors,
    build_trainee_latest_assessments,
    end_of_course_attempts_left_by_course,
    get_total_modules_count,
    prefetch_s3_module_counts,
)
# Explicit import (not via the star imports above): every module count on an admin tab now
# comes from one per-request S3 pass instead of a scan per course. See services/module_index.py.
from services.module_index import (
    author_report_metadata,
    invalidate_author_report_metadata,
    register_derived_cache_key,
)
from utils.security_helpers import GENERIC_API_ERROR_MESSAGE, log_safe, strip_bracket_image_tags
from utils.upload_validation import safe_content_disposition

from features.trainer.router import (
    _is_course_fully_completed,
    _parse_assessment_questions,
    get_batch_analytics,
    get_trainee_batch_summary,
)

router = APIRouter()

# Memoized per request: the analytics tabs ask for the same course's module count once per
# trainee and once per attempt, so without this the index is re-filtered thousands of times.
_LOOSE_MODULE_COUNT_CACHE_KEY = "_admin_loose_module_count_by_course"
register_derived_cache_key(_LOOSE_MODULE_COUNT_CACHE_KEY)


def _count_course_modules_loose(db: Session, course) -> int:
    """
    Number of modules belonging to ``course``, matching a module's source document by exact
    normalized name **or** by either substring.

    Those substring fallbacks are wider than ``get_total_modules_count``'s exact match. They
    are kept because the analytics tabs have always counted this way, and tightening them here
    would silently change the completion figures admins already read.

    Reads the per-request S3 metadata index (``services/module_index.py``) instead of scanning
    S3, which is what made these tabs time out: the scan used to sit inside a per-attempt loop
    inside a per-trainee loop, costing trainees x attempts x objects S3 round-trips.
    """
    cache = db.info.setdefault(_LOOSE_MODULE_COUNT_CACHE_KEY, {})
    if course.id in cache:
        return cache[course.id]

    total_modules = 0
    try:
        # Parse source_documents so multi-document courses count all of their modules.
        course_docs_normalized = []
        try:
            if course.source_documents:
                source_docs_list = json.loads(course.source_documents)
                if isinstance(source_docs_list, list):
                    course_docs_normalized = [
                        normalize_document_name(doc) for doc in source_docs_list
                    ]
        except Exception as e:
            logger.warning('[ANALYTICS] Could not parse source_documents JSON: %s', log_safe(e))

        # Fallback to the single source_doc_name if source_documents is empty
        if not course_docs_normalized:
            course_docs_normalized = [normalize_document_name(course.source_doc_name)]

        for _s3_key, meta_data in author_report_metadata(db, course.author_id):
            source_doc_normalized = normalize_document_name(meta_data.get('source_doc', ''))
            if any(
                source_doc_normalized == course_doc_norm
                or course_doc_norm in source_doc_normalized
                or source_doc_normalized in course_doc_norm
                for course_doc_norm in course_docs_normalized
            ):
                total_modules += 1
    except Exception as e:
        # Previously a failure here left the count at 0 and the caller treated the course as
        # having no modules. Preserved, so a bad course cannot fail the whole tab.
        logger.warning('[ANALYTICS] Module count failed for course %s: %s', log_safe(course.id), log_safe(e))
        total_modules = 0

    cache[course.id] = total_modules
    return total_modules


def _count_progress_by_course(db: Session, trainee_id, course_ids) -> dict:
    """``{course_id: completed module count}`` for one trainee in a single query.

    The trainee-detail views used to run this count once per course inside their loop.
    """
    unique_course_ids = list({course_id for course_id in (course_ids or []) if course_id})
    if not trainee_id or not unique_course_ids:
        return {}
    return dict(
        db.query(CourseProgress.course_id, func.count(CourseProgress.id))
        .filter(
            CourseProgress.user_id == trainee_id,
            CourseProgress.course_id.in_(unique_course_ids),
        )
        .group_by(CourseProgress.course_id)
        .all()
    )


def _module_assessments_by_index(db: Session, trainee_id, course_id) -> dict:
    """``{module_index: (latest attempt, number of attempts)}`` for one trainee and course.

    Thin alias for the shared builder: the trainee's own progress view needs the same map, so the
    single query lives next to the other assessment batching helpers.
    """
    return build_module_assessment_attempts(db, trainee_id, course_id)


def _assessment_configs_by_course(db: Session, course_ids) -> dict:
    """``{course_id: AssessmentConfig}`` for a whole list of courses in a single query."""
    unique_course_ids = list({course_id for course_id in (course_ids or []) if course_id})
    if not unique_course_ids:
        return {}
    return {
        config.course_id: config
        for config in db.query(AssessmentConfig)
        .filter(AssessmentConfig.course_id.in_(unique_course_ids))
        .all()
    }


@router.get("/super-admin/check-migration-status", tags=["Super Admin: System Management"])
def check_migration_status(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Checks the status of module metadata migration.
    Returns statistics about modules with and without course_id.
    """
    try:
        total_modules = 0
        modules_with_course_id = 0
        modules_without_course_id = 0
        
        # Get all users
        users = db.query(User).all()
        total_users = len(users)
        
        # Get all courses
        total_courses = db.query(Course).count()
        
        logger.info('[CHECK MIGRATION] Checking migration status for %s users', ("" if total_users is None else str(total_users).replace("\n", "").replace("\r", "")))
        
        for user_obj in users:
            try:
                # List all modules for this user
                paginator = s3_client.get_paginator('list_objects_v2')
                report_pages = paginator.paginate(
                    Bucket=S3_BUCKET_NAME, 
                    Prefix=f"{user_obj.id}/generated_reports/"
                )
                
                for page in report_pages:
                    for obj in page.get('Contents', []):
                        key = obj['Key']
                        if key.endswith('.json'):
                            try:
                                # Read existing metadata
                                meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                                meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                                
                                total_modules += 1
                                
                                # Check if course_id exists
                                if meta_data.get('course_id'):
                                    modules_with_course_id += 1
                                else:
                                    modules_without_course_id += 1
                                    logger.debug('[CHECK MIGRATION] Module without course_id: %s', ("" if key is None else str(key).replace("\n", "").replace("\r", "")))
                                    
                            except Exception as e:
                                logger.error('[CHECK MIGRATION] Error reading module %s: %s', ("" if key is None else str(key).replace("\n", "").replace("\r", "")), e)
                                continue
                                
            except Exception as e:
                logger.error('[CHECK MIGRATION] Error processing user %s: %s', ("" if user_obj.id is None else str(user_obj.id).replace("\n", "").replace("\r", "")), e)
                continue
        
        needs_migration = modules_without_course_id > 0
        
        logger.info('[CHECK MIGRATION] Results: %s total, %s with course_id, %s without course_id', ("" if total_modules is None else str(total_modules).replace("\n", "").replace("\r", "")), ("" if modules_with_course_id is None else str(modules_with_course_id).replace("\n", "").replace("\r", "")), ("" if modules_without_course_id is None else str(modules_without_course_id).replace("\n", "").replace("\r", "")))
        
        return {
            "success": True,
            "total_modules": total_modules,
            "modules_with_course_id": modules_with_course_id,
            "modules_without_course_id": modules_without_course_id,
            "needs_migration": needs_migration,
            "total_users": total_users,
            "total_courses": total_courses,
            "message": f"Found {modules_without_course_id} module(s) that need migration." if needs_migration else "All modules have course_id assigned."
        }
        
    except Exception as e:
        logger.error("[CHECK MIGRATION] Error checking migration status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/super-admin/migrate-module-metadata", tags=["Super Admin: System Management"])
def migrate_module_metadata(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Migrates all existing module metadata to include course_id.
    This is a one-time migration to fix modules created before the course_id feature.
    """
    try:
        updated_count = 0
        skipped_count = 0
        error_count = 0
        
        # Get all courses
        courses = db.query(Course).all()
        logger.info('[MIGRATION] Starting module metadata migration for %s courses', len(courses))
        
        for course in courses:
            try:
                # Parse source documents
                source_docs = []
                if course.source_documents:
                    try:
                        source_docs = json.loads(course.source_documents)
                    except:
                        source_docs = [course.source_doc_name]
                else:
                    source_docs = [course.source_doc_name]
                
                # List all modules for this user
                paginator = s3_client.get_paginator('list_objects_v2')
                report_pages = paginator.paginate(
                    Bucket=S3_BUCKET_NAME, 
                    Prefix=f"{course.author_id}/generated_reports/"
                )
                
                for page in report_pages:
                    for obj in page.get('Contents', []):
                        key = obj['Key']
                        if key.endswith('.json'):
                            try:
                                # Read existing metadata
                                meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
                                meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                                source_doc = meta_data.get("source_doc")
                                
                                # Check if this module belongs to this course
                                if source_doc in source_docs:
                                    # Check if course_id already exists
                                    if meta_data.get("course_id"):
                                        skipped_count += 1
                                        continue
                                    
                                    # Add course_id to metadata
                                    meta_data["course_id"] = course.id
                                    
                                    # Save updated metadata
                                    s3_client.put_object(
                                        Bucket=S3_BUCKET_NAME, 
                                        Key=key, 
                                        Body=json.dumps(meta_data, indent=2).encode('utf-8'),
                                        ServerSideEncryption='AES256'
                                    )
                                    
                                    updated_count += 1
                                    logger.info('[MIGRATION] Updated %s with course_id %s', ("" if key is None else str(key).replace("\n", "").replace("\r", "")), ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")))
                                    
                            except Exception as e:
                                error_count += 1
                                logger.error('[MIGRATION] Error updating %s: %s', ("" if key is None else str(key).replace("\n", "").replace("\r", "")), e)
                                
            except Exception as e:
                logger.error('[MIGRATION] Error processing course %s: %s', ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")), e)
                error_count += 1
        
        logger.info('[MIGRATION] Complete: %s updated, %s skipped, %s errors', log_safe(updated_count), log_safe(skipped_count), log_safe(error_count))

        # This endpoint rewrites the very objects the per-request metadata index caches, so drop
        # the index — and every count cache registered against it — before anything else in this
        # request reads a module count.
        invalidate_author_report_metadata(db)

        log_activity(
            db,
            user.id,
            "MIGRATE_MODULE_METADATA",
            f"Updated {updated_count}, skipped {skipped_count}, errors {error_count}",
        )
        
        return {
            "success": True,
            "message": f"Migration complete: {updated_count} modules updated, {skipped_count} already had course_id, {error_count} errors",
            "updated": updated_count,
            "skipped": skipped_count,
            "errors": error_count
        }
        
    except Exception as e:
        logger.error("[MIGRATION] Migration failed: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/super-admin/processes", response_model=List[ProcessModel], tags=["Super Admin: System Management"])
def admin_get_processes(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Retrieves a list of all processes/teams in the system.
    """
    return db.query(Process).order_by(Process.name).all()

@router.post("/super-admin/processes", response_model=ProcessModel, status_code=status.HTTP_201_CREATED, tags=["Super Admin: System Management"])
def admin_create_process(req: ProcessCreate, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Creates a new process/team.

    - **Raises 400**: If a process with the same name already exists.
    """
    if db.query(Process).filter(Process.name == req.name).first():
        raise HTTPException(status_code=400, detail="Process name already exists")
    new_process = Process(name=req.name)
    db.add(new_process); db.commit(); db.refresh(new_process)
    log_activity(db, user.id, "CREATE_PROCESS", f"Created process '{req.name}'")
    return new_process

@router.post("/super-admin/processes/{process_id}/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Super Admin: System Management"])
def admin_delete_process(process_id: str, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Deletes a process.

    - **Raises 404**: If the process is not found.
    - **Raises 400**: If there are still users assigned to the process.
    """
    process = db.query(Process).filter(Process.id == process_id).first()
    if not process: raise HTTPException(status_code=404, detail="Process not found")
    if db.query(User).filter(User.process_id == process_id).count() > 0:
        raise HTTPException(status_code=400, detail="Cannot delete process with assigned users.")
    log_activity(db, user.id, "DELETE_PROCESS", f"Deleted process '{process.name}' (ID: {process_id})")
    db.delete(process); db.commit()
    return Response(status_code=204)

@router.get("/super-admin/activity-log", response_model=List[ActivityLogInfo], tags=["Super Admin: System Management"])
def get_activity_log(
    limit: int = 500,
    role: Optional[str] = None,
    hours: Optional[int] = None,
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db),
):
    """
    [Super Admin] Retrieves recent activity logs for all users and roles.
    Includes super admin, trainer, and trainee actions (login, content, assessments, admin panel, etc.).
    """
    try:
        from sqlalchemy.orm import joinedload

        safe_limit = max(1, min(limit, 1000))
        query = db.query(ActivityLog).options(joinedload(ActivityLog.user))

        if hours is not None and hours > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            query = query.filter(ActivityLog.timestamp >= cutoff)

        if role:
            query = query.join(User, ActivityLog.user_id == User.id).filter(User.role == role)

        logs = query.order_by(desc(ActivityLog.timestamp)).limit(safe_limit).all()

        logger.info(
            '[ACTIVITY LOG] Returning %s entries (limit=%s, role=%s, hours=%s)',
            len(logs),
            safe_limit,
            log_safe(role or 'all'),
            log_safe(hours if hours is not None else 'all'),
        )

        # Activity details quote the document a trainer acted on, and that is the randomized
        # storage name (VAPT #4). One merged map for the trainers in this page resolves them to
        # the filenames that were uploaded; anything unresolvable degrades to a generic label
        # rather than a hex string. Stored rows are never rewritten - this is display only.
        display_map = author_document_display_map(db, {log.user_id for log in logs})

        return [
            ActivityLogInfo(
                user_email=log.user.email,
                user_role=log.user.role,
                action=log.action,
                details=sanitize_notification_message(log.details, display_map),
                timestamp=log.timestamp,
            )
            for log in logs
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error loading activity log: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/super-admin/live-activity", tags=["Super Admin: System Management"])
def get_live_activity(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Shows users who are currently active (last activity within 30 minutes).
    This shows truly active users, not just logged-in sessions.
    """
    try:
        # Get users with recent activity (last 30 minutes)
        thirty_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
        
        active_sessions = db.query(UserSession).filter(
            UserSession.is_active == True,
            UserSession.last_activity >= thirty_minutes_ago
        ).order_by(desc(UserSession.last_activity)).all()
        
        # Group by user to avoid duplicates and get user details
        user_activity = {}
        for session in active_sessions:
            if session.user_id not in user_activity:
                # Get user's process name
                process_name = session.user.process.name if session.user.process else "No Process"
                
                user_activity[session.user_id] = {
                    'user_id': session.user_id,
                    'user_email': session.user.email,
                    'user_role': session.user.role,
                    'process_name': process_name,
                    'process_id': session.user.process_id,
                    'last_activity': session.last_activity,
                    'login_time': session.login_time,
                    'active_sessions': 1
                }
            else:
                # Keep the most recent activity and count sessions
                if session.last_activity > user_activity[session.user_id]['last_activity']:
                    user_activity[session.user_id]['last_activity'] = session.last_activity
                    user_activity[session.user_id]['login_time'] = session.login_time
                user_activity[session.user_id]['active_sessions'] += 1
        
        active_users = list(user_activity.values())
        active_users.sort(key=lambda x: x['last_activity'], reverse=True)
        
        # Get unique processes and roles for filters
        processes = db.query(Process).all()
        roles = ['super_admin', 'trainer', 'trainee']
        
        logger.info('[LIVE ACTIVITY] %s users active in last 30 minutes', len(active_users))
        
        return {
            "success": True,
            "active_users_count": len(active_users),
            "time_window": "Active in last 30 minutes",
            "users": active_users,
            "filters": {
                "processes": [{"id": p.id, "name": p.name} for p in processes],
                "roles": roles
            }
        }
    except Exception as e:
        logger.error("Error getting live activity: %s", log_safe(e), exc_info=True)
        return {
            "success": False,
            "error": "Failed to load live activity",
            "active_users_count": 0,
            "users": [],
            "filters": {"processes": [], "roles": []}
        }

@router.get("/super-admin/sharing-log", tags=["Super Admin: System Management"])
def admin_get_sharing_log(process_id: Optional[str] = None, role: Optional[str] = None, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Retrieves a log of all sharing activities across the system.
    Includes both course shares and module shares.
    - **process_id**: Optional filter by process ID (filters by sharer's process)
    - **role**: Optional filter by sharer's role (trainer/super_admin)
    """
    # Query for shared reports (modules only). The sharer, their process and the recipient are
    # all read for every row below, so they are joined in rather than lazy-loaded one at a time.
    from sqlalchemy.orm import joinedload

    reports_query = db.query(SharedReport).join(User, SharedReport.sharer_id == User.id).options(
        joinedload(SharedReport.sharer).joinedload(User.process),
        joinedload(SharedReport.recipient),
    )


    # Apply filters
    if process_id and process_id != "":
        reports_query = reports_query.filter(User.process_id == process_id)
    if role and role != "":
        reports_query = reports_query.filter(User.role == role)
    
    report_shares = reports_query.order_by(SharedReport.shared_at.desc()).all()
    
    # Query for shared courses (complete courses)
    courses_query = db.query(SharedCourse).join(User, SharedCourse.sharer_id == User.id).options(
        joinedload(SharedCourse.sharer).joinedload(User.process),
        joinedload(SharedCourse.recipient),
        joinedload(SharedCourse.course),
    )


    # Apply filters
    if process_id and process_id != "":
        courses_query = courses_query.filter(User.process_id == process_id)
    if role and role != "":
        courses_query = courses_query.filter(User.role == role)
    
    course_shares = courses_query.order_by(SharedCourse.shared_at.desc()).all()
    
    # Combine both types of shares
    all_shares = []
    
    # The course behind each module share, in one query instead of one per share. Keyed on the
    # same (author, source document) pair the per-share lookup used, first row seen winning.
    shared_course_by_key = {}
    if report_shares:
        sharer_ids = {s.sharer_id for s in report_shares if s.sharer_id}
        shared_doc_names = {s.source_doc_name for s in report_shares if s.source_doc_name}
        if sharer_ids and shared_doc_names:
            for course_row in db.query(Course).filter(
                Course.author_id.in_(sharer_ids),
                Course.source_doc_name.in_(shared_doc_names),
            ).all():
                shared_course_by_key.setdefault(
                    (course_row.author_id, course_row.source_doc_name), course_row
                )

    # Add module shares
    for s in report_shares:
        # Get course title for better display
        course = shared_course_by_key.get((s.sharer_id, s.source_doc_name))

        course_title = shared_source_display_label(db, s.sharer_id, s.source_doc_name, course)
        
        all_shares.append({
            "source_doc": course_title,  # FIXED: Show course title instead of filename
            "share_type": "Modules Only",
            "sharer": s.sharer.email, 
            "recipient": s.recipient.email, 
            "timestamp": s.shared_at, 
            "status": s.status,
            "reason": s.reason,
            "sharer_process": s.sharer.process.name if s.sharer.process else "N/A",
            "sharer_role": s.sharer.role
        })
    
    # Add course shares
    for s in course_shares:
        course_title = course_display_label(s.course)
        
        all_shares.append({
            "source_doc": course_title,  # FIXED: Show course title instead of filename
            "share_type": "Complete Course",
            "sharer": s.sharer.email, 
            "recipient": s.recipient.email, 
            "timestamp": s.shared_at, 
            "status": s.status,
            "reason": s.reason,
            "sharer_process": s.sharer.process.name if s.sharer.process else "N/A",
            "sharer_role": s.sharer.role
        })
    
    # Sort by timestamp (most recent first)
    all_shares.sort(key=lambda x: x['timestamp'], reverse=True)

    # ── Analytics summary ─────────────────────────────────────────────────────
    from collections import defaultdict
    status_counts  = defaultdict(int)
    type_counts    = defaultdict(int)
    process_counts = defaultdict(int)
    sharer_counts  = defaultdict(int)
    doc_counts     = defaultdict(int)
    day_counts     = defaultdict(int)

    for item in all_shares:
        status_counts[item["status"] or "pending"] += 1
        type_counts[item["share_type"]] += 1
        process_counts[item["sharer_process"]] += 1
        sharer_counts[item["sharer"]] += 1
        doc_counts[item["source_doc"]] += 1
        try:
            ts = item["timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            day = ts.strftime("%m/%d")
            day_counts[day] += 1
        except:
            pass

    # Last 14 days timeline
    now_utc = datetime.now(timezone.utc)
    fourteen_days_ago = now_utc - timedelta(days=14)
    shares_by_day = []
    for i in range(13, -1, -1):
        d = (now_utc - timedelta(days=i)).strftime("%m/%d")
        shares_by_day.append({"day": d, "count": day_counts.get(d, 0)})

    analytics = {
        "total": len(all_shares),
        "by_status": dict(status_counts),
        "by_type": dict(type_counts),
        "by_process": [{"process": k, "count": v} for k, v in sorted(process_counts.items(), key=lambda x: -x[1])],
        "top_sharers": [{"email": k, "count": v} for k, v in sorted(sharer_counts.items(), key=lambda x: -x[1])[:8]],
        "top_courses": [{"course": k, "count": v} for k, v in sorted(doc_counts.items(), key=lambda x: -x[1])[:8]],
        "shares_by_day": shares_by_day,
    }

    return {"shares": all_shares, "analytics": analytics}

@router.get("/super-admin/stats", tags=["Super Admin: System Management"])
def admin_get_stats(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Retrieves comprehensive statistics about the entire system.
    """
    from collections import defaultdict

    def safe_count(q):
        try: return q.count()
        except: return 0

    # ── Core counts ──────────────────────────────────────────────────────────
    total_users       = safe_count(db.query(User).filter(User.is_approved == True))
    total_trainers    = safe_count(db.query(User).filter(User.role == 'trainer',  User.is_approved == True))
    total_trainees    = safe_count(db.query(User).filter(User.role == 'trainee',  User.is_approved == True))
    total_courses     = safe_count(db.query(Course).filter(Course.is_live == True))
    total_batches     = safe_count(db.query(TraineeBatch).filter(TraineeBatch.is_active == True))
    total_processes   = safe_count(db.query(Process))
    pending_approvals = safe_count(db.query(User).filter(User.is_approved == False))

    # ── Sharing stats ─────────────────────────────────────────────────────────
    from sqlalchemy.orm import joinedload

    try:
        # The loops below read s.sharer, s.sharer.process, s.recipient and s.course. Without
        # joinedload each of those was a separate query per share row, so a system with a few
        # hundred shares spent thousands of round-trips here before the tab could render.
        report_shares = db.query(SharedReport).options(
            joinedload(SharedReport.sharer).joinedload(User.process),
            joinedload(SharedReport.recipient),
        ).all()
        course_shares = db.query(SharedCourse).options(
            joinedload(SharedCourse.sharer).joinedload(User.process),
            joinedload(SharedCourse.recipient),
            joinedload(SharedCourse.course),
        ).all()
    except Exception:
        report_shares, course_shares = [], []

    total_shares = len(report_shares) + len(course_shares)
    shares_by_type = {"Modules Only": len(report_shares), "Complete Course": len(course_shares)}
    status_counts = defaultdict(int)
    process_counts = defaultdict(int)
    course_share_counts = defaultdict(int)

    for s in report_shares:
        try:
            status_counts[s.status or "pending"] += 1
            proc = s.sharer.process.name if s.sharer and s.sharer.process else "N/A"
            process_counts[proc] += 1
            course_share_counts[s.source_doc_name] += 1
        except Exception:
            pass

    for s in course_shares:
        try:
            status_counts[s.status or "pending"] += 1
            proc = s.sharer.process.name if s.sharer and s.sharer.process else "N/A"
            process_counts[proc] += 1
            title = course_display_label(s.course) if s.course else "Unknown"
            course_share_counts[title] += 1
        except Exception:
            pass

    shares_by_status   = dict(status_counts)

    # Build drill-down data for shares_by_process and top_shared_courses
    process_shares_detail = defaultdict(list)   # process_name -> list of share records
    course_shares_detail  = defaultdict(list)   # course_title -> list of share records

    for s in report_shares:
        try:
            proc = s.sharer.process.name if s.sharer and s.sharer.process else "N/A"
            sharer_email = s.sharer.email if s.sharer else "Unknown"
            recipient_email = s.recipient.email if s.recipient else "Unknown"
            process_shares_detail[proc].append({
                "sharer": sharer_email, "recipient": recipient_email,
                "course": s.source_doc_name, "type": "Modules", "status": s.status or "pending"
            })
            course_shares_detail[s.source_doc_name].append({
                "sharer": sharer_email, "recipient": recipient_email,
                "type": "Modules", "status": s.status or "pending", "process": proc
            })
        except Exception:
            pass

    for s in course_shares:
        try:
            proc = s.sharer.process.name if s.sharer and s.sharer.process else "N/A"
            sharer_email = s.sharer.email if s.sharer else "Unknown"
            recipient_email = s.recipient.email if s.recipient else "Unknown"
            title = course_display_label(s.course) if s.course else "Unknown"
            process_shares_detail[proc].append({
                "sharer": sharer_email, "recipient": recipient_email,
                "course": title, "type": "Full Course", "status": s.status or "pending"
            })
            course_shares_detail[title].append({
                "sharer": sharer_email, "recipient": recipient_email,
                "type": "Full Course", "status": s.status or "pending", "process": proc
            })
        except Exception:
            pass

    shares_by_process  = [{"process": k, "count": v, "shares": process_shares_detail.get(k, [])}
                          for k, v in sorted(process_counts.items(), key=lambda x: -x[1])]
    top_shared_courses = [{"course": k, "count": v, "shares": course_shares_detail.get(k, [])}
                          for k, v in sorted(course_share_counts.items(), key=lambda x: -x[1])[:8]]

    # ── Activity by day (last 14 days) ────────────────────────────────────────
    activity_by_day = []
    try:
        now_utc = datetime.now(timezone.utc)
        fourteen_days_ago = now_utc - timedelta(days=14)
        recent_logs = db.query(ActivityLog).all()
        day_counts = defaultdict(int)
        for log in recent_logs:
            try:
                ts = log.timestamp
                # Normalize to UTC-aware datetime
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= fourteen_days_ago:
                    day_counts[ts.strftime("%m/%d")] += 1
            except Exception:
                pass
        for i in range(13, -1, -1):
            d = (now_utc - timedelta(days=i)).strftime("%m/%d")
            activity_by_day.append({"day": d, "count": day_counts.get(d, 0)})
    except Exception:
        now_utc = datetime.now(timezone.utc)
        for i in range(13, -1, -1):
            d = (now_utc - timedelta(days=i)).strftime("%m/%d")
            activity_by_day.append({"day": d, "count": 0})

    # ── Course creation by month with drill-down details ─────────────────────
    user_growth_by_month = []
    try:
        now = datetime.utcnow()
        months = []
        for i in range(5, -1, -1):
            m = now.month - i
            y = now.year
            while m <= 0:
                m += 12
                y -= 1
            months.append((y, m, datetime(y, m, 1).strftime("%b %Y")))

        six_months_ago = datetime(months[0][0], months[0][1], 1)
        new_courses = db.query(Course).filter(Course.created_at >= six_months_ago).all()

        # Build author map for all relevant courses
        author_ids = list({c.author_id for c in new_courses if c.author_id})
        author_map = {}
        if author_ids:
            authors = db.query(User.id, User.email, User.role).filter(User.id.in_(author_ids)).all()
            author_map = {a.id: {"email": a.email, "role": a.role} for a in authors}

        # Build process map
        process_ids = list({c.process_id for c in new_courses if c.process_id})
        process_map = {}
        if process_ids:
            processes = db.query(Process.id, Process.name).filter(Process.id.in_(process_ids)).all()
            process_map = {p.id: p.name for p in processes}

        month_courses = defaultdict(list)
        for c in new_courses:
            if c.created_at:
                try:
                    ts = c.created_at.replace(tzinfo=None) if c.created_at.tzinfo else c.created_at
                    label = ts.strftime("%b %Y")
                    author = author_map.get(c.author_id, {"email": "Unknown", "role": "unknown"})
                    month_courses[label].append({
                        "title": course_display_label(c),
                        "author": author["email"],
                        "author_role": author["role"],
                        "process": process_map.get(c.process_id, "N/A"),
                        "is_live": c.is_live,
                        "date": ts.strftime("%b %d, %Y"),
                    })
                except Exception:
                    pass

        for _, _, label in months:
            courses_this_month = month_courses.get(label, [])
            user_growth_by_month.append({
                "month": label,
                "count": len(courses_this_month),
                "courses": courses_this_month
            })
    except Exception as e:
        logger.warning('[STATS] user_growth_by_month error: %s', e)
        now = datetime.utcnow()
        for i in range(5, -1, -1):
            m = now.month - i
            y = now.year
            while m <= 0:
                m += 12
                y -= 1
            user_growth_by_month.append({"month": datetime(y, m, 1).strftime("%b %Y"), "count": 0, "courses": []})

    # ── Active users (last 30 days) ───────────────────────────────────────────
    active_trainees_30d = 0
    try:
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        active_trainees_30d = db.query(func.count(func.distinct(ActivityLog.user_id))).filter(
            ActivityLog.timestamp >= thirty_days_ago
        ).scalar() or 0
    except Exception:
        pass

    # ── Trainee course status breakdown (DB-only) ────────────────────────────
    trainees_fully_done = 0
    trainees_in_progress = 0
    trainees_not_started = 0
    try:
        all_trainees = db.query(User).filter(User.role == 'trainee', User.is_approved == True).all()

        # Who has an attempt, how much progress and whether anything was ever passed - three
        # grouped queries for every trainee at once. This loop used to run four queries per
        # trainee, which on a system with a few hundred trainees was thousands of round-trips
        # and the single slowest thing on the super-admin dashboard.
        trainees_with_attempts = {
            row[0] for row in db.query(TraineeAttempt.trainee_id).distinct().all()
        }
        progress_counts_by_trainee = dict(
            db.query(CourseProgress.user_id, func.count(CourseProgress.id))
            .group_by(CourseProgress.user_id)
            .all()
        )
        trainees_with_a_pass = {
            row[0] for row in db.query(TraineeAssessment.trainee_id)
            .filter(TraineeAssessment.passed == True)
            .distinct()
            .all()
        }

        for t in all_trainees:
            if t.id not in trainees_with_attempts:
                continue
            total_any_progress = progress_counts_by_trainee.get(t.id, 0)
            # "Fully done" = has at least one assessment passed OR has progress in a no-assessment course
            has_passed = t.id in trainees_with_a_pass
            if has_passed:
                trainees_fully_done += 1
            elif total_any_progress > 0:
                trainees_in_progress += 1
            else:
                trainees_not_started += 1
    except Exception as e:
        logger.warning('[STATS] trainee status breakdown error: %s', e)

    # ── Assessment stats: final outcome per trainee per course (latest attempt) ──
    total_passed_count, total_failed_count = 0, 0
    try:
        all_assessments = db.query(TraineeAssessment).all()
        # Group by (trainee_id, course_id) — take latest attempt per trainee per course
        latest_per_trainee_course = {}
        for a in all_assessments:
            key = (a.trainee_id, a.course_id)
            if key not in latest_per_trainee_course or a.completed_at > latest_per_trainee_course[key].completed_at:
                latest_per_trainee_course[key] = a
        total_passed_count = sum(1 for a in latest_per_trainee_course.values() if a.passed)
        total_failed_count = sum(1 for a in latest_per_trainee_course.values() if not a.passed)
    except Exception:
        pass

    # ── Course completion rate (trainees who completed all modules in at least one course) ──
    # ── Course completion rate: trainees who passed at least one course fully ──
    completion_rate = 0.0
    try:
        total_enrolled = safe_count(db.query(func.distinct(TraineeAttempt.trainee_id)))
        if total_enrolled > 0:
            # "Fully done" = has at least one passed TraineeAssessment (latest per course)
            # OR has module progress in a course with no assessment configured
            passed_trainee_ids = set(
                r[0] for r in db.query(func.distinct(TraineeAssessment.trainee_id)).filter(
                    TraineeAssessment.passed == True
                ).all()
            )
            # Also count trainees with progress in no-assessment courses
            # One query for the live courses and one for the configured ones, instead of an
            # AssessmentConfig lookup per live course.
            live_course_ids = {
                r[0] for r in db.query(Course.id).filter(Course.is_live == True).all()
            }
            configured_course_ids = {
                r[0] for r in db.query(AssessmentConfig.course_id).all()
            }
            no_assess_course_ids = live_course_ids - configured_course_ids
            progress_trainee_ids = set()
            if no_assess_course_ids:
                progress_trainee_ids = set(
                    r[0] for r in db.query(func.distinct(CourseProgress.user_id)).filter(
                        CourseProgress.course_id.in_(no_assess_course_ids)
                    ).all()
                )
            fully_done = len(passed_trainee_ids | progress_trainee_ids)
            completion_rate = round(fully_done / total_enrolled * 100, 1)
    except Exception:
        pass

    # ── Total modules (count source docs across all courses) ──────────────────
    total_modules = 0
    try:
        for c in db.query(Course).all():
            if c.source_documents:
                try:
                    docs = json.loads(c.source_documents)
                    total_modules += len(docs) if isinstance(docs, list) else 1
                except Exception:
                    total_modules += 1
            else:
                total_modules += 1
    except Exception:
        pass

    # ── Top trainers by course count ──────────────────────────────────────────
    top_trainers = []
    try:
        # joinedload(process) because the loop reads t.process.name, which was one query per
        # trainer on its own.
        trainers = db.query(User).options(joinedload(User.process)).filter(
            User.role.in_(['trainer', 'super_admin']), User.is_approved == True
        ).all()

        # Every trainer's courses in one query instead of one query per trainer.
        courses_by_author = {}
        trainer_ids = [t.id for t in trainers]
        if trainer_ids:
            for course in db.query(Course).filter(Course.author_id.in_(trainer_ids)).all():
                courses_by_author.setdefault(course.author_id, []).append(course)

        trainer_stats = []
        for t in trainers:
            courses = courses_by_author.get(t.id, [])
            course_count = len(courses)
            live_count   = sum(1 for c in courses if c.is_live)
            if course_count > 0:
                process_name = t.process.name if t.process else "N/A"
                trainer_stats.append({
                    "email": t.email,
                    "role": t.role,
                    "courses": course_count,
                    "live": live_count,
                    "process": process_name,
                    "course_list": [{"title": course_display_label(c), "is_live": c.is_live} for c in courses[:20]]
                })
        top_trainers = sorted(trainer_stats, key=lambda x: -x["courses"])[:8]
    except Exception:
        pass

    # ── Process breakdown (trainees + courses per process) ────────────────────
    process_breakdown = []
    try:
        processes = db.query(Process).all()

        # Three grouped queries rather than three queries per process.
        def counts_by_process(query):
            return dict(query.all())

        trainee_counts = counts_by_process(
            db.query(User.process_id, func.count(User.id))
            .filter(User.role == 'trainee', User.is_approved == True)
            .group_by(User.process_id)
        )
        trainer_counts = counts_by_process(
            db.query(User.process_id, func.count(User.id))
            .filter(User.role == 'trainer', User.is_approved == True)
            .group_by(User.process_id)
        )
        batch_counts = counts_by_process(
            db.query(TraineeBatch.process_id, func.count(TraineeBatch.id))
            .filter(TraineeBatch.is_active == True)
            .group_by(TraineeBatch.process_id)
        )

        for p in processes:
            trainee_count = trainee_counts.get(p.id, 0)
            trainer_count = trainer_counts.get(p.id, 0)
            batch_count   = batch_counts.get(p.id, 0)
            process_breakdown.append({"process": p.name, "trainees": trainee_count, "trainers": trainer_count, "batches": batch_count})
        process_breakdown.sort(key=lambda x: -(x["trainees"] + x["trainers"]))
    except Exception:
        pass

    # ── Batch health (avg progress per batch, DB-only) ───────────────────────
    batch_health = []
    try:
        batches = db.query(TraineeBatch).filter(TraineeBatch.is_active == True).all()

        # Pre-load all CourseProgress grouped by (user_id, course_id) — count distinct modules completed
        all_cp_rows = db.query(CourseProgress.user_id, CourseProgress.course_id, func.count(func.distinct(CourseProgress.completed_module_index)).label('cnt')).group_by(CourseProgress.user_id, CourseProgress.course_id).all()
        cp_lookup = {(r.user_id, r.course_id): r.cnt for r in all_cp_rows}

        # Pre-load max completed_module_index per course as fallback
        cp_max_rows = db.query(
            CourseProgress.course_id,
            func.max(CourseProgress.completed_module_index).label('max_idx')
        ).group_by(CourseProgress.course_id).all()
        course_max_mod_fallback = {r.course_id: r.max_idx for r in cp_max_rows}

        # Cache accurate S3-based module counts per course (computed on demand)
        course_total_mods_cache = {}

        def get_course_total_mods(cid):
            if cid not in course_total_mods_cache:
                count = get_total_modules_count(cid, db)
                if count == 0:
                    count = course_max_mod_fallback.get(cid, 0)
                course_total_mods_cache[cid] = count
            return course_total_mods_cache[cid]

        # Members, batch course ids, live course labels and user emails for every batch at once.
        # Per batch this was a members query, a distinct-course query, a live-course query and an
        # owner lookup, plus one email query per member - the member loop alone made this section
        # scale with the total number of batch memberships.
        batch_ids = [b.id for b in batches]
        members_by_batch = {}
        raw_course_ids_by_batch = {}
        if batch_ids:
            for member_row in db.query(BatchMember).filter(BatchMember.batch_id.in_(batch_ids)).all():
                members_by_batch.setdefault(member_row.batch_id, []).append(member_row)
            for batch_id, attempt_course_id in db.query(
                TraineeAttempt.batch_id, TraineeAttempt.course_id
            ).filter(TraineeAttempt.batch_id.in_(batch_ids)).distinct().all():
                raw_course_ids_by_batch.setdefault(batch_id, []).append(attempt_course_id)

        # Only include live courses (same as trainer dashboard)
        live_course_labels = {
            row.id: course_display_label(row)
            for row in db.query(Course.id, Course.course_title, Course.source_doc_name)
            .filter(Course.is_live == True)
            .all()
        }
        email_by_user_id = dict(db.query(User.id, User.email).all())

        for b in batches:
            members = members_by_batch.get(b.id, [])
            member_count = len(members)
            if member_count == 0:
                continue
            raw_course_ids = raw_course_ids_by_batch.get(b.id, [])
            batch_course_ids = [cid for cid in raw_course_ids if cid in live_course_labels]
            if not batch_course_ids:
                batch_health.append({"batch": b.batch_name, "members": member_count, "avg_progress": 0, "member_list": [], "courses": []})
                continue
            prog_vals = []
            member_progress = []
            for member in members:
                trainee_email = email_by_user_id.get(member.trainee_id) or member.trainee_id
                member_total_completed = 0
                member_total_possible = 0
                for cid in batch_course_ids:
                    completed = cp_lookup.get((member.trainee_id, cid), 0)
                    total_mods = get_course_total_mods(cid)
                    member_total_completed += completed
                    member_total_possible += total_mods
                # Weighted ratio: total completed / total possible (same as trainer dashboard)
                avg_member = round(member_total_completed / member_total_possible * 100, 1) if member_total_possible > 0 else 0
                prog_vals.append(avg_member)
                member_progress.append({"email": trainee_email, "progress": avg_member})

            # Course names for this batch
            batch_course_names = [live_course_labels[cid] for cid in batch_course_ids]

            avg_prog = round(sum(prog_vals) / len(prog_vals), 1) if prog_vals else 0
            # Get batch owner email
            owner_email = email_by_user_id.get(b.trainer_id) or "Unknown"
            batch_health.append({
                "batch": b.batch_name, "members": member_count, "avg_progress": avg_prog,
                "owner": owner_email,
                "member_list": member_progress, "courses": batch_course_names
            })
        batch_health.sort(key=lambda x: -x["avg_progress"])
        batch_health = batch_health[:8]
    except Exception as e:
        logger.warning('[STATS] batch_health error: %s', e)

    # ── Assessment pass rate per course (DB-only, no S3 scanning) ────────────
    course_pass_rates = []
    try:
        live_courses = db.query(Course).filter(Course.is_live == True).all()

        # Pre-load all TraineeAssessment records once (avoid N+1 queries)
        all_ta = db.query(TraineeAssessment).all()
        ta_by_course = defaultdict(list)
        for a in all_ta:
            ta_by_course[a.course_id].append(a)

        # Pre-load all CourseProgress counts per (user, course)
        all_cp = db.query(CourseProgress.user_id, CourseProgress.course_id, func.count(CourseProgress.id).label('cnt')).group_by(CourseProgress.user_id, CourseProgress.course_id).all()
        cp_map = {(r.user_id, r.course_id): r.cnt for r in all_cp}

        # Pre-load all AssessmentConfig
        all_ac = db.query(AssessmentConfig).all()
        ac_map = {a.course_id: a for a in all_ac}

        # Pre-load the enrolled trainees for every course, instead of one query per live course
        enrolled_by_course = defaultdict(set)
        for attempt_trainee_id, attempt_course_id in db.query(
            TraineeAttempt.trainee_id, TraineeAttempt.course_id
        ).all():
            enrolled_by_course[attempt_course_id].add(attempt_trainee_id)

        for c in live_courses:
            enrolled_trainee_ids = list(enrolled_by_course.get(c.id, set()))
            if not enrolled_trainee_ids:
                continue
            enrolled_count = len(enrolled_trainee_ids)

            assessment_config = ac_map.get(c.id)
            course_assessments = ta_by_course.get(c.id, [])

            # Group assessments by trainee
            by_trainee = defaultdict(list)
            for a in course_assessments:
                by_trainee[a.trainee_id].append(a)

            fully_completed = 0
            passed_count = 0
            failed_count = 0
            pending_count = 0
            scores = []

            for tid in enrolled_trainee_ids:
                mods_done = cp_map.get((tid, c.id), 0)
                trainee_assessments = by_trainee.get(tid, [])

                if not assessment_config:
                    # No assessment configured — "completed" means any module progress
                    if mods_done > 0:
                        fully_completed += 1
                        passed_count += 1
                    else:
                        pending_count += 1
                    continue

                if assessment_config.assessment_scope == 'end_of_course':
                    # Latest end-of-course assessment (module_index is None)
                    eoc = [a for a in trainee_assessments if a.module_index is None]
                    if eoc:
                        latest = max(eoc, key=lambda a: a.completed_at)
                        fully_completed += 1
                        scores.append(latest.percentage)
                        if latest.passed:
                            passed_count += 1
                        else:
                            failed_count += 1
                    else:
                        pending_count += 1

                elif assessment_config.assessment_scope == 'end_of_module':
                    eom = [a for a in trainee_assessments if a.module_index is not None]
                    # Latest per module index
                    module_latest = {}
                    for a in eom:
                        mi = a.module_index
                        if mi not in module_latest or a.completed_at > module_latest[mi].completed_at:
                            module_latest[mi] = a
                    if module_latest:
                        # Has attempted at least one module assessment
                        fully_completed += 1
                        avg_s = round(sum(a.percentage for a in module_latest.values()) / len(module_latest), 1)
                        scores.append(avg_s)
                        if all(a.passed for a in module_latest.values()):
                            passed_count += 1
                        else:
                            failed_count += 1
                    else:
                        pending_count += 1

            rate = round(passed_count / enrolled_count * 100, 1) if enrolled_count > 0 else 0
            avg_score = round(sum(scores) / len(scores), 1) if scores else 0
            title = course_display_label(c)
            course_pass_rates.append({
                "course": title,
                "pass_rate": rate,
                "avg_score": avg_score,
                "passed": passed_count,
                "failed": failed_count,
                "pending": pending_count,
                "fully_completed": fully_completed,
                "enrolled": enrolled_count,
                "has_assessment": assessment_config is not None,
            })
        # Merge duplicate-titled courses (same title, different course_id)
        merged = {}
        for r in course_pass_rates:
            t = r["course"]
            if t not in merged:
                merged[t] = r.copy()
            else:
                m = merged[t]
                m["enrolled"]        += r["enrolled"]
                m["fully_completed"] += r["fully_completed"]
                m["passed"]          += r["passed"]
                m["failed"]          += r["failed"]
                m["pending"]         += r["pending"]
                total = m["enrolled"]
                m["pass_rate"]  = round(m["passed"] / total * 100, 1) if total > 0 else 0
                # avg_score: weighted average
                s1 = m.get("avg_score", 0) * (m["fully_completed"] - r["fully_completed"])
                s2 = r.get("avg_score", 0) * r["fully_completed"]
                fc = m["fully_completed"]
                m["avg_score"] = round((s1 + s2) / fc, 1) if fc > 0 else 0
        course_pass_rates = sorted(merged.values(), key=lambda x: -x["enrolled"])[:12]
    except Exception as e:
        logger.warning('[STATS] course_pass_rates error: %s', e)

    # ── User registration trend with role breakdown (last 6 calendar months) ──
    user_reg_trend = []
    try:
        now = datetime.utcnow()
        months = []
        for i in range(5, -1, -1):
            m = now.month - i
            y = now.year
            while m <= 0:
                m += 12
                y -= 1
            months.append((y, m, datetime(y, m, 1).strftime("%b %Y")))

        six_months_ago = datetime(months[0][0], months[0][1], 1)
        signup_logs = db.query(ActivityLog).filter(
            ActivityLog.action == 'USER_SIGNUP',
            ActivityLog.timestamp >= six_months_ago
        ).all()

        # Build user_id -> role map for all users who signed up
        signup_user_ids = list({log.user_id for log in signup_logs})
        user_role_map = {}
        if signup_user_ids:
            users_in_logs = db.query(User.id, User.email, User.role, User.is_approved).filter(User.id.in_(signup_user_ids)).all()
            user_role_map = {u.id: {"role": u.role, "email": u.email, "is_approved": u.is_approved} for u in users_in_logs}

        # CRITICAL FIX: Also include ALL pending users not already covered by activity logs.
        # Find their registration date from the CREATE_USER admin log (action detail contains their ID).
        # For pending users with no USER_SIGNUP log, look for any activity log tied to their user_id.
        covered_user_ids = set(signup_user_ids)
        all_pending = db.query(User).filter(User.is_approved == False).all()
        uncovered_pending = [u for u in all_pending if u.id not in covered_user_ids]

        if uncovered_pending:
            uncovered_ids = [u.id for u in uncovered_pending]
            # Find earliest activity log for each uncovered pending user (any action)
            earliest_logs = db.query(
                ActivityLog.user_id,
                func.min(ActivityLog.timestamp).label('first_seen')
            ).filter(
                ActivityLog.user_id.in_(uncovered_ids)
            ).group_by(ActivityLog.user_id).all()
            earliest_map = {row.user_id: row.first_seen for row in earliest_logs}

            for pu in uncovered_pending:
                ts_raw = earliest_map.get(pu.id)
                if ts_raw and ts_raw >= six_months_ago:
                    ts = ts_raw.replace(tzinfo=None) if ts_raw.tzinfo else ts_raw
                    label = ts.strftime("%b %Y")
                    signup_user_ids.append(pu.id)
                    user_role_map[pu.id] = {"role": pu.role, "email": pu.email, "is_approved": False}
                    # Inject a synthetic entry directly into month_users
                    # (will be added below in the main loop via a synthetic log list)
                    # Store for later merge
                    if not hasattr(signup_logs, '_extra'):
                        pass
                    # We'll handle this in a separate merge step after the main loop

        # Group by month — distinct user_ids per month with role info
        # month_label -> {user_id -> {role, email, timestamp}}
        month_users = defaultdict(dict)
        for log in signup_logs:
            if log.timestamp and log.user_id:
                ts = log.timestamp.replace(tzinfo=None) if log.timestamp.tzinfo else log.timestamp
                label = ts.strftime("%b %Y")
                if log.user_id not in month_users[label]:  # first signup entry wins
                    info = user_role_map.get(log.user_id, {"role": "unknown", "email": log.user_id, "is_approved": True})
                    month_users[label][log.user_id] = {
                        "email": info["email"],
                        "role": info["role"],
                        "is_approved": info.get("is_approved", True),
                        "date": ts.strftime("%b %d, %Y")
                    }

        # Merge uncovered pending users using their earliest activity timestamp
        if uncovered_pending:
            for pu in uncovered_pending:
                ts_raw = earliest_map.get(pu.id)
                if ts_raw and ts_raw >= six_months_ago:
                    ts = ts_raw.replace(tzinfo=None) if ts_raw.tzinfo else ts_raw
                    label = ts.strftime("%b %Y")
                    if pu.id not in month_users[label]:
                        month_users[label][pu.id] = {
                            "email": pu.email,
                            "role": pu.role,
                            "is_approved": False,
                            "date": ts.strftime("%b %d, %Y")
                        }

        for _, _, label in months:
            users_this_month = list(month_users.get(label, {}).values())
            trainers  = [u for u in users_this_month if u["role"] == "trainer"]
            trainees  = [u for u in users_this_month if u["role"] == "trainee"]
            admins    = [u for u in users_this_month if u["role"] == "super_admin"]
            user_reg_trend.append({
                "month": label,
                "count": len(users_this_month),
                "trainers": len(trainers),
                "trainees": len(trainees),
                "admins": len(admins),
                "users": users_this_month  # full list for drill-down
            })
    except Exception as e:
        logger.warning('[STATS] user_reg_trend error: %s', e)
        now = datetime.utcnow()
        for i in range(5, -1, -1):
            m = now.month - i
            y = now.year
            while m <= 0:
                m += 12
                y -= 1
            user_reg_trend.append({"month": datetime(y, m, 1).strftime("%b %Y"), "count": 0,
                                   "trainers": 0, "trainees": 0, "admins": 0, "users": []})

    # ── Top courses by trainee enrollment ─────────────────────────────────────
    top_courses_by_enrollment = []
    top_courses_by_completion = []
    try:
        live_courses = db.query(Course).filter(Course.is_live == True).all()
        enrollment_by_title = defaultdict(set)   # title -> set of enrolled trainee_ids
        completion_by_title = defaultdict(set)   # title -> set of fully-completed trainee_ids

        # Pre-load data needed for full-completion check
        all_ta_comp = db.query(TraineeAssessment).all()
        ta_by_course_comp = defaultdict(list)
        for a in all_ta_comp:
            ta_by_course_comp[a.course_id].append(a)

        all_cp_comp = db.query(CourseProgress.user_id, CourseProgress.course_id,
                               func.count(func.distinct(CourseProgress.completed_module_index)).label('cnt')
                               ).group_by(CourseProgress.user_id, CourseProgress.course_id).all()
        cp_map_comp = {(r.user_id, r.course_id): r.cnt for r in all_cp_comp}

        # Max completed_module_index per course = total modules (proxy)
        course_max_mod = {}
        for r in db.query(CourseProgress.course_id,
                          func.max(CourseProgress.completed_module_index).label('mx')
                          ).group_by(CourseProgress.course_id).all():
            course_max_mod[r.course_id] = r.mx

        all_ac_comp = {a.course_id: a for a in db.query(AssessmentConfig).all()}

        # Enrolled trainees per course and every user's email up front: the two loops below used
        # to query both per course, and the drill-down lists made a second pass per title.
        enrolled_ids_by_course = defaultdict(set)
        for attempt_trainee_id, attempt_course_id in db.query(
            TraineeAttempt.trainee_id, TraineeAttempt.course_id
        ).distinct().all():
            enrolled_ids_by_course[attempt_course_id].add(attempt_trainee_id)
        emails_by_user_id_comp = dict(db.query(User.id, User.email).all())

        for c in live_courses:
            title = course_display_label(c)
            enrolled_ids = list(enrolled_ids_by_course.get(c.id, set()))
            enrollment_by_title[title].update(enrolled_ids)

            if not enrolled_ids:
                continue

            ac = all_ac_comp.get(c.id)
            course_assessments = ta_by_course_comp.get(c.id, [])
            by_trainee = defaultdict(list)
            for a in course_assessments:
                by_trainee[a.trainee_id].append(a)

            total_mods = course_max_mod.get(c.id, 0)  # highest module index completed in this course

            for tid in enrolled_ids:
                mods_done = cp_map_comp.get((tid, c.id), 0)
                trainee_assessments = by_trainee.get(tid, [])

                if not ac:
                    # No assessment — fully done if completed at least 1 module
                    # (we can't know total without S3, so any progress = done)
                    if mods_done > 0:
                        completion_by_title[title].add(tid)
                    continue

                if ac.assessment_scope == 'end_of_course':
                    eoc = [a for a in trainee_assessments if a.module_index is None]
                    if eoc and max(eoc, key=lambda a: a.completed_at).passed:
                        completion_by_title[title].add(tid)

                elif ac.assessment_scope == 'end_of_module':
                    eom = [a for a in trainee_assessments if a.module_index is not None]
                    module_latest = {}
                    for a in eom:
                        mi = a.module_index
                        if mi not in module_latest or a.completed_at > module_latest[mi].completed_at:
                            module_latest[mi] = a
                    # ALL modules must be attempted AND passed (not just the ones attempted)
                    all_passed = module_latest and all(a.passed for a in module_latest.values())
                    all_modules_covered = total_mods > 0 and len(module_latest) >= total_mods
                    if all_passed and all_modules_covered:
                        completion_by_title[title].add(tid)

        for title, ids in enrollment_by_title.items():
            if ids:
                # Fetch trainee emails for drill-down
                trainee_details = []
                try:
                    completed_ids = completion_by_title.get(title, set())
                    trainee_details = [
                        {"email": emails_by_user_id_comp[tid], "completed": tid in completed_ids}
                        for tid in ids
                        if tid in emails_by_user_id_comp
                    ]
                except Exception:
                    pass
                top_courses_by_enrollment.append({"course": title, "enrolled": len(ids), "trainees": trainee_details})

        # Build completion chart data with status breakdown per course
        # For each enrolled course, compute: fully_completed, modules_done_assessment_pending, in_progress
        for c in live_courses:
            title = course_display_label(c)
            enrolled_ids = list(enrollment_by_title.get(title, set()))
            if not enrolled_ids:
                continue

            ac = all_ac_comp.get(c.id)
            course_assessments = ta_by_course_comp.get(c.id, [])
            by_trainee = defaultdict(list)
            for a in course_assessments:
                by_trainee[a.trainee_id].append(a)
            total_mods = course_max_mod.get(c.id, 0)

            fully_done_ids = completion_by_title.get(title, set())
            modules_pending_ids = set()  # modules done, assessment pending
            in_progress_ids = set()
            not_started_ids = set()

            for tid in enrolled_ids:
                if tid in fully_done_ids:
                    continue
                mods_done = cp_map_comp.get((tid, c.id), 0)
                if mods_done == 0:
                    not_started_ids.add(tid)
                    continue
                modules_complete = mods_done > 0 and (total_mods == 0 or mods_done >= total_mods)
                if modules_complete and ac:
                    modules_pending_ids.add(tid)
                else:
                    in_progress_ids.add(tid)

            fully_done = len(fully_done_ids)
            modules_pending = len(modules_pending_ids)
            in_progress = len(in_progress_ids)
            not_started = len(not_started_ids)
            enrolled = len(enrolled_ids)

            if fully_done > 0 or modules_pending > 0 or in_progress > 0:
                trainee_details = []
                try:
                    trainee_details = [{
                        "email": emails_by_user_id_comp[tid],
                        "status": "completed" if tid in fully_done_ids
                                  else ("assessment_pending" if tid in modules_pending_ids
                                  else ("in_progress" if tid in in_progress_ids
                                  else "not_started"))
                    } for tid in enrolled_ids if tid in emails_by_user_id_comp]
                except Exception:
                    pass
                top_courses_by_completion.append({
                    "course": title,
                    "completed": fully_done,
                    "modules_pending": modules_pending,
                    "in_progress": in_progress,
                    "not_started": not_started,
                    "enrolled": enrolled,
                    "has_assessment": ac is not None,
                    "trainees": trainee_details
                })

        top_courses_by_enrollment.sort(key=lambda x: -x["enrolled"])
        top_courses_by_enrollment = top_courses_by_enrollment[:8]
        top_courses_by_completion.sort(key=lambda x: -x["completed"])
        top_courses_by_completion = top_courses_by_completion[:8]
    except Exception as e:
        logger.warning('[STATS] top_courses_by_completion error: %s', e)

    # ── Assessment pass rate per course ───────────────────────────────────────
    # (computed above using TraineeAssessment.passed — this block intentionally left empty)

    return {
        "total_users": total_users,
        "total_trainers": total_trainers,
        "total_trainees": total_trainees,
        "total_courses": total_courses,
        "total_modules": total_modules,
        "total_shares": total_shares,
        "pending_approvals": pending_approvals,
        "total_batches": total_batches,
        "total_processes": total_processes,
        "active_trainees_30d": active_trainees_30d,
        "trainees_fully_done": trainees_fully_done,
        "trainees_in_progress": trainees_in_progress,
        "trainees_not_started": trainees_not_started,
        "shares_by_type": shares_by_type,
        "shares_by_status": shares_by_status,
        "shares_by_process": shares_by_process,
        "top_shared_courses": top_shared_courses,
        "activity_by_day": activity_by_day,
        "user_growth_by_month": user_growth_by_month,
        "course_completion_rate": completion_rate,
        "total_assessments_passed": total_passed_count,
        "total_assessments_failed": total_failed_count,
        "top_trainers": top_trainers,
        "process_breakdown": process_breakdown,
        "batch_health": batch_health,
        "user_reg_trend": user_reg_trend,
        "top_courses_by_enrollment": top_courses_by_enrollment,
        "top_courses_by_completion": top_courses_by_completion,
        "course_pass_rates": course_pass_rates,
    }

@router.get("/super-admin/analytics/overview", tags=["Super Admin Analytics"])
def get_admin_analytics_overview(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """Get comprehensive system analytics for super admin."""
    total_users = db.query(User).count()
    total_trainers = db.query(User).filter(User.role == "trainer").count()
    total_trainees = db.query(User).filter(User.role == "trainee").count()
    pending_trainers = db.query(User).filter(User.role == "trainer", User.is_approved == False).count()
    
    total_processes = db.query(Process).count()
    total_batches = db.query(TraineeBatch).filter(TraineeBatch.is_active == True).count()
    total_courses = db.query(Course).filter(Course.is_live == True).count()
    
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    recent_activities = db.query(ActivityLog).filter(ActivityLog.timestamp >= thirty_days_ago).count()
    
    return {
        "success": True,
        "analytics": {
            "users": {
                "total": total_users,
                "trainers": total_trainers,
                "trainees": total_trainees,
                "pending_trainers": pending_trainers
            },
            "system": {
                "processes": total_processes,
                "batches": total_batches,
                "courses": total_courses
            },
            "activity": {
                "recent_activities_30d": recent_activities
            }
        }
    }

@router.get("/super-admin/analytics/batches", tags=["Super Admin Analytics"])
def get_all_batches_analytics(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """Get all batches created by all trainers with analytics."""
    batches = db.query(TraineeBatch, User).join(
        User, TraineeBatch.trainer_id == User.id
    ).filter(TraineeBatch.is_active == True).order_by(TraineeBatch.created_at.desc()).all()
    
    # Member counts for every batch in one grouped query instead of a count per batch.
    member_counts = {}
    if batches:
        member_counts = dict(
            db.query(BatchMember.batch_id, func.count(BatchMember.id))
            .filter(BatchMember.batch_id.in_([batch.id for batch, _trainer in batches]))
            .group_by(BatchMember.batch_id)
            .all()
        )

    batch_list = []
    for batch_data in batches:
        batch, trainer = batch_data
        member_count = member_counts.get(batch.id, 0)


        batch_list.append({
            "id": batch.id,
            "batch_name": batch.batch_name,
            "batch_description": batch.batch_description,
            "trainer": {"id": trainer.id, "email": trainer.email},
            "member_count": member_count,
            "created_at": batch.created_at.isoformat()
        })
    
    return {"success": True, "batches": batch_list}

@router.get("/super-admin/analytics/courses", tags=["Super Admin Analytics"])
def get_all_courses_analytics(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """Get all courses with detailed analytics."""
    courses = db.query(Course, User).join(
        User, Course.author_id == User.id
    ).filter(Course.is_live == True).order_by(Course.created_at.desc()).all()
    
    # Enrollments and completed-module counts for every course on the page, in one query each.
    # Both were previously read per course, so the tab's cost grew with the course catalogue.
    live_course_ids = [course.id for course, _trainer in courses]
    attempts_by_course = defaultdict(list)
    completed_by_course_trainee = {}
    if live_course_ids:
        for attempt in db.query(TraineeAttempt).filter(
            TraineeAttempt.course_id.in_(live_course_ids)
        ).all():
            attempts_by_course[attempt.course_id].append(attempt)
        completed_by_course_trainee = {
            (row[0], row[1]): row[2]
            for row in db.query(
                CourseProgress.course_id, CourseProgress.user_id, func.count(CourseProgress.id)
            ).filter(
                CourseProgress.course_id.in_(live_course_ids)
            ).group_by(CourseProgress.course_id, CourseProgress.user_id).all()
        }

    course_list = []
    for course_data in courses:
        course, trainer = course_data

        # Get enrollment stats from the rows fetched above.
        attempts = attempts_by_course.get(course.id, [])
        total_enrolled = len(attempts)

        # Get completion stats
        completed_count = 0
        in_progress_count = 0

        # Get total modules. This depends only on the course, so it is computed once here
        # rather than recomputed identically for every attempt.
        total_modules = _count_course_modules_loose(db, course)

        for attempt in attempts:
            completed_modules = completed_by_course_trainee.get((course.id, attempt.trainee_id), 0)

            if completed_modules >= total_modules and total_modules > 0:
                completed_count += 1
            elif completed_modules > 0:
                in_progress_count += 1


        course_list.append({
            "id": course.id,
            "source_doc_name": course.source_doc_name,
            "trainer": {"id": trainer.id, "email": trainer.email},
            "created_at": course.created_at.isoformat(),
            "enrollment_stats": {
                "total_enrolled": total_enrolled,
                "completed": completed_count,
                "in_progress": in_progress_count,
                "not_started": total_enrolled - completed_count - in_progress_count
            }
        })
    
    return {"success": True, "courses": course_list}

@router.get("/super-admin/analytics/trainers", tags=["Super Admin Analytics"])
def get_trainer_analytics_admin(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """Get detailed analytics for all trainers."""
    trainers = db.query(User).filter(User.role == "trainer", User.is_approved == True).all()

    # Every count this page shows, as one grouped query each. The five per-trainer counts below
    # meant a console with fifty trainers issued 250 queries before it could answer.
    trainer_ids = [trainer.id for trainer in trainers]
    process_ids = {trainer.process_id for trainer in trainers if trainer.process_id}
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    batch_counts = {}
    course_counts = {}
    trainee_counts = {}
    activity_counts = {}
    process_names = {}
    if trainer_ids:
        batch_counts = dict(
            db.query(TraineeBatch.trainer_id, func.count(TraineeBatch.id))
            .filter(TraineeBatch.trainer_id.in_(trainer_ids), TraineeBatch.is_active == True)
            .group_by(TraineeBatch.trainer_id).all()
        )
        course_counts = dict(
            db.query(Course.author_id, func.count(Course.id))
            .filter(Course.author_id.in_(trainer_ids), Course.is_live == True)
            .group_by(Course.author_id).all()
        )
        activity_counts = dict(
            db.query(ActivityLog.user_id, func.count(ActivityLog.id))
            .filter(ActivityLog.user_id.in_(trainer_ids), ActivityLog.timestamp >= thirty_days_ago)
            .group_by(ActivityLog.user_id).all()
        )
    if process_ids:
        trainee_counts = dict(
            db.query(User.process_id, func.count(User.id))
            .filter(User.role == "trainee", User.process_id.in_(process_ids))
            .group_by(User.process_id).all()
        )
        process_names = dict(
            db.query(Process.id, Process.name).filter(Process.id.in_(process_ids)).all()
        )

    trainer_analytics = []
    for trainer in trainers:
        batch_count = batch_counts.get(trainer.id, 0)
        course_count = course_counts.get(trainer.id, 0)
        trainee_count = trainee_counts.get(trainer.process_id, 0) if trainer.process_id else 0
        recent_activity = activity_counts.get(trainer.id, 0)
        process_name = process_names.get(trainer.process_id) if trainer.process_id else None

        trainer_analytics.append({
            "id": trainer.id,
            "email": trainer.email,
            "process_name": process_name,
            "stats": {
                "batches_created": batch_count,
                "courses_published": course_count,
                "trainees_in_process": trainee_count,
                "recent_activities_30d": recent_activity
            }
        })
    
    return {"success": True, "trainers": trainer_analytics}

def _course_completed_from_bulk_maps(
    trainee_id: str,
    course_id: str,
    total_modules: int,
    assessment_config,
    progress_map: dict,
    eoc_assessment_map: dict,
    eom_assessment_map: dict,
) -> bool:
    """Fast completion check using preloaded progress/assessment maps."""
    completed_modules = progress_map.get((trainee_id, course_id), 0)
    if completed_modules < total_modules or total_modules == 0:
        return False
    if not assessment_config:
        return True
    if assessment_config.assessment_scope == 'end_of_course':
        passed = eoc_assessment_map.get((trainee_id, course_id))
        return passed is not None
    if assessment_config.assessment_scope == 'end_of_module':
        mod_latest = eom_assessment_map.get((trainee_id, course_id), {})
        return len(mod_latest) == total_modules and all(a.passed for a in mod_latest.values())
    return False


@router.get("/super-admin/analytics/trainers-comprehensive", tags=["Super Admin Analytics"])
def get_comprehensive_trainer_analytics(
    process_id: Optional[str] = None,
    role: Optional[str] = None,
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """
    Get comprehensive trainer analytics with detailed statistics.
    Uses a single S3 scan and bulk SQL queries (no per-trainer S3 loops).
    """
    logger.info(
        "[COMPREHENSIVE TRAINER ANALYTICS] Loading for process: %s, role: %s",
        log_safe(process_id or "all"),
        log_safe(role or "all"),
    )

    query = db.query(User).filter(
        User.role.in_(["trainer", "super_admin"]),
        User.is_approved == True
    )
    if process_id:
        query = query.filter(User.process_id == process_id)
    if role:
        query = query.filter(User.role == role)

    trainers = query.all()
    trainer_ids = [t.id for t in trainers]
    logger.info('[COMPREHENSIVE TRAINER ANALYTICS] Found %s users', len(trainers))

    token_by_user = {}
    if trainer_ids:
        for trainer in trainers:
            analytics = get_user_token_analytics(db, trainer)
            token_by_user[trainer.id] = analytics

    processes = db.query(Process).all()
    process_map = {p.id: p.name for p in processes}

    courses_by_author = {tid: [] for tid in trainer_ids}
    published_by_author = {tid: [] for tid in trainer_ids}
    all_course_ids = set()
    if trainer_ids:
        for course in db.query(Course).filter(Course.author_id.in_(trainer_ids)).all():
            courses_by_author[course.author_id].append(course)
            all_course_ids.add(course.id)
            if course.is_live:
                published_by_author[course.author_id].append(course)

    module_counts = build_s3_module_counts_for_authors(db, set(trainer_ids))

    enrolled_by_author = {}
    if all_course_ids:
        for row in db.query(
            Course.author_id,
            func.count(func.distinct(TraineeAttempt.trainee_id)),
        ).join(TraineeAttempt, TraineeAttempt.course_id == Course.id).filter(
            Course.id.in_(list(all_course_ids))
        ).group_by(Course.author_id).all():
            enrolled_by_author[row[0]] = int(row[1])

    batch_by_trainer = {}
    if trainer_ids:
        for row in db.query(
            TraineeBatch.trainer_id,
            func.count(TraineeBatch.id),
        ).filter(
            TraineeBatch.trainer_id.in_(trainer_ids),
            TraineeBatch.is_active == True,
        ).group_by(TraineeBatch.trainer_id).all():
            batch_by_trainer[row[0]] = int(row[1])

    pending_by_author = {}
    if trainer_ids:
        for row in db.query(
            Course.author_id,
            func.count(AdditionalAttemptRequest.id),
        ).join(Course, Course.id == AdditionalAttemptRequest.course_id).filter(
            Course.author_id.in_(trainer_ids),
            AdditionalAttemptRequest.status == 'pending',
        ).group_by(Course.author_id).all():
            pending_by_author[row[0]] = int(row[1])

    published_course_ids = [
        c.id for courses in published_by_author.values() for c in courses
    ]
    progress_map = {}
    assessment_configs = {}
    eoc_assessment_map = {}
    eom_assessment_map = {}
    enrollments_by_course = {}

    if published_course_ids:
        for row in db.query(
            CourseProgress.user_id,
            CourseProgress.course_id,
            func.count(CourseProgress.id),
        ).filter(CourseProgress.course_id.in_(published_course_ids)).group_by(
            CourseProgress.user_id,
            CourseProgress.course_id,
        ).all():
            progress_map[(row[0], row[1])] = int(row[2])

        assessment_configs = {
            cfg.course_id: cfg
            for cfg in db.query(AssessmentConfig).filter(
                AssessmentConfig.course_id.in_(published_course_ids)
            ).all()
        }

        for assess in db.query(TraineeAssessment).filter(
            TraineeAssessment.course_id.in_(published_course_ids)
        ).all():
            if assess.module_index is None:
                key = (assess.trainee_id, assess.course_id)
                existing = eoc_assessment_map.get(key)
                if not existing or assess.completed_at > existing.completed_at:
                    if assess.passed:
                        eoc_assessment_map[key] = assess
            else:
                pair_key = (assess.trainee_id, assess.course_id)
                mod_latest = eom_assessment_map.setdefault(pair_key, {})
                prev = mod_latest.get(assess.module_index)
                if not prev or assess.completed_at > prev.completed_at:
                    mod_latest[assess.module_index] = assess

        for attempt in db.query(TraineeAttempt).filter(
            TraineeAttempt.course_id.in_(published_course_ids)
        ).all():
            enrollments_by_course.setdefault(attempt.course_id, []).append(attempt.trainee_id)

    trainer_analytics = []
    for trainer in trainers:
        try:
            all_courses = courses_by_author.get(trainer.id, [])
            published_courses = published_by_author.get(trainer.id, [])
            analytics = token_by_user.get(trainer.id, {})
            total_tokens = analytics.get("total_tokens", 0)
            total_cost_dollars = analytics.get("total_cost_dollars", 0.0)
            total_modules = sum(module_counts.get(c.id, 0) for c in all_courses)

            total_enrollments = 0
            completed_enrollments = 0
            for course in published_courses:
                course_modules = module_counts.get(course.id, 0)
                if course_modules == 0:
                    continue
                cfg = assessment_configs.get(course.id)
                for trainee_id in enrollments_by_course.get(course.id, []):
                    total_enrollments += 1
                    if _course_completed_from_bulk_maps(
                        trainee_id,
                        course.id,
                        course_modules,
                        cfg,
                        progress_map,
                        eoc_assessment_map,
                        eom_assessment_map,
                    ):
                        completed_enrollments += 1

            completion_rate = (
                (completed_enrollments / total_enrollments * 100)
                if total_enrollments > 0 else 0
            )

            trainer_analytics.append({
                "id": trainer.id,
                "email": trainer.email,
                "role": trainer.role,
                "process_id": trainer.process_id,
                "process_name": process_map.get(trainer.process_id, "N/A"),
                "stats": {
                    "total_courses": len(all_courses),
                    "published_courses": len(published_courses),
                    "unpublished_courses": len(all_courses) - len(published_courses),
                    "total_modules": total_modules,
                    "enrolled_trainees": enrolled_by_author.get(trainer.id, 0),
                    "active_batches": batch_by_trainer.get(trainer.id, 0),
                    "total_tokens_used": total_tokens,
                    "total_cost_dollars": total_cost_dollars,
                    "recent_activities_30d": 0,
                    "pending_attempt_requests": pending_by_author.get(trainer.id, 0),
                    "total_enrollments": total_enrollments,
                    "completed_enrollments": completed_enrollments,
                    "completion_rate": round(completion_rate, 1),
                },
            })
        except Exception as exc:
            logger.error(
                '[COMPREHENSIVE TRAINER ANALYTICS] Error processing trainer %s: %s',
                log_safe(trainer.email),
                log_safe(exc),
            )
            trainer_analytics.append({
                "id": trainer.id,
                "email": trainer.email,
                "role": trainer.role,
                "process_id": trainer.process_id,
                "process_name": process_map.get(trainer.process_id, "N/A"),
                "stats": {
                    "total_courses": 0,
                    "published_courses": 0,
                    "unpublished_courses": 0,
                    "total_modules": 0,
                    "enrolled_trainees": 0,
                    "active_batches": 0,
                    "total_tokens_used": 0,
                    "total_cost_dollars": 0.0,
                    "recent_activities_30d": 0,
                    "pending_attempt_requests": 0,
                    "total_enrollments": 0,
                    "completed_enrollments": 0,
                    "completion_rate": 0.0,
                },
            })

    logger.info('[COMPREHENSIVE TRAINER ANALYTICS] Returning %s trainer records', len(trainer_analytics))
    return {
        "success": True,
        "trainers": trainer_analytics,
        "processes": [{"id": p.id, "name": p.name} for p in processes],
        "total_trainers": len(trainer_analytics),
    }


@router.get("/super-admin/analytics/trainer-details/{trainer_id}", tags=["Super Admin Analytics"])
def get_trainer_details(
    trainer_id: str,
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """Get detailed trainer info including courses, enrollments, and synced token usage."""
    try:
        logger.info("[TRAINER DETAILS] Loading details for trainer: %s", log_safe(trainer_id))

        trainer = db.query(User).filter(
            User.id == trainer_id,
            User.role.in_(["trainer", "super_admin"]),
        ).first()
        if not trainer:
            return {"success": False, "error": "Trainer not found"}

        process = db.query(Process).filter(Process.id == trainer.process_id).first()
        process_name = process.name if process else "N/A"

        analytics = get_user_token_analytics(db, trainer)
        course_analytics = {
            item["course_id"]: item for item in analytics.get("courses", [])
        }

        courses = db.query(Course).filter(Course.author_id == trainer_id).all()
        course_ids = [c.id for c in courses]
        module_counts = build_s3_module_counts_for_authors(db, {trainer_id})

        published_course_ids = [c.id for c in courses if c.is_live]
        enrolled_trainees = 0
        if published_course_ids:
            enrolled_trainees = (
                db.query(TraineeAttempt.trainee_id)
                .filter(TraineeAttempt.course_id.in_(published_course_ids))
                .distinct()
                .count()
            )

        # Enrollment count per published course in one grouped query instead of one per course.
        attempt_counts_by_course = {}
        if published_course_ids:
            attempt_counts_by_course = dict(
                db.query(TraineeAttempt.course_id, func.count(TraineeAttempt.id))
                .filter(TraineeAttempt.course_id.in_(published_course_ids))
                .group_by(TraineeAttempt.course_id)
                .all()
            )

        courses_data = []
        for course in courses:
            trainee_count = attempt_counts_by_course.get(course.id, 0) if course.is_live else 0

            usage = course_analytics.get(course.id, {})
            courses_data.append({
                "id": course.id,
                "name": course.source_doc_name,
                "title": course.course_title,
                "is_live": course.is_live,
                "trainee_count": trainee_count,
                "tokens_used": usage.get("tokens_used", course.tokens_used or 0),
                "module_text_tokens": usage.get("module_text_tokens", 0),
                "module_vision_tokens": usage.get("module_vision_tokens", 0),
                "tutor_queries_tokens": usage.get("tutor_queries_tokens", 0),
                "chat_agent_tokens": usage.get("chat_agent_tokens", 0),
                "cost_cents": usage.get("cost_cents", course.generation_cost or 0),
                "created_at": course.created_at.isoformat() if course.created_at else None,
            })

        assessment_configs = {}
        if published_course_ids:
            assessment_configs = {
                cfg.course_id: cfg
                for cfg in db.query(AssessmentConfig).filter(
                    AssessmentConfig.course_id.in_(published_course_ids)
                ).all()
            }

        progress_map = {}
        if published_course_ids:
            for row in db.query(
                CourseProgress.user_id,
                CourseProgress.course_id,
                func.count(CourseProgress.id),
            ).filter(CourseProgress.course_id.in_(published_course_ids)).group_by(
                CourseProgress.user_id,
                CourseProgress.course_id,
            ).all():
                progress_map[(row[0], row[1])] = int(row[2])

        eoc_assessment_map = {}
        eom_assessment_map = {}
        # Latest end-of-course attempt per trainee/course, passed or not. The rows are read
        # newest-first so the first one seen for a pair is the one the per-attempt
        # `order_by(completed_at.desc()).first()` in the loop below used to fetch.
        latest_eoc_by_pair = {}
        if published_course_ids:
            for assess in db.query(TraineeAssessment).filter(
                TraineeAssessment.course_id.in_(published_course_ids)
            ).order_by(TraineeAssessment.completed_at.desc()).all():
                if assess.module_index is None:
                    key = (assess.trainee_id, assess.course_id)
                    if key not in latest_eoc_by_pair:
                        latest_eoc_by_pair[key] = assess
                    existing = eoc_assessment_map.get(key)
                    if not existing or assess.completed_at > existing.completed_at:
                        if assess.passed:
                            eoc_assessment_map[key] = assess
                else:
                    pair_key = (assess.trainee_id, assess.course_id)
                    mod_latest = eom_assessment_map.setdefault(pair_key, {})
                    prev = mod_latest.get(assess.module_index)
                    if not prev or assess.completed_at > prev.completed_at:
                        mod_latest[assess.module_index] = assess

        trainees_data = []
        if published_course_ids:
            attempts = (
                db.query(TraineeAttempt)
                .filter(TraineeAttempt.course_id.in_(published_course_ids))
                .all()
            )
            course_by_id = {c.id: c for c in courses}

            # Enrolled trainees and their last activity in one query each. Both used to be read
            # per enrollment row, so a trainer with many trainees paid two round-trips each.
            attempt_trainee_ids = {a.trainee_id for a in attempts if a.trainee_id}
            trainee_cache = {}
            last_activity_by_user = {}
            if attempt_trainee_ids:
                trainee_cache = {
                    u.id: u
                    for u in db.query(User).filter(User.id.in_(attempt_trainee_ids)).all()
                }
                last_activity_by_user = dict(
                    db.query(ActivityLog.user_id, func.max(ActivityLog.timestamp))
                    .filter(ActivityLog.user_id.in_(attempt_trainee_ids))
                    .group_by(ActivityLog.user_id)
                    .all()
                )

            for attempt in attempts:
                trainee = trainee_cache.get(attempt.trainee_id)
                if not trainee:
                    continue

                course = course_by_id.get(attempt.course_id)
                if not course:
                    continue

                assessment_config = assessment_configs.get(course.id)
                total_modules_for_course = module_counts.get(course.id, 0)
                completed_modules = progress_map.get(
                    (attempt.trainee_id, course.id), 0
                )
                progress_percentage = (
                    (completed_modules / total_modules_for_course * 100)
                    if total_modules_for_course > 0
                    else 0
                )

                assessment_status = "Not Started"
                assessment_score = None
                module_assessment_details = []

                if assessment_config:
                    if assessment_config.assessment_scope == "end_of_course":
                        latest_assessment = latest_eoc_by_pair.get(
                            (attempt.trainee_id, course.id)
                        )
                        if latest_assessment:
                            assessment_status = (
                                "Passed" if latest_assessment.passed else "Failed"
                            )
                            assessment_score = latest_assessment.percentage

                    elif assessment_config.assessment_scope == "end_of_module":
                        module_latest = eom_assessment_map.get(
                            (attempt.trainee_id, course.id), {}
                        )
                        if module_latest:
                            latest_assessments = list(module_latest.values())
                            effective_total = (
                                total_modules_for_course
                                if total_modules_for_course > 0
                                else len(module_latest)
                            )
                            if len(latest_assessments) < effective_total:
                                assessment_status = "In Progress"
                                total_score = sum(
                                    a.percentage for a in latest_assessments
                                )
                                assessment_score = (
                                    round(total_score / effective_total, 1)
                                    if effective_total > 0
                                    else 0
                                )
                                for assessment in latest_assessments:
                                    module_assessment_details.append({
                                        "module_index": assessment.module_index,
                                        "score": assessment.percentage,
                                        "passed": assessment.passed,
                                    })
                            else:
                                avg_score = sum(
                                    a.percentage for a in latest_assessments
                                ) / len(latest_assessments)
                                assessment_score = round(avg_score, 1)
                                assessment_status = (
                                    "Passed"
                                    if assessment_score
                                    >= assessment_config.pass_percentage
                                    else "Failed"
                                )

                last_activity_at = last_activity_by_user.get(attempt.trainee_id)

                attempts_display = f"{attempt.attempts_left} left"
                if assessment_config:
                    if assessment_config.assessment_scope == "end_of_module":
                        attempts_display = (
                            f"{assessment_config.max_attempts} per module"
                        )
                    else:
                        attempts_display = (
                            f"{assessment_config.max_attempts} for course"
                        )

                trainees_data.append({
                    "email": trainee.email,
                    "course_name": course.source_doc_name,
                    "course_title": course.course_title,
                    "progress_percentage": round(progress_percentage, 1),
                    "completed_modules": completed_modules,
                    "total_modules": total_modules_for_course,
                    "assessment_status": assessment_status,
                    "assessment_score": assessment_score,
                    "attempts_display": attempts_display,
                    "last_activity": (
                        last_activity_at.isoformat()
                        if last_activity_at
                        else None
                    ),
                    "module_assessment_details": (
                        module_assessment_details
                        if assessment_config
                        and assessment_config.assessment_scope == "end_of_module"
                        else []
                    ),
                })

        logger.info(
            "[TRAINER DETAILS] Returning details: %s courses, %s enrollments",
            len(courses_data),
            len(trainees_data),
        )

        return {
            "success": True,
            "trainer": {
                "id": trainer.id,
                "email": trainer.email,
                "process_name": process_name,
                "stats": {
                    "total_courses": len(courses),
                    "published_courses": len(published_course_ids),
                    "enrolled_trainees": enrolled_trainees,
                    "total_tokens_used": analytics["total_tokens"],
                    "total_cost_dollars": analytics["total_cost_dollars"],
                    "operations": analytics.get("operations", {}),
                },
            },
            "courses": courses_data,
            "trainees": trainees_data,
        }
    except Exception as exc:
        logger.error(
            "[TRAINER DETAILS] Failed for trainer %s: %s",
            log_safe(trainer_id),
            log_safe(exc),
        )
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)


@router.get("/super-admin/analytics/trainees-comprehensive", tags=["Super Admin Analytics"])
def get_comprehensive_trainee_analytics(
    process_id: Optional[str] = None,
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """
    Get comprehensive trainee analytics with detailed statistics.
    Uses a single S3 scan and bulk SQL queries (no per-trainee S3 loops).
    """
    logger.info(
        "[COMPREHENSIVE TRAINEE ANALYTICS] Loading for process: %s",
        log_safe(process_id or "all"),
    )

    query = db.query(User).filter(User.role == "trainee", User.is_approved == True)
    if process_id:
        query = query.filter(User.process_id == process_id)

    trainees = query.all()
    trainee_ids = [t.id for t in trainees]
    logger.info('[COMPREHENSIVE TRAINEE ANALYTICS] Found %s trainees', len(trainees))

    processes = db.query(Process).all()
    process_map = {p.id: p.name for p in processes}

    enrollments_by_trainee = {tid: [] for tid in trainee_ids}
    enrolled_course_ids = set()
    author_ids = set()
    if trainee_ids:
        enrollment_rows = (
            db.query(TraineeAttempt)
            .join(Course)
            .filter(
                TraineeAttempt.trainee_id.in_(trainee_ids),
                Course.is_live == True,
            )
            .all()
        )
        for enrollment in enrollment_rows:
            enrollments_by_trainee[enrollment.trainee_id].append(enrollment)
            enrolled_course_ids.add(enrollment.course_id)
            if enrollment.course:
                author_ids.add(enrollment.course.author_id)

    module_counts = build_s3_module_counts_for_authors(db, author_ids)

    progress_by_trainee_course = {}
    if trainee_ids and enrolled_course_ids:
        for row in db.query(
            CourseProgress.user_id,
            CourseProgress.course_id,
            func.count(CourseProgress.id),
        ).filter(
            CourseProgress.user_id.in_(trainee_ids),
            CourseProgress.course_id.in_(list(enrolled_course_ids)),
        ).group_by(CourseProgress.user_id, CourseProgress.course_id).all():
            progress_by_trainee_course[(row[0], row[1])] = int(row[2])

    assessment_configs = {}
    eoc_assessment_map = {}
    eom_assessment_map = {}
    latest_assessments_by_trainee = {tid: {} for tid in trainee_ids}
    if trainee_ids and enrolled_course_ids:
        assessment_configs = {
            cfg.course_id: cfg
            for cfg in db.query(AssessmentConfig).filter(
                AssessmentConfig.course_id.in_(list(enrolled_course_ids))
            ).all()
        }
        for assess in db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id.in_(trainee_ids),
            TraineeAssessment.course_id.in_(list(enrolled_course_ids)),
        ).all():
            if assess.module_index is None:
                key = (assess.trainee_id, assess.course_id)
                existing = eoc_assessment_map.get(key)
                if not existing or assess.completed_at > existing.completed_at:
                    if assess.passed:
                        eoc_assessment_map[key] = assess
            else:
                pair_key = (assess.trainee_id, assess.course_id)
                mod_latest = eom_assessment_map.setdefault(pair_key, {})
                prev = mod_latest.get(assess.module_index)
                if not prev or assess.completed_at > prev.completed_at:
                    mod_latest[assess.module_index] = assess

            assess_key = (assess.course_id, assess.module_index)
            trainee_map = latest_assessments_by_trainee[assess.trainee_id]
            prev_assess = trainee_map.get(assess_key)
            if not prev_assess or assess.completed_at > prev_assess.completed_at:
                trainee_map[assess_key] = assess

    last_activity_by_trainee = {}
    if trainee_ids:
        for row in db.query(
            ActivityLog.user_id,
            func.max(ActivityLog.timestamp),
        ).filter(ActivityLog.user_id.in_(trainee_ids)).group_by(ActivityLog.user_id).all():
            last_activity_by_trainee[row[0]] = row[1]

    trainee_analytics = []
    for trainee in trainees:
        try:
            enrollments = enrollments_by_trainee.get(trainee.id, [])
            total_courses = len(enrollments)
            total_modules_all = 0
            completed_modules_all = 0
            completed_courses = 0

            for enrollment in enrollments:
                course = enrollment.course
                if not course:
                    continue
                course_modules = module_counts.get(course.id, 0)
                total_modules_all += course_modules
                completed_modules_all += progress_by_trainee_course.get(
                    (trainee.id, course.id), 0
                )
                if course_modules > 0 and _course_completed_from_bulk_maps(
                    trainee.id,
                    course.id,
                    course_modules,
                    assessment_configs.get(course.id),
                    progress_by_trainee_course,
                    eoc_assessment_map,
                    eom_assessment_map,
                ):
                    completed_courses += 1

            overall_progress = (
                (completed_modules_all / total_modules_all * 100)
                if total_modules_all > 0 else 0
            )

            all_assessments = list(latest_assessments_by_trainee.get(trainee.id, {}).values())
            total_assessments = len(all_assessments)
            passed_assessments = len([a for a in all_assessments if a.passed])
            failed_assessments = total_assessments - passed_assessments
            avg_score = (
                sum(a.percentage for a in all_assessments) / total_assessments
                if all_assessments else 0
            )

            last_ts = last_activity_by_trainee.get(trainee.id)
            trainee_analytics.append({
                "id": trainee.id,
                "email": trainee.email,
                "process_id": trainee.process_id,
                "process_name": process_map.get(trainee.process_id, "N/A"),
                "stats": {
                    "total_courses": total_courses,
                    "completed_courses": completed_courses,
                    "in_progress_courses": total_courses - completed_courses,
                    "total_modules": total_modules_all,
                    "completed_modules": completed_modules_all,
                    "overall_progress": round(overall_progress, 1),
                    "total_assessments": total_assessments,
                    "passed_assessments": passed_assessments,
                    "failed_assessments": failed_assessments,
                    "average_score": round(avg_score, 1),
                    "last_activity": last_ts.isoformat() if last_ts else None,
                },
            })
        except Exception as e:
            logger.error('[COMPREHENSIVE TRAINEE ANALYTICS] Error processing trainee %s: %s', ("" if trainee.email is None else str(trainee.email).replace("\n", "").replace("\r", "")), e)
            trainee_analytics.append({
                "id": trainee.id,
                "email": trainee.email,
                "process_id": trainee.process_id,
                "process_name": process_map.get(trainee.process_id, "N/A"),
                "stats": {
                    "total_courses": 0,
                    "completed_courses": 0,
                    "in_progress_courses": 0,
                    "total_modules": 0,
                    "completed_modules": 0,
                    "overall_progress": 0.0,
                    "total_assessments": 0,
                    "passed_assessments": 0,
                    "failed_assessments": 0,
                    "average_score": 0.0,
                    "last_activity": None,
                },
            })

    logger.info('[COMPREHENSIVE TRAINEE ANALYTICS] Returning %s trainee records', len(trainee_analytics))
    return {
        "success": True,
        "trainees": trainee_analytics,
        "processes": [{"id": p.id, "name": p.name} for p in processes],
        "total_trainees": len(trainee_analytics),
    }


@router.get("/super-admin/analytics/trainees", tags=["Super Admin Analytics"])
def get_trainee_analytics_admin(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """Get detailed analytics for all trainees."""
    from sqlalchemy.orm import joinedload

    trainees = db.query(User).filter(User.role == "trainee").all()
    trainee_ids = [t.id for t in trainees]
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

    # Every lookup below used to be a query issued inside the per-trainee loop, and two of them
    # inside the per-attempt loop within it, so this tab cost roughly trainees x attempts round
    # trips and timed out on a real trainee list. Each is now one grouped query for the page.
    attempts_by_trainee = defaultdict(list)
    completed_modules_by_pair = {}
    assessments_by_pair = defaultdict(list)
    assessment_config_by_course = {}
    batch_memberships_by_trainee = {}
    recent_activity_by_trainee = {}

    if trainee_ids:
        # joinedload keeps attempt.course from lazy-loading one Course per attempt; the module
        # counter needs it.
        all_attempts = (
            db.query(TraineeAttempt)
            .options(joinedload(TraineeAttempt.course))
            .filter(TraineeAttempt.trainee_id.in_(trainee_ids))
            .all()
        )
        for attempt in all_attempts:
            attempts_by_trainee[attempt.trainee_id].append(attempt)

        completed_modules_by_pair = {
            (user_id, course_id): count
            for user_id, course_id, count in db.query(
                CourseProgress.user_id,
                CourseProgress.course_id,
                func.count(CourseProgress.id),
            )
            .filter(CourseProgress.user_id.in_(trainee_ids))
            .group_by(CourseProgress.user_id, CourseProgress.course_id)
            .all()
        }

        for assessment in (
            db.query(TraineeAssessment)
            .filter(TraineeAssessment.trainee_id.in_(trainee_ids))
            .all()
        ):
            assessments_by_pair[(assessment.trainee_id, assessment.course_id)].append(assessment)

        course_ids = {attempt.course_id for attempt in all_attempts if attempt.course_id}
        if course_ids:
            assessment_config_by_course = {
                config.course_id: config
                for config in db.query(AssessmentConfig)
                .filter(AssessmentConfig.course_id.in_(course_ids))
                .all()
            }

        batch_memberships_by_trainee = dict(
            db.query(BatchMember.trainee_id, func.count(BatchMember.id))
            .filter(BatchMember.trainee_id.in_(trainee_ids))
            .group_by(BatchMember.trainee_id)
            .all()
        )

        recent_activity_by_trainee = dict(
            db.query(ActivityLog.user_id, func.count(ActivityLog.id))
            .filter(
                ActivityLog.user_id.in_(trainee_ids),
                ActivityLog.timestamp >= thirty_days_ago,
            )
            .group_by(ActivityLog.user_id)
            .all()
        )

    process_name_by_id = {
        process.id: process.name for process in db.query(Process.id, Process.name).all()
    }

    trainee_analytics = []
    for trainee in trainees:
        attempts = attempts_by_trainee.get(trainee.id, [])

        total_courses = len(attempts)
        completed_courses = 0
        in_progress_courses = 0

        for attempt in attempts:
            completed_count = completed_modules_by_pair.get((trainee.id, attempt.course_id), 0)

            # Get total modules
            total_modules = _count_course_modules_loose(db, attempt.course) if attempt.course else 0

            if completed_count >= total_modules and total_modules > 0:
                # Also require assessment completion
                sa_assessment_config = assessment_config_by_course.get(attempt.course_id)
                sa_assessment_passed = False
                if sa_assessment_config:
                    course_assessments = assessments_by_pair.get((trainee.id, attempt.course_id), [])
                    if sa_assessment_config.assessment_scope == 'end_of_course':
                        sa_assessment_passed = any(
                            a.module_index is None and a.passed for a in course_assessments
                        )
                    elif sa_assessment_config.assessment_scope == 'end_of_module':
                        sa_mod_assessments = [
                            a for a in course_assessments if a.module_index is not None
                        ]
                        if sa_mod_assessments and total_modules > 0:
                            sa_mod_latest = {}
                            for a in sa_mod_assessments:
                                if a.module_index not in sa_mod_latest or a.completed_at > sa_mod_latest[a.module_index].completed_at:
                                    sa_mod_latest[a.module_index] = a
                            sa_assessment_passed = (len(sa_mod_latest) == total_modules and all(a.passed for a in sa_mod_latest.values()))
                else:
                    sa_assessment_passed = True  # No assessment configured
                if sa_assessment_passed:
                    completed_courses += 1
                else:
                    in_progress_courses += 1
            elif completed_count > 0:
                in_progress_courses += 1

        batch_memberships = batch_memberships_by_trainee.get(trainee.id, 0)
        recent_activity = recent_activity_by_trainee.get(trainee.id, 0)
        process_name = process_name_by_id.get(trainee.process_id) if trainee.process_id else None

        trainee_analytics.append({
            "id": trainee.id,
            "email": trainee.email,
            "process_name": process_name,
            "stats": {
                "total_courses": total_courses,
                "completed_courses": completed_courses,
                "in_progress_courses": in_progress_courses,
                "batch_memberships": batch_memberships,
                "recent_activities_30d": recent_activity
            }
        })
    
    return {"success": True, "trainees": trainee_analytics}

# ==============================================================================
# OPTIMIZATION: TOKEN ANALYTICS & SYSTEM SETTINGS
# ==============================================================================

@router.get("/trainer/token-analytics", tags=["Trainer Analytics"])
def get_trainer_token_analytics(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Get complete token usage analytics for trainer (shared sync with super-admin)."""
    try:
        analytics = get_user_token_analytics(db, user)
        return {"success": True, **analytics}
    except Exception as exc:
        logger.error(
            "[TRAINER TOKEN ANALYTICS] Failed for user %s: %s",
            log_safe(user.id),
            log_safe(exc),
        )
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/super-admin/token-analytics", tags=["Super Admin Analytics"])
def get_admin_token_analytics(process_id: Optional[str] = None, role: Optional[str] = None, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    Get system-wide token usage analytics with optional filters.
    Per-user totals use the same sync logic as the trainer dashboard.
    """
    try:
        all_users_query = db.query(User).filter(User.is_approved == True)
        if process_id and process_id != "":
            all_users_query = all_users_query.filter(User.process_id == process_id)
        if role and role != "":
            all_users_query = all_users_query.filter(User.role == role)

        all_users = all_users_query.all()
        user_stats = {
            user_obj.id: build_admin_user_stat(db, user_obj) for user_obj in all_users
        }

        system_total_tokens = sum(u["total_tokens"] for u in user_stats.values())
        system_total_cost_cents = sum(u["total_cost_cents"] for u in user_stats.values())

        trainers = [u for u in user_stats.values() if u["role"] == "trainer"]
        trainees = [u for u in user_stats.values() if u["role"] == "trainee"]
        super_admins = [u for u in user_stats.values() if u["role"] == "super_admin"]

        return {
            "success": True,
            "system_total_tokens": system_total_tokens,
            "system_total_cost_cents": system_total_cost_cents,
            "system_total_cost_dollars": round(system_total_cost_cents / 100, 2),
            "trainers": trainers,
            "trainees": trainees,
            "super_admins": super_admins,
            "total_trainers": len(trainers),
            "total_trainees": len(trainees),
            "total_super_admins": len(super_admins),
            "all_users": list(user_stats.values()),
        }
    except Exception as exc:
        logger.error(
            "[SUPER ADMIN TOKEN ANALYTICS] Failed: %s",
            log_safe(exc),
        )
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/super-admin/settings/global-sharing", tags=["Super Admin: System Management"])
def toggle_global_sharing(
    enabled: bool = Form(...),
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """Toggle whether trainers can share courses to all trainees in their process"""
    set_system_setting(db, "allow_global_sharing", "true" if enabled else "false")
    log_activity(
        db,
        user.id,
        "UPDATE_GLOBAL_SHARING",
        f"Global sharing {'enabled' if enabled else 'disabled'}",
    )
    logger.info('[ADMIN] Global sharing %s by %s', 'enabled' if enabled else 'disabled', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")))
    return {
        "success": True, 
        "global_sharing_enabled": enabled,
        "message": f"Global sharing has been {'enabled' if enabled else 'disabled'}. Trainers can now {'publish to all trainees or specific batches' if enabled else 'only publish to specific batches'}."
    }

@router.get("/super-admin/settings/global-sharing", tags=["Super Admin: System Management"])
def get_global_sharing_setting(
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """Get current global sharing setting"""
    enabled = get_system_setting(db, "allow_global_sharing", "false") == "true"
    return {
        "success": True, 
        "global_sharing_enabled": enabled
    }

@router.get("/trainer/settings/global-sharing", tags=["Trainer Dashboard & Courses"])
def get_trainer_global_sharing_setting(
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """Check if trainer can publish to all trainees (for frontend to show/hide option)"""
    enabled = get_system_setting(db, "allow_global_sharing", "false") == "true"
    return {
        "success": True, 
        "global_sharing_enabled": enabled,
        "can_publish_to_all": enabled
    }

@router.get("/super-admin/rate-limits", response_model=List[RateLimitModel], tags=["Super Admin: System Management"])
def admin_get_rate_limits(user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """[Super Admin] Retrieves the current rate limit configurations for all roles."""
    return db.query(RateLimitSetting).all()

@router.post("/super-admin/rate-limits", response_model=List[RateLimitModel], tags=["Super Admin: System Management"])
def admin_update_rate_limits(limits: List[RateLimitModel], user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """[Super Admin] Updates rate limit configurations for roles."""
    for limit_in in limits:
        if not validate_rate_limit_string(limit_in.limit):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid rate limit format for role '{limit_in.role}': "
                    f"'{limit_in.limit}'. Use format like '100/minute'."
                ),
            )
        setting = db.query(RateLimitSetting).filter(RateLimitSetting.role == limit_in.role).first()
        if setting:
            setting.limit = limit_in.limit.strip()
        else:
            db.add(RateLimitSetting(role=limit_in.role, limit=limit_in.limit.strip()))
    log_activity(db, user.id, "UPDATE_RATE_LIMITS", json.dumps([l.model_dump() for l in limits]))
    db.commit()
    invalidate_rate_limits_cache()
    return db.query(RateLimitSetting).all()

@router.get("/super-admin/users", tags=["Super Admin: User & Content Management"])
def admin_get_users(role: Optional[str] = None, process_id: Optional[str] = None, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Retrieves a list of all APPROVED users in the system, with optional filtering.
    Returns users list along with counts by role.

    - **role**: Filter users by role (e.g., 'trainer', 'trainee').
    - **process_id**: Filter users by their assigned process.
    """
    try:
        # CRITICAL FIX: Only show approved users in All Users tab
        query = db.query(User).filter(User.is_approved == True)
        if role and role != "": query = query.filter(User.role == role)
        if process_id and process_id != "": query = query.filter(User.process_id == process_id)
        
        users = _sort_users_by_signup_desc(query.all(), db)
        
        # Calculate counts by role based on filtered results
        trainer_count = sum(1 for u in users if u.role == "trainer")
        trainee_count = sum(1 for u in users if u.role == "trainee")
        super_admin_count = sum(1 for u in users if u.role == "super_admin")

        # Trainer activity: published vs not published
        trainer_ids = [u.id for u in users if u.role in ("trainer", "super_admin")]
        trainers_with_published = 0
        trainers_without_published = 0
        if trainer_ids:
            published_author_ids = {row[0] for row in db.query(Course.author_id).filter(
                Course.author_id.in_(trainer_ids),
                Course.is_live == True
            ).distinct().all()}
            trainers_with_published = len(published_author_ids)
            trainers_without_published = len(trainer_ids) - trainers_with_published

        # Trainee engagement: trainees who have started at least one course vs not started
        trainee_ids = [u.id for u in users if u.role == "trainee"]
        trainees_active = 0
        trainees_inactive = 0
        if trainee_ids:
            active_trainee_ids = {row[0] for row in db.query(CourseProgress.user_id).filter(
                CourseProgress.user_id.in_(trainee_ids)
            ).distinct().all()}
            trainees_active = len(active_trainee_ids)
            trainees_inactive = len(trainee_ids) - trainees_active

        return {
            "success": True,
            "users": [UserInDB.model_validate(u) for u in users],
            "counts": {
                "total": len(users),
                "trainers": trainer_count,
                "trainees": trainee_count,
                "super_admins": super_admin_count,
                "trainers_with_published": trainers_with_published,
                "trainers_without_published": trainers_without_published,
                "trainees_active": trainees_active,
                "trainees_inactive": trainees_inactive
            }
        }
    except Exception as exc:
        logger.error('[ADMIN USERS] Failed to load users: %s', log_safe(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/super-admin/users", response_model=UserInDB, tags=["Super Admin: User & Content Management"])
def admin_create_user(req: AdminUserCreate, current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Creates a new user (admin, trainer, or trainee) directly from the admin panel.
    - **email**: User's email address (must be unique)
    - **employee_id**: The account starts on the login St@<employee_id>#2026, and must set its
      own password at first sign-in (`must_change_password`). An explicit **password** is
      still accepted from API callers, under the same rule.
    - Either way the starting login passes the same strength policy as self-signup
      (`safe_password()`, VAPT: Weak Password Accepted).
    - **role**: User role (super_admin, trainer, or trainee)
    - **process_id**: Process/team assignment
    - **is_approved**: Whether to auto-approve the user (default: True)
    """
    try:
        # Case-insensitive duplicate check. `AdminUserCreate` already lower-cased
        # `req.email`, but an account created before that normalization existed may still
        # be stored mixed-case - an exact match would miss it and let an admin create a
        # second row for the same person.
        if get_user_from_db(db, email=req.email):
            raise HTTPException(status_code=400, detail="Email already registered")
        process = db.query(Process).filter(Process.id == req.process_id).first()
        if not process:
            raise HTTPException(status_code=400, detail="Invalid process ID")
        try:
            new_user = create_admin_account(
                db, current_user, email=req.email, role=req.role, process_id=req.process_id,
                is_approved=req.is_approved, password=req.password,
            )
        except AccountExistsError:
            raise HTTPException(status_code=400, detail="Email already registered")
        logger.info(
            'Admin %s created new %s user: %s',
            log_safe(current_user.email),
            log_safe(req.role),
            log_safe(req.email),
        )
        return UserInDB(
            id=str(new_user.id),
            email=str(new_user.email),
            role=str(new_user.role),
            is_approved=bool(new_user.is_approved),
            must_change_password=True,
            process=ProcessModel(id=str(process.id), name=str(process.name)),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error creating user: %s", log_safe(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create user")


@router.get("/super-admin/users/bulk-template", tags=["Super Admin: User & Content Management"])
def download_bulk_user_template(current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] The bulk-user Excel template, with Role and Process dropdowns built from the
    processes that exist now, plus an Example sheet and instructions.
    """
    names = [p.name for p in db.query(Process).order_by(Process.name).all() if p.name]
    return Response(
        content=build_template(names),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{TEMPLATE_FILENAME}"'},
    )


def _read_bulk_upload(file: UploadFile, db: Session) -> List[BulkRow]:
    """Read and validate an uploaded template, turning a bad file into a clear 400."""
    filename = (file.filename or "").lower()
    if not filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Upload the .xlsx template (other file types are not accepted).")
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="The file is too large. Keep it under 2 MB.")
    try:
        return check_upload(db, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/super-admin/users/bulk-validate", tags=["Super Admin: User & Content Management"])
def validate_bulk_users(
    file: UploadFile = File(...),
    current_user: User = Depends(super_admin_required),
    db: Session = Depends(get_db),
):
    """[Super Admin] Check every row of an uploaded template. Creates nothing."""
    return summarize(_read_bulk_upload(file, db))


@router.post("/super-admin/users/bulk-validate-rows", tags=["Super Admin: User & Content Management"])
def validate_bulk_user_rows(
    req: BulkUsersRowsRequest,
    current_user: User = Depends(super_admin_required),
    db: Session = Depends(get_db),
):
    """[Super Admin] Re-check rows the admin edited in the review table. Creates nothing."""
    return summarize(check_rows(db, rows_from_payload(req.rows)))


@router.post("/super-admin/users/bulk-create", status_code=status.HTTP_202_ACCEPTED, tags=["Super Admin: User & Content Management"])
def create_bulk_users(
    req: BulkUsersRowsRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(super_admin_required),
    db: Session = Depends(get_db),
):
    """
    [Super Admin] Create the reviewed rows as a background job.

    Takes the rows as the admin left them in the review table (edits included) and validates them
    again here - the browser's copy is never trusted. Rows with errors are skipped. Poll
    `GET /workflow/jobs/{job_id}/status` for progress and per-row results. Each account starts on
    St@<Employee ID>#2026 and must change it at first login.
    """
    rows = check_rows(db, rows_from_payload(req.rows))
    valid = [row for row in rows if not row.errors]
    if not valid:
        raise HTTPException(status_code=400, detail="No valid rows to create. Fix the rows marked with errors.")
    job_id = str(uuid.uuid4())
    create_job_status(job_id, current_user.id, db)
    background_tasks.add_task(
        run_bulk_creation,
        job_id,
        current_user.id,
        [{"row": r.row, "email": r.email, "employee_id": r.employee_id, "role": r.role, "process_id": r.process_id} for r in valid],
        req.is_approved,
    )
    return {"job_id": job_id, "queued": len(valid), "skipped": len(rows) - len(valid)}

@router.get("/super-admin/batches/view", tags=["Super Admin: User & Content Management"])
def view_all_batches(
    process_id: Optional[str] = None,
    role: Optional[str] = None,
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """
    [Super Admin] View all batches grouped by trainers with filters by process and role.
    Shows trainer statistics and batch details.
    
    - **process_id**: Optional filter by process ID
    - **role**: Optional filter by role (trainer or super_admin)
    """
    try:
        # Get all trainers, optionally filtered by process and role
        trainers_query = db.query(User).filter(User.is_approved == True)
        
        # Filter by role - default to trainer if not specified
        if role and role != "":
            trainers_query = trainers_query.filter(User.role == role)
        else:
            # Show both trainers and super_admins by default
            trainers_query = trainers_query.filter(User.role.in_(['trainer', 'super_admin']))
        
        if process_id and process_id != "":
            trainers_query = trainers_query.filter(User.process_id == process_id)
        
        trainers = trainers_query.order_by(User.email).all()
        
        logger.info(
            "[VIEW BATCHES] Found %s trainers (process_id: %s, role: %s)",
            len(trainers),
            str(process_id or "all").replace("\n", "").replace("\r", ""),
            str(role or "all").replace("\n", "").replace("\r", ""),
        )
        
        # Batches, member counts and processes for every trainer on the page, fetched once each.
        # These used to be a batch query, a member count per batch and a process lookup per
        # trainer, so a console with many trainers spent the whole request on round-trips.
        trainer_ids = [trainer.id for trainer in trainers]
        batches_by_trainer = defaultdict(list)
        member_counts = {}
        if trainer_ids:
            all_batches_query = db.query(TraineeBatch).filter(
                TraineeBatch.trainer_id.in_(trainer_ids),
                TraineeBatch.is_active == True
            )
            if process_id and process_id != "":
                all_batches_query = all_batches_query.filter(TraineeBatch.process_id == process_id)
            all_batches = all_batches_query.all()
            for batch in all_batches:
                batches_by_trainer[batch.trainer_id].append(batch)
            batch_ids = [batch.id for batch in all_batches]
            if batch_ids:
                member_counts = dict(
                    db.query(BatchMember.batch_id, func.count(BatchMember.id))
                    .filter(BatchMember.batch_id.in_(batch_ids))
                    .group_by(BatchMember.batch_id)
                    .all()
                )
        processes = db.query(Process).order_by(Process.name).all()
        process_by_id = {p.id: p for p in processes}

        # Build trainer statistics
        trainer_stats = []
        for trainer in trainers:
            batches = batches_by_trainer.get(trainer.id, [])

            # Calculate statistics
            total_batches = len(batches)
            total_trainees = sum(member_counts.get(batch.id, 0) for batch in batches)

            # Get process info
            process = process_by_id.get(trainer.process_id)

            # Include ALL trainers, even those without batches
            trainer_stats.append({
                "trainer_id": trainer.id,
                "trainer_email": trainer.email,
                "trainer_role": trainer.role,
                "process_name": process.name if process else "N/A",
                "process_id": trainer.process_id,
                "total_batches": total_batches,
                "total_trainees": total_trainees
            })
            
            logger.info('[VIEW BATCHES] Trainer %s (%s): %s batches, %s trainees', ("" if trainer.email is None else str(trainer.email).replace("\n", "").replace("\r", "")), ("" if trainer.role is None else str(trainer.role).replace("\n", "").replace("\r", "")), ("" if total_batches is None else str(total_batches).replace("\n", "").replace("\r", "")), ("" if total_trainees is None else str(total_trainees).replace("\n", "").replace("\r", "")))
        
        # Processes for the filter dropdown, from the same list used for the names above
        process_list = [{
            "id": p.id,
            "name": p.name
        } for p in processes]
        
        logger.info('[VIEW BATCHES] Returning %s trainer records', len(trainer_stats))
        
        return {
            "success": True,
            "trainers": trainer_stats,
            "processes": process_list,
            "total_trainers": len(trainer_stats)
        }
        
    except Exception as e:
        logger.error("Error fetching batches: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch batches")

@router.get("/super-admin/batches/trainer/{trainer_id}", tags=["Super Admin: User & Content Management"])
def get_trainer_batches_detail(
    trainer_id: str,
    process_id: Optional[str] = None,
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """
    [Super Admin] Get detailed batch information for a specific trainer.
    Shows all batches with trainee details.
    
    - **trainer_id**: Trainer user ID
    - **process_id**: Optional filter by process ID
    """
    try:
        # Get trainer info
        trainer = db.query(User).filter(User.id == trainer_id).first()
        if not trainer:
            raise HTTPException(status_code=404, detail="Trainer not found")
        
        # Get batches for this trainer
        batches_query = db.query(TraineeBatch).filter(
            TraineeBatch.trainer_id == trainer_id,
            TraineeBatch.is_active == True
        )
        if process_id and process_id != "":
            batches_query = batches_query.filter(TraineeBatch.process_id == process_id)
        
        batches = batches_query.order_by(TraineeBatch.created_at.desc()).all()

        # Processes, members, trainees and published courses for every batch at once. Each of
        # these was a query per batch (and a further query per member and per course), which is
        # what made this tab slow for a trainer running many batches.
        batch_ids = [batch.id for batch in batches]
        process_by_id = {}
        members_by_batch = defaultdict(list)
        course_ids_by_batch = defaultdict(set)
        trainees_by_id = {}
        courses_by_id = {}
        if batch_ids:
            process_ids = {batch.process_id for batch in batches if batch.process_id}
            if process_ids:
                process_by_id = {
                    p.id: p for p in db.query(Process).filter(Process.id.in_(process_ids)).all()
                }
            all_members = db.query(BatchMember).filter(BatchMember.batch_id.in_(batch_ids)).all()
            for member in all_members:
                members_by_batch[member.batch_id].append(member)
            trainee_ids = {member.trainee_id for member in all_members if member.trainee_id}
            if trainee_ids:
                trainees_by_id = {
                    u.id: u for u in db.query(User).filter(User.id.in_(trainee_ids)).all()
                }
            attempt_rows = db.query(TraineeAttempt.batch_id, TraineeAttempt.course_id).filter(
                TraineeAttempt.batch_id.in_(batch_ids)
            ).distinct().all()
            for row in attempt_rows:
                course_ids_by_batch[row[0]].add(row[1])
            published_course_ids = {row[1] for row in attempt_rows if row[1]}
            if published_course_ids:
                courses_by_id = {
                    c.id: c
                    for c in db.query(Course).filter(Course.id.in_(published_course_ids)).all()
                }

        # Build batch details
        batch_list = []
        for batch in batches:
            # Get process info
            process = process_by_id.get(batch.process_id)

            # Get batch members with trainee details
            trainee_list = []
            for member in members_by_batch.get(batch.id, []):
                trainee = trainees_by_id.get(member.trainee_id)
                if trainee:
                    trainee_list.append({
                        "id": trainee.id,
                        "email": trainee.email,
                        "added_at": member.added_at.isoformat() if member.added_at else None
                    })

            # Courses published directly to this batch
            batch_courses = []
            for course_id in course_ids_by_batch.get(batch.id, set()):
                course_obj = courses_by_id.get(course_id)
                if course_obj and course_obj.is_live:
                    batch_courses.append({"id": course_obj.id, "title": course_display_label(course_obj)})

            batch_list.append({
                "id": batch.id,
                "batch_name": batch.batch_name,
                "batch_description": batch.batch_description,
                "process": {
                    "id": process.id if process else None,
                    "name": process.name if process else "Unknown"
                },
                "trainee_count": len(trainee_list),
                "trainees": trainee_list,
                "batch_courses": batch_courses,
                "batch_voice_scenarios": [
                    {"id": s["id"], "title": s["title"], "category": s.get("category")}
                    for s in list_scenarios_published_to_batch(db, batch.id, batch.process_id)
                ],
                "created_at": batch.created_at.isoformat() if batch.created_at else None,
                "updated_at": batch.updated_at.isoformat() if batch.updated_at else None
            })

        # Build trainer-level manual_trainees — ALL trainees with manually-added courses
        # Query ALL manual attempts for this trainer's live courses
        trainer_course_ids = {row[0] for row in db.query(Course.id).filter(
            Course.author_id == trainer_id,
            Course.is_live == True
        ).all()}

        # Get all trainer's batch IDs to identify truly manual attempts
        trainer_batch_ids_set = {b["id"] for b in batch_list}

        manual_attempts_all = []
        if trainer_course_ids:
            all_course_attempts = db.query(TraineeAttempt).filter(
                TraineeAttempt.course_id.in_(trainer_course_ids)
            ).all()
            manual_attempts_all = [
                a for a in all_course_attempts
                if a.batch_id is None or a.batch_id not in trainer_batch_ids_set
            ]

        # Courses and trainees for the manual attempts, again in one query each instead of two
        # per attempt. Only the trainer's live courses can appear here, so the ids are known.
        manual_courses_by_id = {}
        manual_trainees_by_id = {}
        if manual_attempts_all:
            manual_course_ids = {a.course_id for a in manual_attempts_all if a.course_id}
            if manual_course_ids:
                manual_courses_by_id = {
                    c.id: c
                    for c in db.query(Course).filter(Course.id.in_(manual_course_ids)).all()
                }
            manual_trainee_ids = {a.trainee_id for a in manual_attempts_all if a.trainee_id}
            if manual_trainee_ids:
                manual_trainees_by_id = {
                    u.id: u for u in db.query(User).filter(User.id.in_(manual_trainee_ids)).all()
                }

        trainee_courses_map = {}
        for attempt in manual_attempts_all:
            course_obj = manual_courses_by_id.get(attempt.course_id)
            if not course_obj or not course_obj.is_live:
                continue
            trainee = manual_trainees_by_id.get(attempt.trainee_id)
            if not trainee:
                continue
            if trainee.email not in trainee_courses_map:
                trainee_courses_map[trainee.email] = []
            course_entry = {"id": course_obj.id, "title": course_display_label(course_obj)}
            if course_entry not in trainee_courses_map[trainee.email]:
                trainee_courses_map[trainee.email].append(course_entry)

        manual_trainees = [{"email": email, "courses": courses} for email, courses in trainee_courses_map.items()]

        return {
            "success": True,
            "trainer": {
                "id": trainer.id,
                "email": trainer.email
            },
            "batches": batch_list,
            "manual_trainees": manual_trainees,
            "total_batches": len(batch_list),
            "total_trainees": sum(b["trainee_count"] for b in batch_list)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching trainer batches: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch trainer batches")


@router.get("/super-admin/batches/{batch_id}/analytics", tags=["Super Admin: User & Content Management"])
def super_admin_get_batch_analytics(batch_id: str, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """[Super Admin] Get full analytics for any batch — reuses trainer analytics logic."""
    batch = db.query(TraineeBatch).filter(TraineeBatch.id == batch_id, TraineeBatch.is_active == True).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    trainer = db.query(User).filter(User.id == batch.trainer_id).first()
    if not trainer:
        raise HTTPException(status_code=404, detail="Batch trainer not found")
    from features.trainer.router import get_batch_analytics
    return get_batch_analytics(batch_id=batch_id, user=trainer, db=db)


@router.get("/super-admin/batches/{batch_id}/trainee/{trainee_id}/summary", tags=["Super Admin: User & Content Management"])
def super_admin_get_trainee_summary(batch_id: str, trainee_id: str, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """[Super Admin] Get AI coaching summary for a trainee in any batch."""
    batch = db.query(TraineeBatch).filter(TraineeBatch.id == batch_id, TraineeBatch.is_active == True).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    trainer = db.query(User).filter(User.id == batch.trainer_id).first()
    if not trainer:
        raise HTTPException(status_code=404, detail="Batch trainer not found")
    from features.trainer.router import get_trainee_batch_summary
    return get_trainee_batch_summary(batch_id=batch_id, trainee_id=trainee_id, user=trainer, db=db)


def _get_batch_analytics_data(batch_id: str, trainer: User, db: Session):
    """Internal helper — delegates to trainer batch analytics (includes voice training)."""
    from features.trainer.router import get_batch_analytics
    return get_batch_analytics(batch_id=batch_id, user=trainer, db=db)


def _get_trainee_summary_data(batch_id: str, trainee_id: str, trainer: User, db: Session):
    """Internal helper — delegates to trainer trainee summary (includes voice training)."""
    from features.trainer.router import get_trainee_batch_summary
    return get_trainee_batch_summary(batch_id=batch_id, trainee_id=trainee_id, user=trainer, db=db)
    batch_course_ids = [row[0] for row in db.query(TraineeAttempt.course_id).filter(TraineeAttempt.batch_id == batch_id).distinct().all()]


def _sort_users_by_signup_desc(users: List[User], db: Session) -> List[User]:
    """Order users with the most recently registered first (USER_SIGNUP activity log)."""
    if not users:
        return users
    user_ids = [u.id for u in users]
    signup_rows = (
        db.query(ActivityLog.user_id, func.min(ActivityLog.timestamp).label("signup_at"))
        .filter(ActivityLog.user_id.in_(user_ids), ActivityLog.action == "USER_SIGNUP")
        .group_by(ActivityLog.user_id)
        .all()
    )
    signup_at = {row.user_id: row.signup_at for row in signup_rows}

    def _signup_sort_key(user: User) -> float:
        ts = signup_at.get(user.id)
        if not ts:
            return 0.0
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts.timestamp()

    return sorted(users, key=_signup_sort_key, reverse=True)


@router.get("/super-admin/trainers/pending", response_model=List[UserInDB], tags=["Super Admin: User & Content Management"])
def get_pending_trainers(process_id: Optional[str] = None, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Retrieves all user accounts awaiting approval (any role).
    - **process_id**: Optional filter by process ID
    """
    query = db.query(User).filter(User.is_approved == False)
    if process_id and process_id != "":
        query = query.filter(User.process_id == process_id)
    return _sort_users_by_signup_desc(query.all(), db)

@router.post("/super-admin/trainers/bulk-approve", tags=["Super Admin: User & Content Management"])
def bulk_approve_trainers(user_ids: List[str] = Form(...), current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Bulk approve multiple pending user registrations (any role).
    """
    approved_count = 0
    approved_emails = []

    # Load every selected account and its existing signup log in two queries instead of two per
    # account: approving a long pending list otherwise cost 2N round-trips and could outlast the
    # request timeout. The loop still walks `user_ids`, so the response order is unchanged.
    pending_by_id = {}
    signup_logged_ids = set()
    if user_ids:
        pending_by_id = {
            row.id: row
            for row in db.query(User).filter(User.id.in_(user_ids), User.is_approved == False).all()
        }
        if pending_by_id:
            signup_logged_ids = {
                row[0]
                for row in db.query(ActivityLog.user_id).filter(
                    ActivityLog.user_id.in_(list(pending_by_id.keys())),
                    ActivityLog.action == 'USER_SIGNUP',
                ).all()
            }

    for user_id in user_ids:
        # `pop` mirrors the old `is_approved == False` re-query: once an id has been handled it is
        # no longer pending, so a repeated id in the form data is processed exactly once.
        trainer = pending_by_id.pop(user_id, None)
        if trainer:
            trainer.is_approved = True
            role_label = trainer.role.replace('_', ' ').title()
            db.add(Notification(user_id=trainer.id, message=f"Welcome! Your {role_label} account has been approved. You now have full access."))
            log_activity(db, current_user.id, "BULK_APPROVE_TRAINER", f"Bulk approved {trainer.role} '{trainer.email}' (ID: {user_id})")
            if trainer.id not in signup_logged_ids:
                log_activity(db, trainer.id, "USER_SIGNUP", f"Role: {trainer.role} (bulk approved by admin)")
                signup_logged_ids.add(trainer.id)
            approved_count += 1
            approved_emails.append(trainer.email)
    db.commit()
    return {"success": True, "message": f"Successfully approved {approved_count} user(s)", "approved_count": approved_count, "approved_emails": approved_emails}

@router.post("/super-admin/trainers/bulk-reject", tags=["Super Admin: User & Content Management"])
def bulk_reject_trainers(user_ids: List[str] = Form(...), current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Bulk reject multiple pending user registrations (any role).
    """
    rejected_count = 0
    rejected_emails = []

    # One query for the selected accounts, then one delete per dependent table for the whole
    # selection. Rejecting N accounts used to run 1 + 14 statements each, so a large selection
    # could exceed the request timeout; the statements below are the same filters widened with
    # `IN`, which deletes exactly the same rows.
    pending_by_id = {}
    if user_ids:
        pending_by_id = {
            row.id: row
            for row in db.query(User).filter(User.id.in_(user_ids), User.is_approved == False).all()
        }

    rejected_ids = []
    rejected_users = []
    for user_id in user_ids:
        # `pop` keeps a repeated id in the form data from being counted twice, which the old
        # `is_approved == False` re-query did implicitly.
        trainer = pending_by_id.pop(user_id, None)
        if trainer:
            rejected_emails.append(trainer.email)
            rejected_ids.append(user_id)
            rejected_users.append(trainer)
            # Logged before the ActivityLog delete below, exactly as before: this row belongs to
            # the acting super admin, who is never part of the rejected selection.
            log_activity(db, current_user.id, "BULK_REJECT_TRAINER", f"Bulk rejected {trainer.role} '{trainer.email}' (ID: {user_id})")
            rejected_count += 1

    if rejected_ids:
        db.query(Notification).filter(Notification.user_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(ActivityLog).filter(ActivityLog.user_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(UserSession).filter(UserSession.user_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(ChatSession).filter(ChatSession.user_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(TokenUsage).filter(TokenUsage.user_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(CourseProgress).filter(CourseProgress.user_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(TraineeAttempt).filter(TraineeAttempt.trainee_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(TraineeAssessment).filter(TraineeAssessment.trainee_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(JobStatus).filter(JobStatus.user_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(BatchMember).filter(BatchMember.trainee_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(TraineeBatch).filter(TraineeBatch.trainer_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(SharedReportEdit).filter(SharedReportEdit.editor_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(SharedReport).filter(SharedReport.sharer_id.in_(rejected_ids) | SharedReport.recipient_id.in_(rejected_ids)).delete(synchronize_session=False)
        db.query(AdditionalAttemptRequest).filter(AdditionalAttemptRequest.trainee_id.in_(rejected_ids) | AdditionalAttemptRequest.reviewed_by.in_(rejected_ids)).delete(synchronize_session=False)
        for trainer in rejected_users:
            db.delete(trainer)
    db.commit()
    return {"success": True, "message": f"Successfully rejected {rejected_count} user(s)", "rejected_count": rejected_count, "rejected_emails": rejected_emails}

@router.post("/super-admin/trainers/{user_id}/{action}", tags=["Super Admin: User & Content Management"])
def action_on_trainer(user_id: str, action: str, current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Approves or rejects a pending user registration (any role).

    - If approved, the user's `is_approved` flag is set to true.
    - If rejected, the user account is deleted from the system.
    - **user_id**: The UUID of the user to action.
    - **action**: Must be either `approve` or `reject`.
    - **Raises 404**: If the user is not found.
    """
    if action not in ['approve', 'reject']: raise HTTPException(status_code=400, detail="Invalid action")
    trainer = db.query(User).filter(User.id == user_id).first()
    if not trainer: raise HTTPException(status_code=404, detail="User not found")
    if action == 'approve':
        trainer.is_approved = True
        role_label = trainer.role.replace('_', ' ').title()
        msg = f"{role_label} account for {trainer.email} has been approved."
        db.add(Notification(user_id=trainer.id, message=f"Welcome! Your {role_label} account has been approved. You now have full access."))
        log_activity(db, current_user.id, "APPROVE_TRAINER", f"Approved {trainer.role} '{trainer.email}' (ID: {user_id})")
        # Only log USER_SIGNUP if one doesn't already exist (self-registered users already have one)
        existing_signup = db.query(ActivityLog).filter(ActivityLog.user_id == trainer.id, ActivityLog.action == 'USER_SIGNUP').first()
        if not existing_signup:
            log_activity(db, trainer.id, "USER_SIGNUP", f"Role: {trainer.role} (approved by admin)")
    else:
        # Delete all related records before deleting the user to avoid FK constraint violations
        trainer_email = trainer.email
        
        # Delete related records in order
        db.query(Notification).filter(Notification.user_id == user_id).delete(synchronize_session=False)
        db.query(ActivityLog).filter(ActivityLog.user_id == user_id).delete(synchronize_session=False)
        db.query(UserSession).filter(UserSession.user_id == user_id).delete(synchronize_session=False)
        db.query(ChatSession).filter(ChatSession.user_id == user_id).delete(synchronize_session=False)
        db.query(TokenUsage).filter(TokenUsage.user_id == user_id).delete(synchronize_session=False)
        db.query(CourseProgress).filter(CourseProgress.user_id == user_id).delete(synchronize_session=False)
        db.query(TraineeAttempt).filter(TraineeAttempt.trainee_id == user_id).delete(synchronize_session=False)
        db.query(TraineeAssessment).filter(TraineeAssessment.trainee_id == user_id).delete(synchronize_session=False)
        db.query(JobStatus).filter(JobStatus.user_id == user_id).delete(synchronize_session=False)
        db.query(BatchMember).filter(BatchMember.trainee_id == user_id).delete(synchronize_session=False)
        db.query(TraineeBatch).filter(TraineeBatch.trainer_id == user_id).delete(synchronize_session=False)
        db.query(SharedReportEdit).filter(SharedReportEdit.editor_id == user_id).delete(synchronize_session=False)
        db.query(SharedReport).filter((SharedReport.sharer_id == user_id) | (SharedReport.recipient_id == user_id)).delete(synchronize_session=False)
        db.query(AdditionalAttemptRequest).filter((AdditionalAttemptRequest.trainee_id == user_id) | (AdditionalAttemptRequest.reviewed_by == user_id)).delete(synchronize_session=False)
        
        # Now safe to delete the user
        db.delete(trainer)
        
        # Log activity after deletion (using current_user's ID, not the deleted trainer's ID)
        log_activity(db, current_user.id, "REJECT_TRAINER", f"Rejected and deleted {trainer.role} '{trainer_email}' (ID: {user_id})")
        msg = f"{trainer.role.replace('_', ' ').title()} account for {trainer_email} has been rejected and deleted."
    db.commit()
    return {"message": msg}

@router.get("/super-admin/users/{user_id}/stats", tags=["Super Admin: User & Content Management"])
def admin_get_user_stats(user_id: str, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Retrieves statistics for a specific user's knowledge base.

    Includes counts of source documents (courses), generated modules, indexed vector chunks, and review questions.
    - **user_id**: The UUID of the user whose stats to fetch.
    - **Raises 404**: If the user is not found.
    """
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # FIXED: Count courses instead of individual uploaded documents
    course_count = db.query(Course).filter(Course.author_id == user_id).count()
    
    # FIXED: Count only modules that belong to user's courses (not duplicates or orphaned modules)
    report_count = 0
    try:
        # Get all user's courses and their source documents
        courses = db.query(Course).filter(Course.author_id == user_id).all()
        course_ids = {course.id for course in courses}
        
        # Build a map of source_doc -> course_id for fallback matching
        source_doc_to_course = {}
        for course in courses:
            # Parse source_documents JSON array
            source_docs = []
            if course.source_documents:
                try:
                    source_docs = json.loads(course.source_documents)
                except:
                    source_docs = [course.source_doc_name]
            else:
                source_docs = [course.source_doc_name]
            
            for doc in source_docs:
                if doc not in source_doc_to_course:
                    source_doc_to_course[doc] = course.id
        
        # Count modules that have course_id matching user's courses. Read from the per-request
        # index rather than fetching each metadata object in turn: the super-admin user list
        # opens this per user, and M sequential GETs per user is what made the tab time out.
        for _s3_key, meta_data in author_report_metadata(db, user_id):
            module_course_id = meta_data.get('course_id')
            source_doc = meta_data.get('source_doc')

            # First try: Direct course_id match
            if module_course_id and module_course_id in course_ids:
                report_count += 1
            # Second try: Fallback to source_doc matching ONLY if source_doc exists in current courses
            elif not module_course_id and source_doc and source_doc in source_doc_to_course:
                report_count += 1
            # Skip modules without course_id that don't match any current course (orphaned)
    except BotoCoreError as e:
        logger.error('Error counting modules for user %s: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), e)
        pass
    
    try:
        stats = pinecone_index.describe_index_stats()
        chunk_count = stats.namespaces.get(user_id, {}).get('vector_count', 0)
    except Exception:
        chunk_count = 0
    
    # Get total review questions count for user's courses
    question_count = 0
    try:
        question_count = db.query(AssessmentQuestion).join(
            Course, AssessmentQuestion.course_id == Course.id
        ).filter(Course.author_id == user_id).count()
    except Exception as e:
        logger.error("Error counting questions for user %s: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), e)
        question_count = 0
    
    logger.info(
        "[ADMIN USER STATS] User %s: %s courses, %s modules, %s chunks, %s questions",
        ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
        course_count,
        report_count,
        chunk_count,
        question_count,
    )
    
    return {
        "total_documents": course_count,  # Now returns course count instead of file count
        "total_reports": report_count,  # Now uses same logic as /kb/stats
        "total_chunks": chunk_count,
        "total_questions": question_count
    }

@router.get("/super-admin/users/{user_id}/search", tags=["Super Admin: User & Content Management"])
def admin_search_user_kb(user_id: str, query: str, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    Super Admin: Search within a specific user's knowledge base.
    Returns relevant chunks from the user's documents using Pinecone.
    """
    try:
        # Verify the target user exists
        target_user = db.query(User).filter(User.id == user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        if not query:
            raise HTTPException(status_code=400, detail="Query parameter is required.")
        
        # Generate query vector using embedding model
        logger.info("Super Admin searching user %s KB for: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), ("" if query is None else str(query).replace("\n", "").replace("\r", "")))
        query_vector = embedding_model.embed_query(query)
        
        # Query Pinecone index in the target user's namespace
        logger.info("Querying Pinecone index in namespace: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
        search_response = pinecone_index.query(
            namespace=user_id,
            vector=query_vector,
            top_k=5,
            include_metadata=True
        )
        
        # Format results
        formatted_results = []
        if search_response.get('matches'):
            # Chunk metadata records the randomized STORAGE name (VAPT #4); the search card must
            # show the filename the trainer uploaded. Resolved once for the whole result set.
            display_map = author_document_display_map(db, user_id)
            for match in search_response['matches']:
                metadata = match.get('metadata', {})
                source_name = metadata.get("source", "")
                formatted_results.append({
                    "content": strip_bracket_image_tags(metadata.get("text", "")),
                    "source": display_name_from_map(display_map, source_name) or "Unknown",
                    "score": float(match.get('score', 0.0))
                })

        logger.info(
            "Found %s results for admin search in user %s KB.",
            len(formatted_results),
            ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
        )
        return formatted_results
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error searching user %s KB: %s", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/super-admin/users/{user_id}/reports", tags=["Super Admin: User & Content Management"])
def admin_get_user_reports(user_id: str, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Retrieves all reports for a specific user.

    - **user_id**: The UUID of the user whose reports to fetch.
    - **Raises 404**: If the user is not found.
    """
    if not db.query(User).filter(User.id == user_id).first(): raise HTTPException(status_code=404, detail="User not found")
    return {"grouped_reports": _get_grouped_reports(user_id, db)}

@router.get("/super-admin/users/{user_id}/reports-content/{doc_name:path}", tags=["Super Admin: User & Content Management"])
def admin_get_user_report_contents_for_doc(user_id: str, doc_name: str, user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Retrieves the content of all modules for a specific user's document.

    - **user_id**: The UUID of the user.
    - **doc_name**: The filename of the source document.
    - **Raises 404**: If the user or their reports are not found.
    """
    if not db.query(User).filter(User.id == user_id).first(): raise HTTPException(status_code=404, detail="User not found")
    if ".." in doc_name: raise HTTPException(status_code=400, detail="Invalid document name")
    target_group = next((g for g in _get_grouped_reports(user_id, db, include_content=True) if g['source_doc'] == doc_name), None)
    if not target_group: raise HTTPException(status_code=404, detail="Reports for this document not found.")
    return {
        "reports": target_group['reports'],
        # Never fall back to doc_name: that is the randomized storage name (VAPT #4).
        "course_title": target_group.get('course_title') or friendly_document_name(doc_name),
        "source_doc": doc_name,
        "course_id": target_group.get('course_id'),
        "is_published": target_group.get('is_published', False),
    }

@router.post("/super-admin/users/{user_id}/source-document/{doc_name:path}/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Super Admin: User & Content Management"])
def admin_delete_user_source_document(user_id: str, doc_name: str, current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Deletes a specific source document and all its associated data for a user.
    Uses the same full cleanup as trainer My Modules delete (multi-doc embeddings, DB metadata, S3).

    - **user_id**: The UUID of the user.
    - **doc_name**: The filename of the source document to delete.
    - **Raises 404**: If the user is not found.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    course = (
        db.query(Course)
        .filter(Course.author_id == user.id, Course.source_doc_name == doc_name)
        .first()
    )
    # Resolve both labels BEFORE the delete - the display-name mapping lives on the
    # Course row that is about to go away, and a storage key means nothing to a recipient.
    course_label = shared_source_display_label(db, user.id, doc_name, course)
    document_label = display_name_for_stored_document(db, user.id, doc_name) or course_label

    try:
        delete_source_document_completely(
            owner_user_id=user.id,
            doc_name=doc_name,
            db=db,
            shared_report_notify_message=(
                f"The document '{log_safe(document_label)}' shared by {log_safe(user.email)} "
                "has been deleted by an admin."
            ),
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document name")

    # No course_id: the row it would point at is already gone, and this notice is written
    # after delete_source_document_completely() swept the course's notifications, so it is
    # the one message about this course that is meant to survive the delete.
    db.add(
        Notification(
            user_id=user.id,
            message=(
                f"An admin ({log_safe(current_user.email)}) deleted your course "
                f"'{log_safe(course_label)}'."
            ),
        )
    )
    log_activity(
        db,
        current_user.id,
        "ADMIN_DELETE_DOCUMENT",
        f"Deleted document '{log_safe(doc_name)}' for user '{log_safe(user.email)}'",
    )
    logger.info(
        "ADMIN ACTION: %s deleted source document %s for user %s.",
        log_safe(current_user.email),
        log_safe(doc_name),
        log_safe(user.email),
    )
    return Response(status_code=204)

@router.get("/super-admin/users/{user_id}/view-pdf/{doc_name:path}", tags=["Super Admin: User & Content Management"])
def admin_view_user_pdf(user_id: str, doc_name: str, user: User = Depends(super_admin_required)):
    """
    [Super Admin] Serves a specific user's uploaded document for viewing.

    - **user_id**: The UUID of the user.
    - **doc_name**: The filename of the document to view.
    - **Raises 404**: If the document is not found.
    """
    if ".." in doc_name: raise HTTPException(status_code=400, detail="Invalid document name.")
    s3_key = f"{user_id}/uploaded_docs/{doc_name}"
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        # VAPT: Double Extension File Upload — the stored name is attacker-influenced,
        # so collapse it to a single safe extension in the header rather than echoing it.
        return Response(
            content=obj['Body'].read(),
            media_type=obj.get('ContentType') or "application/octet-stream",
            headers={"Content-Disposition": safe_content_disposition(doc_name, disposition="inline")},
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Document not found.")
        raise

@router.get("/super-admin/users/{user_id}/reports/{filename:path}", tags=["Super Admin: User & Content Management"])
def admin_get_user_report(
    user_id: str,
    filename: str,
    user: User = Depends(super_admin_required),
    db: Session = Depends(get_db),
):
    """
    [Super Admin] Retrieves the content of a single report belonging to a specific user,
    along with metadata including the source document name.

    - **Raises 404**: If the report is not found.
    """
    if ".." in filename: raise HTTPException(status_code=400, detail="Invalid filename")
    report_key = f"{user_id}/generated_reports/{filename}"
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

        # source_doc is the randomized storage name (VAPT #4) and stays the join key; the
        # display name is looked up against the OWNING user's courses, not the admin's.
        return {
            "content": content,
            "source_doc": source_doc,
            "source_doc_display_name": display_name_for_stored_document(
                db, user_id, source_doc
            ),
        }
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Report not found")
        raise

@router.post("/super-admin/users/{user_id}/reports/{filename:path}/title/update", tags=["Super Admin: User & Content Management"])
def admin_update_user_module_title(
    user_id: str,
    filename: str,
    req: ModuleTitleUpdateRequest,
    current_user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """
    [Super Admin] Updates the title of a module belonging to a specific user.

    - **Raises 404**: If the user or report is not found.
    """
    if ".." in filename: raise HTTPException(status_code=400, detail="Invalid filename")
    
    new_title = req.new_title.strip()
    if not new_title: raise HTTPException(status_code=400, detail="Title cannot be empty")
    
    user_to_notify = db.query(User).filter(User.id == user_id).first()
    if not user_to_notify: raise HTTPException(status_code=404, detail="User not found")
    
    report_key = f"{user_id}/generated_reports/{filename}"
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
        
        db.add(Notification(
            user_id=user_id,
            message=f"An admin ({current_user.email}) updated the module title to '{new_title}'.",
            course_id=course_notification_id_for_document(db, user_id, meta_data.get('source_doc')),
        ))
        log_activity(db, current_user.id, "ADMIN_UPDATE_MODULE_TITLE", f"Updated module title to '{new_title}' for user '{user_to_notify.email}'")
        db.commit()
        
        return {"success": True, "message": "Module title updated successfully"}
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Report metadata not found")
        raise
    except Exception as e:
        logger.error(
            "Error updating module title %s for user %s: %s",
            ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")),
            ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
            e,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="An internal server error occurred")

@router.post("/super-admin/users/{user_id}/reports/{filename:path}/update", tags=["Super Admin: User & Content Management"])
def admin_update_user_report(
    user_id: str,
    filename: str,
    req: ReportUpdateRequest,
    current_user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """
    [Super Admin] Updates the content of a specific user's report file.

    This action also re-indexes the document and notifies the user of the change.
    - **Raises 404**: If the user or report is not found.
    """
    if ".." in filename: raise HTTPException(status_code=400, detail="Invalid filename")
    user_to_notify = db.query(User).filter(User.id == user_id).first()
    if not user_to_notify: raise HTTPException(status_code=404, detail="User not found")
    report_key = f"{user_id}/generated_reports/{filename}"
    meta_key = report_key.replace('.md', '.json')
    try:
        s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=report_key, Body=req.new_content.encode('utf-8'), ServerSideEncryption='AES256')
        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=meta_key)
        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
        source_doc = meta_data.get('source_doc')
        if source_doc: _reindex_source_document(user_id, source_doc)
        # Name the document the owner knows, not its randomized storage key.
        source_display = display_name_for_stored_document(db, user_id, source_doc) if source_doc else 'a course'
        db.add(Notification(
            user_id=user_id,
            message=f"An admin ({current_user.email}) updated the module '{meta_data.get('title', 'a report')}' from '{source_display}'.",
            course_id=course_notification_id_for_document(db, user_id, source_doc),
        ))
        log_activity(db, current_user.id, "ADMIN_UPDATE_MODULE", f"Updated module '{filename}' for user '{user_to_notify.email}'")
        db.commit()
        return {"message": "Report updated, re-indexed, and user notified."}
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey': raise HTTPException(status_code=404, detail="Report not found")
        raise

@router.post("/super-admin/users/{user_id}/reports/{filename:path}/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Super Admin: User & Content Management"])
def admin_delete_user_report(
    user_id: str,
    filename: str,
    current_user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """
    [Super Admin] Deletes a single report file belonging to a specific user
    and notifies the course owner.

    - **Raises 404**: If the user or report is not found.
    """
    if ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    user_to_notify = db.query(User).filter(User.id == user_id).first()
    if not user_to_notify:
        raise HTTPException(status_code=404, detail="User not found")
    report_key = f"{user_id}/generated_reports/{filename}"
    meta_key = report_key.replace(".md", ".json")
    try:
        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=meta_key)
        meta_data = json.loads(meta_obj["Body"].read().decode("utf-8"))
        s3_client.delete_objects(
            Bucket=S3_BUCKET_NAME,
            Delete={"Objects": [{"Key": report_key}, {"Key": meta_key}]},
        )
        source_doc = meta_data.get("source_doc")
        if source_doc:
            _reindex_source_document(user_id, source_doc)

        module_title = log_safe(meta_data.get("title") or "a module")
        # The owner knows the document by its own name, not the randomized storage key.
        source_label = log_safe(display_name_for_stored_document(db, user_id, source_doc)) if source_doc else ""
        owner_message = (
            f"An admin ({log_safe(current_user.email)}) deleted your module "
            f"'{module_title}'"
            + (f" from '{source_label}'." if source_label else ".")
        )
        db.add(Notification(
            user_id=user_id,
            message=owner_message,
            course_id=course_notification_id_for_document(db, user_id, source_doc),
        ))
        log_activity(
            db,
            current_user.id,
            "ADMIN_DELETE_MODULE",
            f"Deleted module '{log_safe(filename)}' for user '{log_safe(user_to_notify.email)}'",
        )
        return Response(status_code=204)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            raise HTTPException(status_code=404, detail="Report not found")
        raise

def _purge_user_external_storage(user_id: str) -> None:
    """Remove Pinecone namespace and S3 prefixes after the user row is deleted."""
    from services.video_analysis_kb import _delete_s3_prefix

    try:
        pinecone_index.delete(delete_all=True, namespace=user_id)
    except NotFoundException:
        pass
    except Exception as exc:
        logger.warning(
            "Failed to delete Pinecone namespace for %s: %s",
            log_safe(user_id),
            log_safe(exc),
        )
        raise IOError("Failed to clear vector data.") from exc

    try:
        bucket = boto3.resource("s3").Bucket(S3_BUCKET_NAME)
        bucket.objects.filter(Prefix=f"{user_id}/").delete()
        _delete_s3_prefix(f"video-analysis/sessions/{user_id}/")
    except Exception as exc:
        logger.warning(
            "Failed to delete S3 objects for %s: %s",
            log_safe(user_id),
            log_safe(exc),
        )
        raise IOError("Failed to clear file storage data.") from exc


def _purge_video_analysis_history_row(
    db: Session,
    user_id: str,
    history: VideoAnalysisHistory,
) -> None:
    from services.video_analysis_kb import delete_video_analysis_report_assets

    try:
        delete_video_analysis_report_assets(user_id, history)
    except Exception as exc:
        logger.warning(
            "Failed to purge video analysis assets for %s: %s",
            log_safe(history.id),
            log_safe(exc),
        )
    for key_attr in ("pdf_s3_key", "docx_s3_key"):
        object_key = getattr(history, key_attr, None)
        if not object_key:
            continue
        try:
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=object_key)
        except Exception as exc:
            logger.warning(
                "Failed to delete video analysis export %s: %s",
                log_safe(object_key),
                log_safe(exc),
            )
    db.delete(history)


def _purge_user_database_records(db: Session, user_id: str) -> None:
    """Delete all database rows referencing a user before removing the user record."""
    for history in db.query(VideoAnalysisHistory).filter(VideoAnalysisHistory.user_id == user_id).all():
        _purge_video_analysis_history_row(db, user_id, history)

    course_ids = [
        row[0] for row in db.query(Course.id).filter(Course.author_id == user_id).all()
    ]

    if course_ids:
        shared_on_courses = [
            row[0]
            for row in db.query(SharedCourse.id).filter(SharedCourse.course_id.in_(course_ids)).all()
        ]
        if shared_on_courses:
            db.query(SharedCourseEdit).filter(
                SharedCourseEdit.shared_course_id.in_(shared_on_courses)
            ).delete(synchronize_session=False)
        db.query(SharedCourse).filter(SharedCourse.course_id.in_(course_ids)).delete(synchronize_session=False)
        db.query(TraineeQuestionSet).filter(TraineeQuestionSet.course_id.in_(course_ids)).delete(synchronize_session=False)
        db.query(TraineeAssessment).filter(TraineeAssessment.course_id.in_(course_ids)).delete(synchronize_session=False)
        db.query(AdditionalAttemptRequest).filter(
            AdditionalAttemptRequest.course_id.in_(course_ids)
        ).delete(synchronize_session=False)
        db.query(AssessmentQuestion).filter(AssessmentQuestion.course_id.in_(course_ids)).delete(
            synchronize_session=False
        )
        db.query(AssessmentConfig).filter(AssessmentConfig.course_id.in_(course_ids)).delete(
            synchronize_session=False
        )
        cache_entries = db.query(TutorResponseCache).filter(
            TutorResponseCache.course_id.in_(course_ids)
        ).all()
        for cache_entry in cache_entries:
            try:
                s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
            except Exception as cache_exc:
                logger.warning(
                    "Failed to delete tutor cache object %s: %s",
                    log_safe(cache_entry.response_s3_key),
                    log_safe(cache_exc),
                )
        db.query(TutorResponseCache).filter(TutorResponseCache.course_id.in_(course_ids)).delete(
            synchronize_session=False
        )
        db.query(TokenUsage).filter(TokenUsage.course_id.in_(course_ids)).delete(synchronize_session=False)
        db.query(TraineeAttempt).filter(TraineeAttempt.course_id.in_(course_ids)).delete(synchronize_session=False)
        db.query(CourseProgress).filter(CourseProgress.course_id.in_(course_ids)).delete(synchronize_session=False)

    trainer_batch_ids = [
        row[0] for row in db.query(TraineeBatch.id).filter(TraineeBatch.trainer_id == user_id).all()
    ]
    if trainer_batch_ids:
        db.query(BatchMember).filter(BatchMember.batch_id.in_(trainer_batch_ids)).delete(
            synchronize_session=False
        )
        db.query(TraineeAttempt).filter(TraineeAttempt.batch_id.in_(trainer_batch_ids)).delete(
            synchronize_session=False
        )

    shared_course_ids = [
        row[0]
        for row in db.query(SharedCourse.id).filter(
            (SharedCourse.sharer_id == user_id) | (SharedCourse.recipient_id == user_id)
        ).all()
    ]
    if shared_course_ids:
        db.query(SharedCourseEdit).filter(
            SharedCourseEdit.shared_course_id.in_(shared_course_ids)
        ).delete(synchronize_session=False)
    db.query(SharedCourseEdit).filter(SharedCourseEdit.editor_id == user_id).delete(synchronize_session=False)
    db.query(SharedCourse).filter(
        (SharedCourse.sharer_id == user_id) | (SharedCourse.recipient_id == user_id)
    ).delete(synchronize_session=False)

    shared_report_ids = [
        row[0]
        for row in db.query(SharedReport.id).filter(
            (SharedReport.sharer_id == user_id) | (SharedReport.recipient_id == user_id)
        ).all()
    ]
    if shared_report_ids:
        db.query(SharedReportEdit).filter(
            SharedReportEdit.shared_report_id.in_(shared_report_ids)
        ).delete(synchronize_session=False)
    db.query(SharedReportEdit).filter(SharedReportEdit.editor_id == user_id).delete(synchronize_session=False)
    db.query(SharedReport).filter(
        (SharedReport.sharer_id == user_id) | (SharedReport.recipient_id == user_id)
    ).delete(synchronize_session=False)

    chat_session_ids = [
        row[0] for row in db.query(ChatSession.id).filter(ChatSession.user_id == user_id).all()
    ]
    if chat_session_ids:
        db.query(ChatMessage).filter(ChatMessage.session_id.in_(chat_session_ids)).delete(
            synchronize_session=False
        )
    db.query(ChatSession).filter(ChatSession.user_id == user_id).delete(synchronize_session=False)

    db.query(Notification).filter(Notification.user_id == user_id).delete(synchronize_session=False)
    db.query(ActivityLog).filter(ActivityLog.user_id == user_id).delete(synchronize_session=False)
    db.query(UserSession).filter(UserSession.user_id == user_id).delete(synchronize_session=False)
    db.query(TokenUsage).filter(TokenUsage.user_id == user_id).delete(synchronize_session=False)
    db.query(CourseProgress).filter(CourseProgress.user_id == user_id).delete(synchronize_session=False)
    db.query(TraineeAttempt).filter(TraineeAttempt.trainee_id == user_id).delete(synchronize_session=False)
    db.query(TraineeAssessment).filter(TraineeAssessment.trainee_id == user_id).delete(synchronize_session=False)
    db.query(TraineeQuestionSet).filter(TraineeQuestionSet.trainee_id == user_id).delete(synchronize_session=False)
    db.query(JobStatus).filter(JobStatus.user_id == user_id).delete(synchronize_session=False)
    db.query(BatchMember).filter(BatchMember.trainee_id == user_id).delete(synchronize_session=False)
    db.query(TraineeBatch).filter(TraineeBatch.trainer_id == user_id).delete(synchronize_session=False)
    db.query(AdditionalAttemptRequest).filter(
        (AdditionalAttemptRequest.trainee_id == user_id)
        | (AdditionalAttemptRequest.reviewed_by == user_id)
    ).delete(synchronize_session=False)

    if course_ids:
        db.query(Course).filter(Course.id.in_(course_ids)).delete(synchronize_session=False)


@router.post("/super-admin/users/{user_id}/delete", status_code=status.HTTP_204_NO_CONTENT, tags=["Super Admin: User & Content Management"])
def admin_delete_user(user_id: str, current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Deletes a user and all of their associated data.

    Trainers and trainees are always eligible. Other super admin accounts may be
    deleted except your own account and the last remaining super admin.
    """
    user_to_delete = db.query(User).filter(User.id == user_id).first()
    if not user_to_delete:
        raise HTTPException(status_code=404, detail="User not found")
    if user_to_delete.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    if user_to_delete.role == "super_admin":
        remaining_super_admins = (
            db.query(User)
            .filter(User.role == "super_admin", User.id != user_id)
            .count()
        )
        if remaining_super_admins < 1:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete the last super admin account.",
            )
    logger.critical(
        "ADMIN ACTION: Admin %s is deleting user %s (%s).",
        log_safe(current_user.email),
        log_safe(user_to_delete.email),
        log_safe(user_id),
    )
    try:
        email_to_log = user_to_delete.email
        role_to_log = user_to_delete.role
        _purge_user_database_records(db, user_id)
        db.delete(user_to_delete)
        try:
            log_activity(
                db,
                current_user.id,
                "ADMIN_DELETE_USER",
                f"Deleted {role_to_log} '{email_to_log}' (ID: {user_id})",
            )
        except Exception as log_error:
            logger.warning("Failed to log deletion activity: %s", log_safe(log_error))
        db.commit()
        try:
            _purge_user_external_storage(user_id)
        except IOError as ext_error:
            logger.warning(
                "User %s deleted from database but external cleanup failed: %s",
                log_safe(user_id),
                log_safe(ext_error),
            )
        return Response(status_code=204)
    except IOError as exc:
        db.rollback()
        logger.error("Failed to delete user files: %s", log_safe(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)
    except Exception as exc:
        db.rollback()
        logger.error(
            "Failed to delete user %s: %s",
            log_safe(user_id),
            log_safe(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.post("/super-admin/users/{user_id}/share", tags=["Super Admin: User & Content Management"])
def admin_share_document(user_id: str, req: ShareRequest, current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Shares a document from one user to another.

    This bypasses the usual process-sharing restrictions.
    - **user_id**: The UUID of the user who owns the document.
    - **Raises 404**: If the owner or recipient user is not found.
    """
    try:
        owner = db.query(User).filter(User.id == user_id).first()
        recipient = db.query(User).filter(User.id == req.recipient_id).first()
        if not owner or not recipient: raise HTTPException(status_code=404, detail="Owner or recipient not found.")
        
        # Get course title for notification
        course = db.query(Course).filter(
            Course.source_doc_name == req.source_doc_name,
            Course.author_id == user_id
        ).first()
        course_display_name = shared_source_display_label(db, user_id, req.source_doc_name, course)
        
        # CRITICAL FIX: Pass bypass_process_check=True to allow cross-process sharing by admin
        result_message = _execute_share_reports(db=db, sharer_id=user_id, recipient_id=req.recipient_id, source_doc_name=req.source_doc_name, reason=req.reason, bypass_process_check=True)
        if "Successfully" in result_message:
            db.add(Notification(
                user_id=owner.id,
                message=f"An admin ({current_user.email}) shared your course '{course_display_name}' with {recipient.email}.",
                course_id=course_notification_id(course),
            ))
            log_activity(db, current_user.id, "ADMIN_SHARE_MODULES", f"Shared '{course_display_name}' from '{owner.email}' with '{recipient.email}'")
            db.commit()
            return {"message": result_message}
        else:
            raise HTTPException(status_code=400, detail=result_message)
    except Exception as e:
        logger.error("Error in admin share endpoint: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred.")

@router.post("/super-admin/users/{user_id}/share-course", tags=["Super Admin: User & Content Management"])
def admin_share_course(user_id: str, req: ShareRequest, current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Shares a complete course (modules + assessment) from one user to another.
    
    This allows super admin to share courses on behalf of trainers.
    - **user_id**: The UUID of the user who owns the course.
    - **Raises 404**: If the owner, recipient, or course is not found.
    """
    try:
        owner = db.query(User).filter(User.id == user_id).first()
        recipient = db.query(User).filter(User.id == req.recipient_id).first()
        if not owner or not recipient:
            raise HTTPException(status_code=404, detail="Owner or recipient not found.")
        
        # Find the course
        course = db.query(Course).filter(
            Course.source_doc_name == req.source_doc_name,
            Course.author_id == user_id
        ).first()
        
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Use the existing share_course logic but with the owner's context
        share_req = ShareCourseRequest(course_id=course.id, recipient_id=req.recipient_id, reason=req.reason)
        
        # Manually execute the sharing logic (can't call share_course directly as it requires trainer_required)
        # Check if already shared
        existing_share = db.query(SharedCourse).filter(
            SharedCourse.course_id == course.id,
            SharedCourse.recipient_id == req.recipient_id
        ).first()
        
        if existing_share:
            raise HTTPException(status_code=400, detail="Course already shared with this user")
        
        # Create share record
        shared_course = SharedCourse(
            course_id=course.id,
            sharer_id=user_id,
            recipient_id=req.recipient_id,
            reason=req.reason
        )
        db.add(shared_course)
        
        # Send notification to recipient
        reason_text = f" Reason: {req.reason}" if req.reason else ""
        course_display_name = course_display_label(course)
        notification = Notification(
            user_id=req.recipient_id,
            message=f"{owner.email} shared the course '{course_display_name}' with you (via admin).{reason_text}",
            related_link="/shared",
            course_id=course_notification_id(course)
        )
        db.add(notification)

        # Notify owner that admin shared their course
        owner_notification = Notification(
            user_id=owner.id,
            message=f"An admin ({current_user.email}) shared your course '{course_display_name}' with {recipient.email}.",
            course_id=course_notification_id(course)
        )
        db.add(owner_notification)
        
        # Log activity
        log_activity(db, current_user.id, "ADMIN_SHARE_COURSE", f"Shared course '{course_display_name}' from '{owner.email}' with '{recipient.email}'")
        
        db.commit()
        
        return {"message": f"Course '{course_display_name}' shared successfully with {recipient.email}"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in admin share course endpoint: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred while sharing the course")

# ==============================================================================
# TRAINER: TRAINEE PROGRESS ENDPOINTS
# ==============================================================================

@router.get("/trainer/trainee/{trainee_id}/courses", tags=["Trainer Dashboard & Courses"])
def trainer_get_trainee_courses(
    trainee_id: str, 
    current_user: User = Depends(trainer_required), 
    db: Session = Depends(get_db)
):
    """
    [Trainer] Get all PUBLISHED courses for a specific trainee with progress and assessment data.
    Trainer can only view trainees in their process.
    Shows only published courses that the trainee has attempts for AND that were authored by the logged-in trainer.
    """
    try:
        trainee = db.query(User).filter(User.id == trainee_id).first()
        if not trainee:
            raise HTTPException(status_code=404, detail="Trainee not found")
        
        # Verify trainee is in trainer's process
        if trainee.process_id != current_user.process_id:
            raise HTTPException(status_code=403, detail="You can only view trainees in your process")
        
        if trainee.role != 'trainee':
            raise HTTPException(status_code=400, detail="User is not a trainee")
        
        # CRITICAL FIX: Get only courses authored by THIS trainer that are published
        my_courses = db.query(Course).filter(
            Course.author_id == current_user.id,
            Course.is_live == True
        ).all()
        my_course_ids = {c.id for c in my_courses}
        
        logger.info('[TRAINER VIEW] Trainer %s has %s published courses', ("" if current_user.email is None else str(current_user.email).replace("\n", "").replace("\r", "")), len(my_course_ids))
        
        if not my_course_ids:
            logger.info('[TRAINER VIEW] No published courses found for trainer %s', ("" if current_user.email is None else str(current_user.email).replace("\n", "").replace("\r", "")))
            return {"success": True, "data": []}
        
        from sqlalchemy.orm import joinedload

        # Get only courses the trainee has attempts for that are ALSO authored by this trainer
        # joinedload keeps attempt.course from being fetched one row at a time below.
        attempts = db.query(TraineeAttempt).join(Course).options(
            joinedload(TraineeAttempt.course)
        ).filter(
            TraineeAttempt.trainee_id == trainee_id,
            TraineeAttempt.course_id.in_(my_course_ids),  # CRITICAL: Only trainer's courses
            Course.is_live == True
        ).all()

        # Prevent duplicate course cards/counts when multiple TraineeAttempt rows
        # exist for the same trainee+course (e.g., batch + manual assignment).
        unique_attempts_by_course = {}
        for att in attempts:
            if not att.course_id:
                continue
            existing = unique_attempts_by_course.get(att.course_id)
            if existing is None or (att.attempts_left or 0) > (existing.attempts_left or 0):
                unique_attempts_by_course[att.course_id] = att
        attempts = list(unique_attempts_by_course.values())

        logger.info(
            "[TRAINER VIEW] Found %s unique published course attempts for trainee %s in trainer's courses",
            len(attempts),
            ("" if trainee_id is None else str(trainee_id).replace("\n", "").replace("\r", "")),
        )
        
        # Progress counts, assessment configs and assessment attempts for every course at once.
        # These were a query each per course (and the assessment state was a further query per
        # module), which is what pushed this view past the request timeout for trainees with
        # several courses.
        viewed_course_ids = [att.course_id for att in attempts if att.course_id]
        completed_by_course = _count_progress_by_course(db, trainee_id, viewed_course_ids)
        config_by_course = _assessment_configs_by_course(db, viewed_course_ids)
        latest_assessments_by_course = build_trainee_latest_assessments(trainee_id, viewed_course_ids, db)
        course_attempts_left_by_course = end_of_course_attempts_left_by_course(
            db, trainee_id, viewed_course_ids, config_by_course
        )
        prefetch_s3_module_counts(
            db, {att.course.author_id for att in attempts if att.course and att.course.author_id}
        )

        courses_info = []
        for attempt in attempts:
            if not attempt.course:
                logger.warning('[TRAINER VIEW] Attempt %s has no course relationship', log_safe(attempt.id))
                continue

            course = attempt.course

            # Get progress
            completed_modules = completed_by_course.get(course.id, 0)

            # Get total modules
            total_modules = _count_modules_for_course(course.id, db)
            logger.debug(
                '[TRAINER VIEW] Course %s: completed=%s, total=%s',
                log_safe(course.id),
                completed_modules,
                total_modules,
            )

            # Get assessment status and score
            assessment_config = config_by_course.get(course.id)

            assessment_state = _get_trainee_course_assessment_state(
                trainee_id,
                course.id,
                assessment_config,
                total_modules,
                completed_modules,
                db,
                latest_assessments=latest_assessments_by_course,
            )
            has_assessment = assessment_state["has_assessment"]
            assessment_scope = assessment_state["assessment_scope"]
            assessment_status = assessment_state["assessment_status"]
            assessment_score = assessment_state["assessment_score"]
            module_assessment_details = assessment_state["module_assessment_details"]
            max_attempts_per_module = assessment_config.max_attempts if assessment_config else 3
            
            courses_info.append({
                "course_id": course.id,
                "source_doc_name": course.source_doc_name,
                "course_title": course_display_label(course),
                "completed_modules": completed_modules,
                "total_modules": total_modules,
                # End-of-course: computed the way the load and submit gates count, so the trainer
                # sees the same remaining attempts the trainee can actually use. Module scope keeps
                # the legacy column, whose real budget is per module (module_assessment_details).
                "attempts_left": course_attempts_left_by_course.get(course.id, attempt.attempts_left),
                "has_assessment": has_assessment,
                "assessment_status": assessment_status,
                "assessment_score": assessment_score,
                "assessment_scope": assessment_scope,
                "max_attempts_per_module": max_attempts_per_module,
                "module_assessment_details": module_assessment_details if assessment_scope == 'end_of_module' else []
            })
        
        logger.info(
            "[TRAINER VIEW] Returning %s courses for trainee %s",
            len(courses_info),
            ("" if trainee_id is None else str(trainee_id).replace("\n", "").replace("\r", "")),
        )
        return {"success": True, "data": courses_info}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting trainee courses for trainer: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get trainee courses")

@router.get("/trainer/trainee/{trainee_id}/course-progress/{course_id}", tags=["Trainer Dashboard & Courses"])
def trainer_get_trainee_course_progress(
    trainee_id: str, 
    course_id: str, 
    current_user: User = Depends(trainer_required), 
    db: Session = Depends(get_db)
):
    """
    [Trainer] Get detailed module-by-module progress for a trainee in a specific course.
    Trainer can only view trainees in their process.
    """
    try:
        trainee = db.query(User).filter(User.id == trainee_id).first()
        if not trainee:
            raise HTTPException(status_code=404, detail="Trainee not found")
        
        # Verify trainee is in trainer's process
        if trainee.process_id != current_user.process_id:
            raise HTTPException(status_code=403, detail="You can only view trainees in your process")
        
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        logger.info(
            "[TRAINER MODULE PROGRESS] Getting modules for course %s (trainee: %s)",
            ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
            ("" if trainee_id is None else str(trainee_id).replace("\n", "").replace("\r", "")),
        )
        
        # Get all modules for this course
        modules_data = _get_modules_for_course(course_id, db)
        logger.info('[TRAINER MODULE PROGRESS] _get_modules_for_course returned %s modules', len(modules_data))
        
        # Get completed modules
        completed_module_indices = set(
            progress.completed_module_index 
            for progress in db.query(CourseProgress).filter(
                CourseProgress.user_id == trainee_id,
                CourseProgress.course_id == course_id
            ).all()
        )
        logger.info('[TRAINER MODULE PROGRESS] Completed module indices: %s', ("" if completed_module_indices is None else str(completed_module_indices).replace("\n", "").replace("\r", "")))
        
        # If no modules found but there's progress, build modules from progress
        if not modules_data and completed_module_indices:
            logger.warning(
                "[TRAINER MODULE PROGRESS] No modules from _get_modules_for_course but found progress. Building from indices."
            )
            max_index = max(completed_module_indices)
            modules_data = [
                {'seg_index': i, 'title': f'Module {i}', 'filename': ''}
                for i in range(1, max_index + 1)
            ]
        
        # Check if this course has module-based assessments configured
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        has_module_assessments = assessment_config and assessment_config.assessment_scope == 'end_of_module'
        total_modules = len(modules_data)
        completed_modules = len(completed_module_indices)

        course_assessment = _build_course_assessment_progress(
            trainee_id,
            course_id,
            assessment_config,
            total_modules,
            completed_modules,
            db,
        )

        # One query for every module's attempts instead of two queries per module.
        module_assessments_by_index = _module_assessments_by_index(db, trainee_id, course_id)

        modules_progress = []
        for module in modules_data:
            idx = module['seg_index']
            study_done = idx in completed_module_indices

            # Check for module assessment
            module_assessment, module_attempts_used = module_assessments_by_index.get(idx, (None, 0))

            has_assessment = has_module_assessments
            assessment_score = None
            if module_assessment:
                assessment_score = module_assessment.percentage
                if assessment_score is None and module_assessment.total_possible:
                    assessment_score = round(
                        (module_assessment.score or 0) / module_assessment.total_possible * 100
                    )
            assessment_status = 'passed' if (module_assessment and module_assessment.passed) else ('failed' if module_assessment else 'not_attempted')
            
            # Module is only truly "completed" when study is done AND assessment passed (if required)
            is_completed = study_done and (not has_module_assessments or (module_assessment and module_assessment.passed))
            
            modules_progress.append({
                "module_index": idx,
                "title": module.get('title', f'Module {idx}'),
                "is_completed": is_completed,
                "study_completed": study_done,
                "has_assessment": has_assessment,
                "assessment_score": assessment_score,
                "assessment_status": assessment_status,
                "assessment_attempts_used": module_attempts_used,
            })
        
        logger.info('[TRAINER MODULE PROGRESS] Returning %s modules', len(modules_progress))
        
        return {
            "success": True,
            "data": {
                "course_name": course.source_doc_name,
                "assessment_scope": assessment_config.assessment_scope if assessment_config else None,
                "course_assessment": course_assessment,
                "modules": modules_progress
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting trainee course progress: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

def _build_module_assessment_results(
    assessment: "TraineeAssessment",
    trainee_id: str,
    course_id: str,
    module_index: int,
    db: "Session",
    question_results: Optional[List[dict]] = None,
) -> dict:
    """
    Shared helper: build the full assessment results dict for a trainee's module attempt.

    ``question_results`` lets a caller that has already built the rows for *this same attempt* hand
    them over instead of paying for the parse twice; it defaults to None, in which case they are
    built here as before.
    """
    detailed_questions = question_results if question_results is not None else (
        _build_trainee_assessment_question_results(
            trainee_id=trainee_id,
            course_id=course_id,
            module_index=module_index,
            assessment=assessment,
            db=db,
            hide_correct_answers=False,
        )
    )

    total_questions = len(detailed_questions)
    correct_answers = sum(1 for q in detailed_questions if q.get("is_correct") is True)
    pct = _assessment_display_percentage(assessment)

    return {
        "status":          "passed" if assessment.passed else "failed",
        "score":           pct,
        "percentage":      pct,
        "total_questions": total_questions,
        "correct_answers": correct_answers,
        "submitted_at":    assessment.completed_at.isoformat() if assessment.completed_at else None,
        "questions":       detailed_questions,
    }


def _build_module_assessment_results_with_history(
    trainee_id: str,
    course_id: str,
    module_index: int,
    db: "Session",
    *,
    include_all_attempts: bool = True,
    detail_attempt_number: Optional[int] = None,
) -> dict:
    """Build module assessment payload for review UIs.

    Question review is built for the latest attempt (or detail_attempt_number).
    All attempts are included as lightweight attempt_summaries for history tabs.
    """
    all_assessments = db.query(TraineeAssessment).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id == course_id,
        TraineeAssessment.module_index == module_index,
    ).all()

    if not all_assessments:
        raise HTTPException(status_code=404, detail="No module assessment found")

    has_passed_module = any(a.passed for a in all_assessments)
    history_payload = _build_module_assessment_attempt_history(
        all_assessments,
        trainee_id,
        course_id,
        module_index,
        db,
        has_passed_module,
        include_all_attempts=include_all_attempts,
        reveal_correct_answers=True,
        detail_attempt_number=detail_attempt_number,
        detail_latest_only=True,
    )
    display_attempts = _resolve_assessment_display_attempts(
        all_assessments, has_passed_module, include_all_attempts=include_all_attempts
    )
    assessment = _resolve_detail_attempt(display_attempts, detail_attempt_number)
    if assessment is None:
        assessment = _pick_latest_module_assessment(all_assessments)

    config = db.query(AssessmentConfig).filter(
        AssessmentConfig.course_id == course_id
    ).first()
    pass_percentage = config.pass_percentage if config else 70
    # Same ceiling the module load/submit gates enforce (configured cap + approved extras).
    # Subtracting only config.max_attempts ignored trainer grants and disagreed with the
    # trainee-facing results path that already uses _module_attempt_limits.
    if config is not None:
        module_limits = _module_attempt_limits(
            db, trainee_id, course_id, module_index, config
        )
        max_attempts = module_limits["effective_max_attempts"]
        attempts_used = module_limits["attempts_used"]
        attempts_remaining = module_limits["attempts_remaining"]
    else:
        max_attempts = 3
        attempts_used = len(all_assessments)
        attempts_remaining = max(0, max_attempts - attempts_used)

    detailed = (history_payload.get("attempt_history") or [None])[0]
    # The history builder already produced the question rows for this exact attempt under the same
    # hide_correct_answers rule, so hand them over: building them again doubled the work of the
    # slowest part of this endpoint for a payload that is overwritten immediately below.
    data = _build_module_assessment_results(
        assessment, trainee_id, course_id, module_index, db,
        question_results=detailed.get("questions") if detailed else None,
    )
    if detailed and detailed.get("questions") is not None:
        data["questions"] = detailed["questions"]
        data["score"] = detailed.get("score", data.get("score"))
        data["total_possible"] = detailed.get("total_possible", data.get("total_possible"))
        data["percentage"] = detailed.get("percentage", data.get("percentage"))
        data["passed"] = detailed.get("passed", data.get("passed"))
        data["time_taken"] = detailed.get("time_taken", data.get("time_taken"))
        data["submitted_at"] = detailed.get("submitted_at", data.get("submitted_at"))
    data.update({
        "passed": bool(assessment.passed),
        "has_passed": has_passed_module,
        "pass_percentage": pass_percentage,
        # Report the number the history builder gave this same attempt. Never fall back to the
        # stored attempt_number column alone — duplicates / NULLs there are what made one tab
        # show another attempt's marks for end-of-course review.
        "attempt_number": (
            history_payload.get("detailed_attempt_number")
            or _canonical_attempt_number(assessment, all_assessments)
        ),
        "attempts_used": attempts_used,
        "max_attempts": max_attempts,
        "attempts_remaining": attempts_remaining,
        "time_taken": assessment.time_taken or 0,
        "attempt_history": history_payload.get("attempt_history") or [],
        "attempt_summaries": history_payload.get("attempt_summaries") or [],
        "detailed_attempt_number": history_payload.get("detailed_attempt_number"),
    })
    return data


def _build_course_assessment_results_with_history(
    trainee_id: str,
    course_id: str,
    db: "Session",
    *,
    include_all_attempts: bool = True,
    detail_attempt_number: Optional[int] = None,
) -> dict:
    """Build end-of-course assessment payload for review UIs (latest detail by default)."""
    all_assessments = db.query(TraineeAssessment).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id == course_id,
        TraineeAssessment.module_index.is_(None),
    ).all()

    if not all_assessments:
        raise HTTPException(status_code=404, detail="No assessment found for this trainee")

    has_passed_course = any(a.passed for a in all_assessments)
    history_payload = _build_course_assessment_attempt_history(
        all_assessments,
        trainee_id,
        course_id,
        db,
        has_passed_course,
        include_all_attempts=include_all_attempts,
        reveal_correct_answers=True,
        detail_attempt_number=detail_attempt_number,
        detail_latest_only=True,
    )
    display_attempts = _resolve_assessment_display_attempts(
        all_assessments, has_passed_course, include_all_attempts=include_all_attempts
    )
    assessment = _resolve_detail_attempt(display_attempts, detail_attempt_number)
    if assessment is None:
        assessment = _pick_latest_module_assessment(all_assessments)

    detailed = (history_payload.get("attempt_history") or [None])[0]
    # Same attempt, same reveal rule as the history builder above — reuse its rows instead of
    # re-parsing the attempt only to overwrite the result a few lines down.
    data = _build_course_assessment_results_data(
        assessment, trainee_id, course_id, db, all_assessments=all_assessments,
        reveal_correct_answers=True,
        question_results=detailed.get("questions") if detailed else None,
    )
    if detailed and detailed.get("questions") is not None:
        data["questions"] = detailed["questions"]
        data["score"] = detailed.get("score", data.get("score"))
        data["total_possible"] = detailed.get("total_possible", data.get("total_possible"))
        data["percentage"] = detailed.get("percentage", data.get("percentage"))
        data["passed"] = detailed.get("passed", data.get("passed"))
        data["time_taken"] = detailed.get("time_taken", data.get("time_taken"))
        data["submitted_at"] = detailed.get("submitted_at", data.get("submitted_at"))
        data["attempt_number"] = detailed.get("attempt_number", data.get("attempt_number"))
    data["attempt_history"] = history_payload.get("attempt_history") or []
    data["attempt_summaries"] = history_payload.get("attempt_summaries") or []
    data["detailed_attempt_number"] = history_payload.get("detailed_attempt_number")
    return data


@router.get("/trainer/trainee/{trainee_id}/module-assessment-results/{course_id}/{module_index}", tags=["Trainer Dashboard & Courses"])
def trainer_get_trainee_module_assessment_results(
    trainee_id: str, 
    course_id: str, 
    module_index: int,
    attempt_number: Optional[int] = Query(None, ge=1),
    current_user: User = Depends(trainer_required), 
    db: Session = Depends(get_db)
):
    """
    [Trainer] Get detailed module assessment results for a trainee.
    Trainer can only view trainees in their process.
    Initial load details the latest attempt; pass attempt_number to lazy-load another.
    """
    try:
        trainee = db.query(User).filter(User.id == trainee_id).first()
        if not trainee:
            raise HTTPException(status_code=404, detail="Trainee not found")

        if trainee.process_id != current_user.process_id:
            raise HTTPException(status_code=403, detail="You can only view trainees in your process")

        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")

        assessment = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == trainee_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index == module_index
        ).order_by(TraineeAssessment.completed_at.desc()).first()

        if not assessment:
            raise HTTPException(status_code=404, detail="No module assessment found")

        data = _build_module_assessment_results_with_history(
            trainee_id, course_id, module_index, db,
            detail_attempt_number=attempt_number,
        )
        return {"success": True, "data": data}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting module assessment results: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/trainer/trainee/{trainee_id}/assessment-results/{course_id}", tags=["Trainer Dashboard & Courses"])
def trainer_get_trainee_assessment_results(
    trainee_id: str, 
    course_id: str, 
    attempt_number: Optional[int] = Query(None, ge=1),
    current_user: User = Depends(trainer_required), 
    db: Session = Depends(get_db)
):
    """
    [Trainer] Get detailed end-of-course assessment results for a trainee.
    Trainer can only view trainees in their process.
    Initial load details the latest attempt; pass attempt_number to lazy-load another.
    """
    try:
        trainee = db.query(User).filter(User.id == trainee_id).first()
        if not trainee:
            raise HTTPException(status_code=404, detail="Trainee not found")
        
        # Verify trainee is in trainer's process
        if trainee.process_id != current_user.process_id:
            raise HTTPException(status_code=403, detail="You can only view trainees in your process")
        
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Get latest end-of-course assessment (module_index is null)
        assessment = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == trainee_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index.is_(None),
        ).order_by(TraineeAssessment.completed_at.desc()).first()
        
        if not assessment:
            raise HTTPException(status_code=404, detail="No assessment found for this trainee")

        return {
            "success": True,
            "data": _build_course_assessment_results_with_history(
                trainee_id, course_id, db,
                detail_attempt_number=attempt_number,
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting assessment results: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

# ==============================================================================
# SUPER ADMIN: TRAINEE DATA ENDPOINTS
# ==============================================================================

@router.get("/super-admin/users/{user_id}/trainee-courses", tags=["Super Admin: User & Content Management"])
def admin_get_trainee_courses(user_id: str, current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Get all PUBLISHED courses assigned to a specific trainee with progress and assessment data.
    Shows only published courses that the trainee has attempts for.
    """
    try:
        trainee = db.query(User).filter(User.id == user_id).first()
        if not trainee:
            raise HTTPException(status_code=404, detail="Trainee not found")
        
        if trainee.role != 'trainee':
            raise HTTPException(status_code=400, detail="User is not a trainee")
        
        from sqlalchemy.orm import joinedload

        # Get all courses the trainee has attempts for that are also published
        # Using same pattern as trainee's own dashboard
        # joinedload keeps the course and its author from being fetched one row at a time below.
        attempts = db.query(TraineeAttempt).join(Course).options(
            joinedload(TraineeAttempt.course).joinedload(Course.author)
        ).filter(
            TraineeAttempt.trainee_id == user_id,
            Course.is_live == True
        ).all()

        # Prevent duplicate course cards/counts when multiple TraineeAttempt rows
        # exist for the same trainee+course (e.g., batch + manual assignment).
        unique_attempts_by_course = {}
        for att in attempts:
            if not att.course_id:
                continue
            existing = unique_attempts_by_course.get(att.course_id)
            if existing is None or (att.attempts_left or 0) > (existing.attempts_left or 0):
                unique_attempts_by_course[att.course_id] = att
        attempts = list(unique_attempts_by_course.values())

        logger.info('[ADMIN VIEW] Found %s unique published course attempts for trainee %s', len(attempts), log_safe(user_id))

        # Progress counts, assessment configs and assessment attempts for every course at once.
        # This endpoint returned 504 because each course cost a progress count, a config lookup
        # and one assessment query per module; it is now three queries for the whole page.
        viewed_course_ids = [att.course_id for att in attempts if att.course_id]
        completed_by_course = _count_progress_by_course(db, user_id, viewed_course_ids)
        config_by_course = _assessment_configs_by_course(db, viewed_course_ids)
        latest_assessments_by_course = build_trainee_latest_assessments(user_id, viewed_course_ids, db)
        course_attempts_left_by_course = end_of_course_attempts_left_by_course(
            db, user_id, viewed_course_ids, config_by_course
        )
        prefetch_s3_module_counts(
            db, {att.course.author_id for att in attempts if att.course and att.course.author_id}
        )

        courses_info = []
        for attempt in attempts:
            if not attempt.course:
                logger.warning('[ADMIN VIEW] Attempt %s has no course relationship', log_safe(attempt.id))
                continue

            course = attempt.course

            # Get progress
            completed_modules = completed_by_course.get(course.id, 0)

            # Get total modules
            total_modules = _count_modules_for_course(course.id, db)
            logger.debug(
                '[ADMIN VIEW] Course %s: total_modules=%s, completed_modules=%s',
                log_safe(course.id),
                total_modules,
                completed_modules,
            )

            # Get assessment status and score
            assessment_config = config_by_course.get(course.id)

            assessment_state = _get_trainee_course_assessment_state(
                user_id,
                course.id,
                assessment_config,
                total_modules,
                completed_modules,
                db,
                latest_assessments=latest_assessments_by_course,
            )
            has_assessment = assessment_state["has_assessment"]
            assessment_scope = assessment_state["assessment_scope"]
            assessment_status = assessment_state["assessment_status"]
            assessment_score = assessment_state["assessment_score"]
            module_assessment_details = assessment_state["module_assessment_details"]
            max_attempts_per_module = assessment_config.max_attempts if assessment_config else 3

            
            courses_info.append({
                "course_id": course.id,
                "source_doc_name": course.source_doc_name,
                "course_title": course_display_label(course),
                "author_email": course.author.email if course.author else "Unknown",  # ADDED: Trainer/Owner email
                "author_id": course.author_id,  # ADDED: Trainer/Owner ID
                "completed_modules": completed_modules,
                "total_modules": total_modules,
                # End-of-course: computed the way the load and submit gates count, so the trainer
                # sees the same remaining attempts the trainee can actually use. Module scope keeps
                # the legacy column, whose real budget is per module (module_assessment_details).
                "attempts_left": course_attempts_left_by_course.get(course.id, attempt.attempts_left),
                "has_assessment": has_assessment,
                "assessment_status": assessment_status,
                "assessment_score": assessment_score,
                "assessment_scope": assessment_scope,
                "max_attempts_per_module": max_attempts_per_module,
                "module_assessment_details": module_assessment_details if assessment_scope == 'end_of_module' else []
            })
        
        logger.info('[ADMIN VIEW] Returning %s courses for trainee %s', len(courses_info), log_safe(user_id))
        return {"success": True, "data": courses_info}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting trainee courses: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get trainee courses")

@router.get("/super-admin/users/{user_id}/trainee-stats", tags=["Super Admin: User & Content Management"])
def admin_get_trainee_stats(user_id: str, current_user: User = Depends(super_admin_required), db: Session = Depends(get_db)):
    """
    [Super Admin] Get overall statistics for a specific trainee.
    Shows stats for PUBLISHED courses the trainee has attempts for.
    """
    try:
        trainee = db.query(User).filter(User.id == user_id).first()
        if not trainee:
            raise HTTPException(status_code=404, detail="Trainee not found")
        
        if trainee.role != 'trainee':
            raise HTTPException(status_code=400, detail="User is not a trainee")
        
        from sqlalchemy.orm import joinedload

        # Get all published courses the trainee has attempts for
        # Using same pattern as trainee's own dashboard
        # joinedload keeps attempt.course from being fetched one row at a time below.
        attempts = db.query(TraineeAttempt).join(Course).options(
            joinedload(TraineeAttempt.course)
        ).filter(
            TraineeAttempt.trainee_id == user_id,
            Course.is_live == True
        ).all()

        # Ensure stats are computed per unique course, not per raw attempt row.
        unique_attempts_by_course = {}
        for att in attempts:
            if not att.course_id:
                continue
            existing = unique_attempts_by_course.get(att.course_id)
            if existing is None or (att.attempts_left or 0) > (existing.attempts_left or 0):
                unique_attempts_by_course[att.course_id] = att
        attempts = list(unique_attempts_by_course.values())

        logger.info(
            "[ADMIN STATS] Found %s unique published course attempts for trainee %s",
            len(attempts),
            log_safe(user_id),
        )

        total_courses = len(attempts)
        completed_courses = 0
        in_progress_courses = 0
        total_score = 0
        courses_with_scores = 0

        # Progress counts, assessment configs and assessment attempts for every course at once.
        # These were up to four queries per course inside the loop below, which is what made this
        # tab time out for trainees with several courses.
        stats_course_ids = [att.course_id for att in attempts if att.course_id]
        completed_by_course = _count_progress_by_course(db, user_id, stats_course_ids)
        config_by_course = _assessment_configs_by_course(db, stats_course_ids)
        assessments_by_course = {}
        if stats_course_ids:
            for row in (
                db.query(TraineeAssessment)
                .filter(
                    TraineeAssessment.trainee_id == user_id,
                    TraineeAssessment.course_id.in_(list(set(stats_course_ids))),
                )
                .order_by(TraineeAssessment.completed_at.desc())
                .all()
            ):
                assessments_by_course.setdefault(row.course_id, []).append(row)
        prefetch_s3_module_counts(
            db, {att.course.author_id for att in attempts if att.course and att.course.author_id}
        )

        for attempt in attempts:
            if not attempt.course:
                continue

            course = attempt.course
            total_modules = _count_modules_for_course(course.id, db)
            course_assessments = assessments_by_course.get(course.id, [])
            completed_modules = completed_by_course.get(course.id, 0)
            logger.debug(
                '[ADMIN STATS] Course %s: total_modules=%s, completed_modules=%s',
                log_safe(course.id),
                total_modules,
                completed_modules,
            )

            if completed_modules == total_modules and total_modules > 0:
                # Also check assessment completion
                assessment_config_obj = config_by_course.get(course.id)
                assessment_passed = False
                if assessment_config_obj:
                    if assessment_config_obj.assessment_scope == 'end_of_course':
                        assessment_passed = any(
                            a.module_index is None and a.passed for a in course_assessments
                        )
                    elif assessment_config_obj.assessment_scope == 'end_of_module':
                        # Rows arrive newest-first, so the first one seen for a module index is
                        # the latest attempt for that module.
                        mod_latest = {}
                        for a in course_assessments:
                            if a.module_index is not None and a.module_index not in mod_latest:
                                mod_latest[a.module_index] = a
                        if mod_latest:
                            assessment_passed = (len(mod_latest) == total_modules and all(a.passed for a in mod_latest.values()))
                else:
                    assessment_passed = True  # No assessment configured
                if assessment_passed:
                    completed_courses += 1
                else:
                    in_progress_courses += 1
            elif completed_modules > 0:
                in_progress_courses += 1

            # End-of-course assessment: newest-first ordering means the first match is the latest
            latest_assessment = next(
                (a for a in course_assessments if a.module_index is None), None
            )

            if latest_assessment and latest_assessment.percentage is not None:
                total_score += latest_assessment.percentage
                courses_with_scores += 1
        
        average_score = total_score / courses_with_scores if courses_with_scores > 0 else None
        
        # Calculate not started courses
        not_started_courses = total_courses - completed_courses - in_progress_courses
        
        logger.info('[ADMIN STATS] Stats for trainee %s: total=%s, completed=%s, in_progress=%s, not_started=%s, avg_score=%s', log_safe(user_id), total_courses, completed_courses, in_progress_courses, not_started_courses, log_safe(average_score))
        
        return {
            "success": True,
            "data": {
                "total_courses": total_courses,
                "completed_courses": completed_courses,
                "in_progress_courses": in_progress_courses,
                "not_started_courses": not_started_courses,  # ADDED
                "average_score": average_score
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting trainee stats: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get trainee stats")

@router.get("/super-admin/users/{user_id}/course-progress/{course_id}", tags=["Super Admin: User & Content Management"])
def admin_get_trainee_course_progress(
    user_id: str, 
    course_id: str, 
    current_user: User = Depends(super_admin_required), 
    db: Session = Depends(get_db)
):
    """
    [Super Admin] Get detailed module-by-module progress for a trainee in a specific course.
    """
    try:
        trainee = db.query(User).filter(User.id == user_id).first()
        if not trainee:
            raise HTTPException(status_code=404, detail="Trainee not found")
        
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        logger.info(
            "[ADMIN MODULE PROGRESS] Getting modules for course %s (trainee: %s)",
            ("" if course_id is None else str(course_id).replace("\n", "").replace("\r", "")),
            ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")),
        )
        
        # Get all modules for this course
        modules_data = _get_modules_for_course(course_id, db)
        logger.info('[ADMIN MODULE PROGRESS] _get_modules_for_course returned %s modules', len(modules_data))
        
        # Get completed modules
        completed_module_indices = set(
            progress.completed_module_index 
            for progress in db.query(CourseProgress).filter(
                CourseProgress.user_id == user_id,
                CourseProgress.course_id == course_id
            ).all()
        )
        logger.info('[ADMIN MODULE PROGRESS] Completed module indices: %s', ("" if completed_module_indices is None else str(completed_module_indices).replace("\n", "").replace("\r", "")))
        
        # If no modules found but there's progress, build modules from progress
        if not modules_data and completed_module_indices:
            logger.warning(
                "[ADMIN MODULE PROGRESS] No modules from _get_modules_for_course but found progress. Building from indices."
            )
            max_index = max(completed_module_indices)
            modules_data = [
                {'seg_index': i, 'title': f'Module {i}', 'filename': ''}
                for i in range(1, max_index + 1)
            ]
        
        # Check if this course has module-based assessments configured
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_id
        ).first()
        has_module_assessments = assessment_config and assessment_config.assessment_scope == 'end_of_module'
        total_modules = len(modules_data)
        completed_modules = len(completed_module_indices)

        course_assessment = _build_course_assessment_progress(
            user_id,
            course_id,
            assessment_config,
            total_modules,
            completed_modules,
            db,
        )

        # One query for every module's attempts instead of two queries per module.
        module_assessments_by_index = _module_assessments_by_index(db, user_id, course_id)

        modules_progress = []
        for module in modules_data:
            idx = module['seg_index']
            study_done = idx in completed_module_indices

            # Check for module assessment
            module_assessment, module_attempts_used = module_assessments_by_index.get(idx, (None, 0))

            has_assessment = has_module_assessments
            assessment_score = None
            if module_assessment:
                assessment_score = module_assessment.percentage
                if assessment_score is None and module_assessment.total_possible:
                    assessment_score = round(
                        (module_assessment.score or 0) / module_assessment.total_possible * 100
                    )
            assessment_status = 'passed' if (module_assessment and module_assessment.passed) else ('failed' if module_assessment else 'not_attempted')
            
            # Module is only truly "completed" when study is done AND assessment passed (if required)
            is_completed = study_done and (not has_module_assessments or (module_assessment and module_assessment.passed))
            
            modules_progress.append({
                "module_index": idx,
                "title": module.get('title', f'Module {idx}'),
                "is_completed": is_completed,
                "study_completed": study_done,
                "has_assessment": has_assessment,
                "assessment_score": assessment_score,
                "assessment_status": assessment_status,
                "assessment_attempts_used": module_attempts_used,
            })
        
        logger.info('[ADMIN MODULE PROGRESS] Returning %s modules', len(modules_progress))
        
        return {
            "success": True,
            "data": {
                "course_name": course.source_doc_name,
                "assessment_scope": assessment_config.assessment_scope if assessment_config else None,
                "course_assessment": course_assessment,
                "modules": modules_progress
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting course progress: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get course progress")

@router.get("/super-admin/users/{user_id}/assessment-results/{course_id}", tags=["Super Admin: User & Content Management"])
def admin_get_trainee_assessment_results(
    user_id: str, 
    course_id: str, 
    attempt_number: Optional[int] = Query(None, ge=1),
    current_user: User = Depends(super_admin_required), 
    db: Session = Depends(get_db)
):
    """
    [Super Admin] Get detailed assessment results for a trainee in a specific course.
    Initial load details the latest attempt; pass attempt_number to lazy-load another.
    """
    try:
        trainee = db.query(User).filter(User.id == user_id).first()
        if not trainee:
            raise HTTPException(status_code=404, detail="Trainee not found")
        
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Get latest end-of-course assessment (module_index is null)
        assessment = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == user_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index.is_(None),
        ).order_by(TraineeAssessment.completed_at.desc()).first()
        
        if not assessment:
            raise HTTPException(status_code=404, detail="No assessment found for this trainee")

        return {
            "success": True,
            "data": _build_course_assessment_results_with_history(
                user_id, course_id, db,
                detail_attempt_number=attempt_number,
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting assessment results: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

@router.get("/super-admin/users/{user_id}/module-assessment-results/{course_id}/{module_index}", tags=["Super Admin: User & Content Management"])
def admin_get_trainee_module_assessment_results(
    user_id: str,
    course_id: str,
    module_index: int,
    attempt_number: Optional[int] = Query(None, ge=1),
    current_user: User = Depends(super_admin_required),
    db: Session = Depends(get_db)
):
    """[Super Admin] Get detailed module assessment results for a trainee.

    Initial load details the latest attempt; pass attempt_number to lazy-load another.
    """
    try:
        if not db.query(User).filter(User.id == user_id).first():
            raise HTTPException(status_code=404, detail="Trainee not found")
        if not db.query(Course).filter(Course.id == course_id).first():
            raise HTTPException(status_code=404, detail="Course not found")

        assessment = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == user_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index == module_index
        ).order_by(TraineeAssessment.completed_at.desc()).first()

        if not assessment:
            raise HTTPException(status_code=404, detail="No module assessment found")

        data = _build_module_assessment_results_with_history(
            user_id, course_id, module_index, db,
            detail_attempt_number=attempt_number,
        )
        return {"success": True, "data": data}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting module assessment results: %s", log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)
