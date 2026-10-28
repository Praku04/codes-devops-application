from __future__ import annotations

from fastapi import APIRouter

from core.runtime import *
from models.domain import *
from schemas.domain import *
from services.jobs import *
from services.auth_platform import *
from services.tutor_cache import *
from services.documents import *
from services.knowledge_base import *
from services.agents import *
from services.batch_voice_analytics import (
    build_trainee_voice_progress,
    enrich_batch_analytics_with_voice,
    list_scenarios_published_to_batch,
    trainee_has_voice_activity,
    voice_summary_lines_for_trainee,
)
from features.assessment.router import (
    _count_modules_for_course,
    _get_trainee_course_assessment_state,
    _normalize_mcq_selections,
    _question_to_dict,
    _trainee_question_set_query,
    build_trainee_latest_assessments,
    build_trainee_question_sets,
    end_of_course_attempts_left_by_course,
    end_of_course_attempts_left_by_trainee_course,
    prefetch_s3_module_counts,
)
from utils.security_helpers import log_safe
# Explicit import (not via the star imports above): module counts read one per-request S3 pass
# instead of re-scanning the bucket per course. See services/module_index.py.
from services.module_index import author_report_metadata

router = APIRouter()


def _run_tutor_pre_generation_background(
    owner_id: str,
    course_id: str,
    source_doc_name: str,
) -> None:
    """
    Run tutor pre-generation after publish returns.
    Keeps /publish-* under gateway timeouts (504) while still warming the cache.
    """
    db = SessionLocal()
    try:
        logger.info(
            "[PUBLISH BG] Starting tutor pre-generation for course %s (%s)",
            log_safe(course_id),
            log_safe(source_doc_name),
        )
        tutor_tokens = pre_generate_tutor_responses(db, owner_id, course_id, source_doc_name)
        if tutor_tokens > 0:
            from services.token_tracking import track_tutor_pre_generation_tokens

            track_tutor_pre_generation_tokens(db, owner_id, course_id, tutor_tokens)
            course = db.query(Course).filter(Course.id == course_id).first()
            if course:
                course.tokens_used = (course.tokens_used or 0) + tutor_tokens
                tutor_input_tokens = int(tutor_tokens * 0.6)
                tutor_output_tokens = tutor_tokens - tutor_input_tokens
                course.generation_cost = (course.generation_cost or 0) + calculate_token_cost(
                    tutor_input_tokens, tutor_output_tokens, BEDROCK_GENERATION_MODEL
                )
            display = log_safe(course_display_label(course)) if course else "your course"
            if not display:
                display = "your course"
            db.add(Notification(
                user_id=owner_id,
                message=(
                    f"✅ Tutor responses finished pre-generating for '{display}'. "
                    "Trainees will get instant tutor playback."
                ),
                related_link="/trainer-dashboard",
                course_id=course_notification_id(course),
            ))
            db.commit()
            logger.info(
                "[PUBLISH BG] Tutor pre-generation complete for %s (%s tokens)",
                log_safe(course_id),
                tutor_tokens,
            )
        else:
            logger.info(
                "[PUBLISH BG] No new tutor tokens for course %s (already cached or no modules)",
                log_safe(course_id),
            )
    except Exception as exc:
        db.rollback()
        logger.error(
            "[PUBLISH BG] Tutor pre-generation failed for course %s: %s",
            log_safe(course_id),
            log_safe(exc),
            exc_info=True,
        )
    finally:
        db.close()


@router.post("/trainer/publish-course", tags=["Trainer Dashboard & Courses"])
def publish_course(
    req: PublishCourseRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db),
):
    """
    Publishes or updates a course, making it available to trainees in a process.

    If the course already exists, it is updated and re-published, resetting attempts for trainees.
    If it's a new course, it is created and assigned to all relevant trainees.
    
    **Cross-Process Sharing (Super Admin Only):**
    - Super admin can specify target_process_id to share a course across processes
    - Example: RPA trainer's course can be shared with BPS trainees by BPS super admin

    - **Required Role**: Trainer or Super Admin.
    - **Raises 400**: If the trainer is not assigned to a process or no trainees exist in the process.
    - **Returns**: Success message with number of trainees assigned.
    """
    # Find the course to determine the actual owner
    existing_course = db.query(Course).filter(
        Course.source_doc_name == req.source_doc_name
    ).first()
    
    # Determine the course owner and target process
    if existing_course:
        # Publishing existing course
        course_owner_id = existing_course.author_id
        
        # Check authorization: must be course owner OR super admin
        if course_owner_id != user.id and user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="You don't have permission to publish this course")
        
        is_super_admin_action = (user.role == 'super_admin' and course_owner_id != user.id)
        
        # Determine target process for assignment
        if req.target_process_id:
            # Super admin specified a target process (cross-process sharing)
            if user.role != 'super_admin':
                raise HTTPException(status_code=403, detail="Only super admin can publish courses to different processes")
            process_id_for_assignment = req.target_process_id
            is_cross_process = (process_id_for_assignment != existing_course.process_id)
            logger.info(
                '[CROSS-PROCESS] Super admin publishing course from %s to %s',
                str(existing_course.process_id).replace("\n", "").replace("\r", ""),
                str(process_id_for_assignment).replace("\n", "").replace("\r", ""),
            )
        else:
            # Use the existing course's process
            process_id_for_assignment = existing_course.process_id
            is_cross_process = False
    else:
        # Creating new course - must be the owner
        course_owner_id = user.id
        is_super_admin_action = False
        is_cross_process = False
        
        # Determine target process
        if req.target_process_id:
            if user.role != 'super_admin':
                raise HTTPException(status_code=403, detail="Only super admin can specify target process")
            process_id_for_assignment = req.target_process_id
        else:
            process_id_for_assignment = user.process_id
        
        if user.role == 'super_admin' and not process_id_for_assignment:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Super Admins not assigned to a process must specify target_process_id when publishing courses.")
    
    if not process_id_for_assignment:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot publish course: No target process specified.")
    
    # Check if trainees exist in the TARGET process
    trainees = db.query(User).filter(User.process_id == process_id_for_assignment, User.role == 'trainee').all()
    if not trainees:
        process = db.query(Process).filter(Process.id == process_id_for_assignment).first()
        process_name = process.name if process else "the target process"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=f"Unable to publish course: No trainees found in {process_name}. Please ensure trainees are registered and assigned to the target process before publishing."
        )
    
    # req.source_doc_name is the randomized storage name (VAPT #4). Every message below is read
    # by the trainer, so they all quote the course title / uploaded filename instead.
    doc_label = (
        course_display_label(existing_course)
        if existing_course
        else display_name_for_stored_document(db, course_owner_id, req.source_doc_name)
    )

    # VALIDATION: Check if modules exist in S3
    logger.info(
        "[PUBLISH VALIDATION] Checking modules for course %s...",
        ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
    )
    total_modules = 0

    # CRITICAL FIX: Parse source_documents JSON array to check ALL documents in this course
    course_docs_normalized = []
    try:
        if existing_course and existing_course.source_documents:
            # Parse JSON array of source documents
            source_docs_list = json.loads(existing_course.source_documents)
            if isinstance(source_docs_list, list):
                for doc in source_docs_list:
                    doc_normalized = normalize_document_name(doc)
                    course_docs_normalized.append(doc_normalized)
                logger.info(
                    "[PUBLISH VALIDATION] Parsed %s documents from source_documents: %s",
                    len(course_docs_normalized),
                    ("" if course_docs_normalized is None else str(course_docs_normalized).replace("\n", "").replace("\r", "")),
                )
    except Exception as e:
        logger.warning('[PUBLISH VALIDATION] Could not parse source_documents JSON: %s', e)
    
    # Fallback to single source_doc_name if source_documents is empty
    if not course_docs_normalized:
        course_doc_normalized = normalize_document_name(req.source_doc_name)
        course_docs_normalized = [course_doc_normalized]
        logger.info(
            "[PUBLISH VALIDATION] Using fallback single source_doc_name: %s",
            ("" if course_doc_normalized is None else str(course_doc_normalized).replace("\n", "").replace("\r", "")),
        )
    
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{course_owner_id}/generated_reports/")
        
        for page in pages:
            if 'Contents' not in page:
                continue
                
            for obj in page['Contents']:
                if obj['Key'].endswith('.json'):
                    try:
                        meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                        meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                        source_doc = meta_data.get('source_doc', '')
                        
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
                            total_modules += 1
                    except Exception as e:
                        logger.error('[PUBLISH VALIDATION] Error reading module metadata %s: %s', ("" if obj['Key'] is None else str(obj['Key']).replace("\n", "").replace("\r", "")), e)
    except Exception as e:
        logger.error('[PUBLISH VALIDATION] Error counting modules: %s', e, exc_info=True)
    
    if total_modules == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to publish course: No modules found for '{doc_label}'. Please generate course modules before publishing."
        )
    
    logger.info(
        "[PUBLISH VALIDATION] Found %s modules for %s",
        total_modules,
        ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
    )
    
    # VALIDATION: Check if assessment configuration exists
    # For existing courses, use the course_id; for new courses, check by source_doc_name
    if existing_course:
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == existing_course.id
        ).first()
    else:
        # For new courses, we can't check by course_id yet, so we'll skip this check
        # and validate after course creation
        assessment_config = None
    
    if existing_course and not assessment_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to publish course: Assessment configuration not found for '{doc_label}'. Please configure assessments before publishing."
        )
    
    # VALIDATION: Check if assessment questions have been generated
    if existing_course and assessment_config:
        question_count = db.query(func.count(AssessmentQuestion.id)).filter(
            AssessmentQuestion.course_id == existing_course.id
        ).scalar()
        
        if question_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unable to publish course: No assessment questions found for '{doc_label}'. Please generate assessment questions before publishing."
            )
        
        logger.info(
            "[PUBLISH VALIDATION] Found %s assessment questions for %s",
            question_count,
            ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
        )
    
    try:
        # CRITICAL FIX: Use max_attempts from assessment config, not hardcoded 1
        if existing_course and assessment_config:
            initial_attempts = assessment_config.max_attempts
            logger.info('[PUBLISH] Setting initial attempts to %s (from assessment config)', ("" if initial_attempts is None else str(initial_attempts).replace("\n", "").replace("\r", "")))
        else:
            initial_attempts = 1
            logger.info('[PUBLISH] Using default initial attempts: %s', ("" if initial_attempts is None else str(initial_attempts).replace("\n", "").replace("\r", "")))
        
        # For cross-process sharing, check if we need to create a new course record
        if is_cross_process:
            # Check if course already exists for target process
            target_course = db.query(Course).filter(
                Course.source_doc_name == req.source_doc_name,
                Course.process_id == process_id_for_assignment
            ).first()
            
            if target_course:
                # Course already shared with this process, update it
                course = target_course
                logger.info('[CROSS-PROCESS] Updating existing course for process %s', ("" if process_id_for_assignment is None else str(process_id_for_assignment).replace("\n", "").replace("\r", "")))
            else:
                # Create new course record for target process (reference to original).
                # The title and the storage-name -> uploaded-filename lists are copied across:
                # without them this row has no way back to what the trainer uploaded, and every
                # screen that reads it (My Modules, View Docs, KB search) would fall back to the
                # randomized storage name (VAPT #4).
                course = Course(
                    author_id=course_owner_id,  # Keep original author
                    source_doc_name=req.source_doc_name,
                    process_id=process_id_for_assignment,  # Target process
                    initial_attempts=initial_attempts,
                    is_live=True,
                    course_title=existing_course.course_title if existing_course else None,
                    source_documents=existing_course.source_documents if existing_course else None,
                    source_document_display_names=(
                        existing_course.source_document_display_names if existing_course else None
                    ),
                )
                db.add(course)
                logger.info('[CROSS-PROCESS] Created new course record for process %s', ("" if process_id_for_assignment is None else str(process_id_for_assignment).replace("\n", "").replace("\r", "")))
        else:
            # Same process or new course
            course = existing_course
        
        if course:
            course.is_live = True
            course.initial_attempts = initial_attempts
            if is_cross_process:
                target_process = db.query(Process).filter(Process.id == process_id_for_assignment).first()
                message = f"Course '{doc_label}' shared with {target_process.name if target_process else 'target process'}."
            else:
                message = f"Course '{doc_label}' updated and re-published."
        else:
            course = Course(
                author_id=course_owner_id,
                source_doc_name=req.source_doc_name,
                process_id=process_id_for_assignment,
                initial_attempts=initial_attempts,
                is_live=True,
            )
            db.add(course)
            message = f"Course '{doc_label}' published successfully."
        db.flush()

        # Track token usage when course is published
        logger.info(
            "Module count: %s, resolving generation tokens...",
            total_modules,
        )

        # Track module generation tokens (text LLM + vision) from job data or estimates
        from services.token_tracking import persist_course_generation_tokens

        gen_totals = persist_course_generation_tokens(
            db,
            course_owner_id,
            course.id,
            req.source_doc_name,
            total_modules,
        )
        logger.info(
            "Tracked module generation for %s: %s text + %s vision = %s tokens",
            log_safe(req.source_doc_name),
            gen_totals["module_text"],
            gen_totals["module_vision"],
            gen_totals["total"],
        )
        
        # Tutor pre-generation runs in the background after publish to avoid gateway 504s.
        schedule_tutor_pre_gen = total_modules > 0
        if not schedule_tutor_pre_gen:
            logger.warning(
                "No modules to pre-generate for course %s",
                ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
            )
        
        # Get course title for notifications
        course_display_name = course_display_label(course)
        
        # Existing attempt rows for every target trainee in one query instead of one per trainee,
        # so publishing to a large audience does not grow with the trainee count. `setdefault`
        # keeps the first row returned, matching the `.first()` this replaces.
        existing_attempt_by_trainee = {}
        target_trainee_ids = [trainee.id for trainee in trainees]
        if target_trainee_ids:
            for attempt_row in db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id.in_(target_trainee_ids),
                TraineeAttempt.course_id == course.id,
            ).all():
                existing_attempt_by_trainee.setdefault(attempt_row.trainee_id, attempt_row)

        for trainee in trainees:
            attempt = existing_attempt_by_trainee.get(trainee.id)
            if attempt:
                attempt.attempts_left = initial_attempts
            else:
                new_attempt = TraineeAttempt(trainee_id=trainee.id, course_id=course.id, attempts_left=initial_attempts)
                db.add(new_attempt)
                # Keeps a trainee listed twice from getting a second row, which the per-trainee
                # query this replaces prevented through autoflush.
                existing_attempt_by_trainee[trainee.id] = new_attempt
            db.add(Notification(user_id=trainee.id, message=f"A course '{course_display_name}' has been assigned/updated by {user.email}.", related_link="/trainee-dashboard", course_id=course_notification_id(course)))
        
        # Notify trainer if super admin published their course
        if is_super_admin_action:
            trainer = db.query(User).filter(User.id == course_owner_id).first()
            if trainer:
                target_process = db.query(Process).filter(Process.id == process_id_for_assignment).first()
                process_name = target_process.name if target_process else "another process"
                
                if is_cross_process:
                    notification_msg = f"Super Admin shared your course '{course_display_name}' with {process_name} ({len(trainees)} trainees)"
                else:
                    notification_msg = f"Super Admin published your course '{course_display_name}' to {len(trainees)} trainees"
                
                notification = Notification(
                    user_id=trainer.id,
                    message=notification_msg,
                    course_id=course_notification_id(course),
                )
                db.add(notification)
                logger.info('[PUBLISH] Notified trainer %s about super admin action', ("" if course_owner_id is None else str(course_owner_id).replace("\n", "").replace("\r", "")))
        
        # Notify the trainer (course owner) about successful publish with full details
        trainer_notification_msg = (
            f"✅ Course '{course_display_name}' published successfully — "
            f"{total_modules} module(s) ready, {len(trainees)} trainee(s) enrolled. "
            f"Tutor responses are being prepared in the background."
        )
        db.add(Notification(user_id=course_owner_id, message=trainer_notification_msg, course_id=course_notification_id(course)))

        log_activity(db, user.id, "PUBLISH_COURSE", f"Published '{doc_label}' to process {process_id_for_assignment}")
        db.commit()

        if schedule_tutor_pre_gen:
            background_tasks.add_task(
                _run_tutor_pre_generation_background,
                course_owner_id,
                course.id,
                req.source_doc_name,
            )

        if is_cross_process:
            return {
                "success": True,
                "message": f"{message} Course content from original author is now accessible to {len(trainees)} trainees in the target process. Tutor responses are being prepared in the background.",
                "trainee_count": len(trainees),
                "module_count": total_modules,
            }
        else:
            return {
                "success": True,
                "message": f"{message} It is now available to {len(trainees)} trainees. Tutor responses are being prepared in the background.",
                "trainee_count": len(trainees),
                "module_count": total_modules,
            }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error('Error publishing course: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to publish course.")


def _is_newer_assessment(candidate, current) -> bool:
    """True when ``candidate`` is the later attempt of the two.

    ``completed_at`` is nullable, and comparing a NULL timestamp with a real one raises, which
    used to be enough for one unfinished attempt to break a whole analytics page. A row without
    a timestamp therefore loses to any row that has one.
    """
    if current is None:
        return True
    if current.completed_at is None:
        return candidate.completed_at is not None
    if candidate.completed_at is None:
        return False
    return candidate.completed_at > current.completed_at


def _is_course_fully_completed(
    trainee_id: str,
    course_id: str,
    total_modules: int,
    assessment_config,
    db: Session,
    completed_modules: Optional[int] = None,
    assessments: Optional[list] = None,
) -> bool:
    """Returns True only when study is done AND assessment is passed (if configured).

    ``completed_modules`` and ``assessments`` let a caller that already loaded a whole page of
    trainees hand the values over instead of paying two queries per trainee/course pair;
    ``assessments`` is every attempt this trainee made on this course, in any order. When they
    are omitted the original two queries run exactly as before.
    """
    if completed_modules is None:
        completed_modules = db.query(func.count(CourseProgress.id)).filter_by(
            user_id=trainee_id, course_id=course_id
        ).scalar() or 0
    if completed_modules < total_modules or total_modules == 0:
        return False
    if not assessment_config:
        return True  # No assessment required
    if assessment_config.assessment_scope == 'end_of_course':
        if assessments is None:
            passed = db.query(TraineeAssessment).filter(
                TraineeAssessment.trainee_id == trainee_id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index.is_(None),
                TraineeAssessment.passed == True
            ).first()
        else:
            passed = next((a for a in assessments if a.module_index is None and a.passed), None)
        return passed is not None
    elif assessment_config.assessment_scope == 'end_of_module':
        if assessments is None:
            mod_assessments = db.query(TraineeAssessment).filter(
                TraineeAssessment.trainee_id == trainee_id,
                TraineeAssessment.course_id == course_id,
                TraineeAssessment.module_index != None
            ).all()
        else:
            mod_assessments = [a for a in assessments if a.module_index is not None]
        if not mod_assessments:
            return False
        mod_latest = {}
        for a in mod_assessments:
            if _is_newer_assessment(a, mod_latest.get(a.module_index)):
                mod_latest[a.module_index] = a
        return len(mod_latest) == total_modules and all(a.passed for a in mod_latest.values())
    return False


def _get_assessment_status(trainee_id: str, course_id: str, total_modules: int, assessment_config, db: Session) -> str:
    """Returns assessment status string for a trainee/course."""
    if not assessment_config:
        return 'not_configured'
    if assessment_config.assessment_scope == 'end_of_course':
        latest = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == trainee_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index.is_(None)
        ).order_by(TraineeAssessment.completed_at.desc()).first()
        if not latest:
            return 'not_started'
        return 'passed' if latest.passed else 'failed'
    elif assessment_config.assessment_scope == 'end_of_module':
        mod_assessments = db.query(TraineeAssessment).filter(
            TraineeAssessment.trainee_id == trainee_id,
            TraineeAssessment.course_id == course_id,
            TraineeAssessment.module_index != None
        ).all()
        if not mod_assessments:
            return 'not_started'
        mod_latest = {}
        for a in mod_assessments:
            if _is_newer_assessment(a, mod_latest.get(a.module_index)):
                mod_latest[a.module_index] = a
        effective_total = total_modules if total_modules > 0 else len(mod_latest)
        if len(mod_latest) < effective_total:
            return 'in_progress'
        return 'passed' if all(a.passed for a in mod_latest.values()) else 'failed'
    return 'not_started'


def _latest_module_assessments(rows) -> dict:
    """Pick latest TraineeAssessment per module_index from preloaded rows."""
    mod_latest = {}
    for a in rows:
        if a.module_index is None:
            continue
        prev = mod_latest.get(a.module_index)
        if prev is None:
            mod_latest[a.module_index] = a
            continue
        a_ts = a.completed_at
        p_ts = prev.completed_at
        if a_ts is None:
            continue
        if p_ts is None:
            mod_latest[a.module_index] = a
            continue
        a_cmp = a_ts.replace(tzinfo=None) if getattr(a_ts, "tzinfo", None) else a_ts
        p_cmp = p_ts.replace(tzinfo=None) if getattr(p_ts, "tzinfo", None) else p_ts
        if a_cmp > p_cmp:
            mod_latest[a.module_index] = a
    return mod_latest


def _assessment_status_cached(assessment_config, total_modules: int, assessment_rows) -> str:
    if not assessment_config:
        return "not_configured"
    if assessment_config.assessment_scope == "end_of_course":
        course_rows = [a for a in assessment_rows if a.module_index is None]
        if not course_rows:
            return "not_started"

        def _sort_key(row):
            ts = row.completed_at
            if ts is None:
                return datetime.min
            # Normalize aware/naive datetimes for safe comparison.
            return ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts

        latest = max(course_rows, key=_sort_key)
        return "passed" if latest.passed else "failed"
    if assessment_config.assessment_scope == "end_of_module":
        mod_latest = _latest_module_assessments(assessment_rows)
        if not mod_latest:
            return "not_started"
        effective_total = total_modules if total_modules > 0 else len(mod_latest)
        if len(mod_latest) < effective_total:
            return "in_progress"
        return "passed" if all(a.passed for a in mod_latest.values()) else "failed"
    return "not_started"


def _course_completed_cached(
    completed_modules: int,
    total_modules: int,
    assessment_config,
    assessment_rows,
) -> bool:
    if completed_modules < total_modules or total_modules == 0:
        return False
    if not assessment_config:
        return True
    if assessment_config.assessment_scope == "end_of_course":
        return any(a.module_index is None and a.passed for a in assessment_rows)
    if assessment_config.assessment_scope == "end_of_module":
        mod_latest = _latest_module_assessments(assessment_rows)
        return len(mod_latest) == total_modules and all(a.passed for a in mod_latest.values())
    return False


@router.get("/trainer/dashboard", response_model=List[TrainerTraineeInfo], tags=["Trainer Dashboard & Courses"])
def get_trainer_dashboard(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Retrieves the dashboard view for a trainer or super admin.

    Shows progress and attempt status for all trainees in the user's
    process group for the courses THEY have authored.

    - **Required Role**: Trainer or Super Admin.
    - **Note**: Super admin sees their OWN courses and trainees (acts as trainer)
    """
    logger.info(
        "[TRAINER DASHBOARD] User %s (role: %s) loading their own dashboard",
        log_safe(user.email),
        log_safe(user.role),
    )

    my_courses = (
        db.query(Course)
        .filter(Course.author_id == user.id, Course.is_live == True)
        .all()
    )
    if not my_courses:
        logger.info("[TRAINER DASHBOARD] User %s has 0 published courses", log_safe(user.email))
        return []

    my_course_ids = [c.id for c in my_courses]
    course_by_id = {c.id: c for c in my_courses}
    logger.info(
        "[TRAINER DASHBOARD] User %s has %s published courses",
        log_safe(user.email),
        len(my_course_ids),
    )

    # Count modules once per course (S3 list is expensive — do not repeat per enrollment).
    # Every course here is this trainer's own, so one pass over their prefix produces all the
    # counts; without it each course re-filters the whole cached index.
    prefetch_s3_module_counts(db, {user.id})
    module_count_by_course = {
        course_id: _count_modules_for_course(course_id, db) for course_id in my_course_ids
    }

    config_by_course = {
        cfg.course_id: cfg
        for cfg in db.query(AssessmentConfig)
        .filter(AssessmentConfig.course_id.in_(my_course_ids))
        .all()
    }

    attempt_rows = (
        db.query(TraineeAttempt, User)
        .join(User, User.id == TraineeAttempt.trainee_id)
        .filter(
            TraineeAttempt.course_id.in_(my_course_ids),
            User.role == "trainee",
            User.process_id == user.process_id,
        )
        .all()
    )
    if not attempt_rows:
        logger.info("[TRAINER DASHBOARD] Returning 0 trainee course stats")
        return []

    trainee_ids = {trainee.id for _, trainee in attempt_rows}

    progress_map = {
        (uid, cid): int(cnt or 0)
        for uid, cid, cnt in (
            db.query(
                CourseProgress.user_id,
                CourseProgress.course_id,
                func.count(CourseProgress.id),
            )
            .filter(
                CourseProgress.user_id.in_(trainee_ids),
                CourseProgress.course_id.in_(my_course_ids),
            )
            .group_by(CourseProgress.user_id, CourseProgress.course_id)
            .all()
        )
    }

    assessments_by_pair = {}
    for row in (
        db.query(TraineeAssessment)
        .filter(
            TraineeAssessment.trainee_id.in_(trainee_ids),
            TraineeAssessment.course_id.in_(my_course_ids),
        )
        .all()
    ):
        key = (row.trainee_id, row.course_id)
        assessments_by_pair.setdefault(key, []).append(row)

    # Remaining end-of-course attempts per (trainee, course) - two grouped queries for the whole
    # dashboard, rather than the TraineeAttempt.attempts_left each row carries. That column is
    # rewritten by /admin/reset-attempts, /admin/sync-course-attempts and the auto-sync just below,
    # so the trainer was shown remaining attempts the trainee could not actually use.
    course_attempts_left_by_pair = end_of_course_attempts_left_by_trainee_course(
        db, trainee_ids, my_course_ids, config_by_course
    )

    stats = []
    sync_needed = False
    for att, trainee in attempt_rows:
        course = course_by_id.get(att.course_id)
        if not course:
            continue

        total_modules = module_count_by_course.get(course.id, 0)
        completed_modules = progress_map.get((trainee.id, course.id), 0)
        assessment_config = config_by_course.get(course.id)
        assessment_rows = assessments_by_pair.get((trainee.id, course.id), [])

        max_attempts_per_module = 3
        assessment_scope = None
        if assessment_config:
            assessment_scope = assessment_config.assessment_scope
            max_attempts_per_module = assessment_config.max_attempts
            # Auto-sync legacy attempts_left==1 for MODULE-scope only. End-of-course remaining
            # is computed from stored assessments; rewriting the column here recreated the
            # "2 Attempts Left" card while results said 3/3 used.
            if (
                assessment_scope == "end_of_module"
                and att.attempts_left != max_attempts_per_module
                and att.attempts_left == 1
            ):
                logger.warning(
                    "[TRAINER DASHBOARD] Auto-syncing attempts for trainee %s in course %s: %s -> %s",
                    log_safe(trainee.email),
                    log_safe(course.source_doc_name),
                    log_safe(att.attempts_left),
                    log_safe(max_attempts_per_module),
                )
                att.attempts_left = max_attempts_per_module
                sync_needed = True

        # End-of-course: counted, the way the assessment gates count. Module scope keeps the legacy
        # column, whose per-module budget is reported through max_attempts_per_module instead.
        attempts_left_display = course_attempts_left_by_pair.get(
            (trainee.id, course.id), att.attempts_left
        )

        stats.append(
            TrainerTraineeInfo(
                trainee_email=trainee.email,
                course_name=course.source_doc_name,
                course_title=course.course_title,
                attempts_left=attempts_left_display,
                total_modules=total_modules,
                completed_modules=completed_modules,
                assessment_scope=assessment_scope,
                max_attempts_per_module=max_attempts_per_module,
                course_completed=_course_completed_cached(
                    completed_modules, total_modules, assessment_config, assessment_rows
                ),
                assessment_status=_assessment_status_cached(
                    assessment_config, total_modules, assessment_rows
                ),
            )
        )

    if sync_needed:
        db.commit()

    logger.info("[TRAINER DASHBOARD] Returning %s trainee course stats", len(stats))
    return stats


# ==============================================================================
# 9.1 BATCH MANAGEMENT ENDPOINTS
# ==============================================================================
@router.post("/trainer/batches", tags=["Batch Management"])
def create_batch(req: CreateBatchRequest, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Create a new trainee batch."""
    if not user.process_id:
        raise HTTPException(status_code=400, detail="Trainer must be assigned to a process")
    
    # Verify all trainee IDs belong to the trainer's process
    if req.trainee_ids:
        trainees = db.query(User).filter(
            User.id.in_(req.trainee_ids),
            User.role == "trainee",
            User.process_id == user.process_id
        ).all()
        if len(trainees) != len(req.trainee_ids):
            raise HTTPException(status_code=400, detail="Some trainee IDs are invalid or not in your process")
    
    try:
        batch = TraineeBatch(
            trainer_id=user.id,
            batch_name=req.batch_name,
            batch_description=req.batch_description,
            process_id=user.process_id
        )
        db.add(batch)
        db.flush()
        
        for trainee_id in req.trainee_ids:
            member = BatchMember(batch_id=batch.id, trainee_id=trainee_id)
            db.add(member)
        
        db.commit()
        log_activity(db, user.id, "BATCH_CREATED", f"Created batch '{req.batch_name}' with {len(req.trainee_ids)} trainees")
        return {"success": True, "message": f"Batch '{req.batch_name}' created successfully", "batch_id": batch.id}
    except Exception as e:
        db.rollback()
        logger.error('Error creating batch: %s', e)
        raise HTTPException(status_code=500, detail="Failed to create batch")

@router.get("/trainer/batches", tags=["Batch Management"])
def get_trainer_batches(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Get all batches created by the trainer."""
    print(f"\n=== GET BATCHES REQUEST ===")
    print(f"User ID: {user.id}")
    print(f"User Email: {user.email}")
    print(f"User Role: {user.role}")
    
    batches = db.query(TraineeBatch).filter(
        TraineeBatch.trainer_id == user.id,
        TraineeBatch.is_active == True
    ).all()
    
    print(f"Found {len(batches)} batches for this trainer")
    
    # Membership of every batch in one query instead of one query per batch.
    members_by_batch = defaultdict(list)
    if batches:
        for member, trainee in db.query(BatchMember, User).join(
            User, BatchMember.trainee_id == User.id
        ).filter(BatchMember.batch_id.in_([b.id for b in batches])).all():
            members_by_batch[member.batch_id].append(trainee)

    batch_list = []
    for batch in batches:
        print(f"  - Batch: {batch.batch_name} (ID: {batch.id})")
        members = members_by_batch.get(batch.id, [])

        print(f"    Members: {len(members)}")

        batch_list.append({
            "id": batch.id,
            "batch_name": batch.batch_name,
            "batch_description": batch.batch_description,
            "created_at": batch.created_at.isoformat(),
            "member_count": len(members),
            "members": [{"id": trainee.id, "email": trainee.email} for trainee in members]
        })

    print(f"Returning {len(batch_list)} batches")
    print(f"=== END GET BATCHES ===\n")
    
    # Build trainer-level manual_trainees (all trainees with manually-added courses)
    # Get ALL manually-added attempts for this trainer's courses (batch_id=None)
    trainer_course_ids = {row[0] for row in db.query(Course.id).filter(
        Course.author_id == user.id,
        Course.is_live == True
    ).all()}
    
    logger.info('[MANUAL TRAINEES] trainer_course_ids count: %s', len(trainer_course_ids))
    
    # Get all trainer's batch IDs to identify truly manual attempts
    trainer_batch_ids = {b["id"] for b in batch_list}

    manual_attempts_all = []
    if trainer_course_ids:
        # Include attempts with batch_id=None OR batch_id not belonging to this trainer's batches
        all_course_attempts = db.query(TraineeAttempt).filter(
            TraineeAttempt.course_id.in_(trainer_course_ids)
        ).all()
        manual_attempts_all = [
            a for a in all_course_attempts
            if a.batch_id is None or a.batch_id not in trainer_batch_ids
        ]
    
    logger.info('[MANUAL TRAINEES] manual_attempts_all count: %s', len(manual_attempts_all))

    # Courses and trainees for those attempts in one query each rather than two per attempt.
    manual_courses_by_id = {}
    manual_trainees_by_id = {}
    if manual_attempts_all:
        manual_course_ids = {a.course_id for a in manual_attempts_all if a.course_id}
        if manual_course_ids:
            manual_courses_by_id = {
                c.id: c for c in db.query(Course).filter(Course.id.in_(manual_course_ids)).all()
            }
        manual_trainee_ids = {a.trainee_id for a in manual_attempts_all if a.trainee_id}
        if manual_trainee_ids:
            manual_trainees_by_id = {
                u.id: u for u in db.query(User).filter(User.id.in_(manual_trainee_ids)).all()
            }

    # Group by trainee — show all courses where this specific attempt has batch_id=None
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

    return {"success": True, "batches": batch_list, "manual_trainees": manual_trainees}

@router.get("/trainer/batches/{batch_id}", tags=["Batch Management"])
def get_batch_details(batch_id: str, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Get details of a specific batch."""
    batch = db.query(TraineeBatch).filter(
        TraineeBatch.id == batch_id,
        TraineeBatch.trainer_id == user.id,
        TraineeBatch.is_active == True
    ).first()
    
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    
    # Get batch members
    members = db.query(BatchMember, User).join(
        User, BatchMember.trainee_id == User.id
    ).filter(BatchMember.batch_id == batch.id).all()
    
    batch_data = {
        "id": batch.id,
        "batch_name": batch.batch_name,
        "batch_description": batch.batch_description,
        "created_at": batch.created_at.isoformat(),
        "member_count": len(members),
        "members": [{"id": m.User.id, "email": m.User.email} for m in members]
    }
    
    # Courses published directly to this batch
    batch_course_ids = {row[0] for row in db.query(TraineeAttempt.course_id).filter(
        TraineeAttempt.batch_id == batch_id
    ).distinct().all()}
    # The manual attempts of every member, and every course either list names, in one query each.
    # This used to be an attempt query per member plus a course query per attempt.
    member_ids = [m.User.id for m in members]
    manual_attempts_by_trainee = defaultdict(list)
    if member_ids:
        for attempt in db.query(TraineeAttempt).filter(
            TraineeAttempt.trainee_id.in_(member_ids),
            TraineeAttempt.batch_id.is_(None)
        ).all():
            manual_attempts_by_trainee[attempt.trainee_id].append(attempt)
    referenced_course_ids = set(batch_course_ids)
    for attempts in manual_attempts_by_trainee.values():
        referenced_course_ids.update(a.course_id for a in attempts if a.course_id)
    courses_by_id = {}
    if referenced_course_ids:
        courses_by_id = {
            c.id: c for c in db.query(Course).filter(Course.id.in_(referenced_course_ids)).all()
        }

    batch_courses = []
    for course_id in batch_course_ids:
        course_obj = courses_by_id.get(course_id)
        if course_obj and course_obj.is_live:
            batch_courses.append({"id": course_obj.id, "title": course_display_label(course_obj)})

    # Per-trainee manually added courses for this batch's members (batch_id=None, not in batch_courses)
    # No dedup needed here — this is a single batch view
    manual_trainees = []
    seen_trainee_ids = set()
    for m in members:
        if m.User.id in seen_trainee_ids:
            continue
        seen_trainee_ids.add(m.User.id)
        manual_courses_for_trainee = []
        for attempt in manual_attempts_by_trainee.get(m.User.id, []):
            course_obj = courses_by_id.get(attempt.course_id)
            if course_obj and course_obj.is_live and attempt.course_id not in batch_course_ids:
                manual_courses_for_trainee.append({"id": course_obj.id, "title": course_display_label(course_obj)})
        if manual_courses_for_trainee:
            manual_trainees.append({"email": m.User.email, "courses": manual_courses_for_trainee})

    batch_data["batch_courses"] = batch_courses
    batch_data["batch_voice_scenarios"] = [
        {"id": s["id"], "title": s["title"], "category": s.get("category")}
        for s in list_scenarios_published_to_batch(db, batch_id, batch.process_id)
    ]
    batch_data["manual_trainees"] = manual_trainees
    
    return {"success": True, "batch": batch_data}

@router.post("/trainer/batches/{batch_id}/update", tags=["Batch Management"])
def update_batch(batch_id: str, req: UpdateBatchRequest, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Update a batch (name, description, or members)."""
    batch = db.query(TraineeBatch).filter(
        TraineeBatch.id == batch_id,
        TraineeBatch.trainer_id == user.id,
        TraineeBatch.is_active == True
    ).first()
    
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    
    try:
        if req.batch_name is not None:
            batch.batch_name = req.batch_name
        if req.batch_description is not None:
            batch.batch_description = req.batch_description
        
        if req.trainee_ids is not None:
            if req.trainee_ids:
                trainees = db.query(User).filter(
                    User.id.in_(req.trainee_ids),
                    User.role == "trainee",
                    User.process_id == user.process_id
                ).all()
                if len(trainees) != len(req.trainee_ids):
                    raise HTTPException(status_code=400, detail="Some trainee IDs are invalid")
            
            db.query(BatchMember).filter(BatchMember.batch_id == batch_id).delete()
            for trainee_id in req.trainee_ids:
                db.add(BatchMember(batch_id=batch_id, trainee_id=trainee_id))
        
        batch.updated_at = datetime.now(timezone.utc)
        db.commit()
        log_activity(db, user.id, "BATCH_UPDATED", f"Updated batch '{batch.batch_name}'")
        return {"success": True, "message": "Batch updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error('Error updating batch: %s', e)
        raise HTTPException(status_code=500, detail="Failed to update batch")

@router.post("/trainer/batches/{batch_id}/delete", tags=["Batch Management"])
def delete_batch(batch_id: str, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Delete a batch (soft delete)."""
    batch = db.query(TraineeBatch).filter(
        TraineeBatch.id == batch_id,
        TraineeBatch.trainer_id == user.id,
        TraineeBatch.is_active == True
    ).first()
    
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    
    try:
        batch.is_active = False
        batch.updated_at = datetime.now(timezone.utc)
        db.commit()
        log_activity(db, user.id, "BATCH_DELETED", f"Deleted batch '{batch.batch_name}'")
        return {"success": True, "message": "Batch deleted successfully"}
    except Exception as e:
        db.rollback()
        logger.error('Error deleting batch: %s', e)
        raise HTTPException(status_code=500, detail="Failed to delete batch")

def _parse_assessment_questions(
    assessment: TraineeAssessment,
    db: Session,
    configs_by_course: Optional[dict] = None,
    question_sets: Optional[dict] = None,
) -> dict:
    """
    Parse a TraineeAssessment and return per-question right/wrong breakdown.
    Uses TraineeQuestionSet (unique questions shown to the trainee) so answer IDs match.
    correct_count and total are derived from the stored score/total_possible for accuracy.

    ``configs_by_course`` and ``question_sets`` let a caller that is walking many attempts pass
    maps it already loaded, so a page of trainees costs two queries instead of two per attempt.
    Both default to ``None``, in which case the per-attempt queries run as before.
    """
    result = {"correct": [], "incorrect": [], "total": 0, "correct_count": 0}
    if not assessment or not assessment.questions_data:
        return result
    try:
        # ── Ground-truth counts from the stored assessment record ──────────
        # These are always accurate regardless of question type (MCQ/subjective)
        if configs_by_course is None:
            config = db.query(AssessmentConfig).filter(
                AssessmentConfig.course_id == assessment.course_id
            ).first()
        else:
            config = configs_by_course.get(assessment.course_id)
        wpq = config.weightage_per_question if config else 1
        total_possible = assessment.total_possible or 0
        score_marks    = assessment.score or 0

        # total questions = total_possible / weightage_per_question
        total_qs = int(total_possible / wpq) if wpq > 0 else total_possible
        # correct = score_marks / weightage_per_question (for MCQ full marks)
        # For mixed, use percentage to estimate
        if total_qs > 0 and wpq > 0:
            correct_qs = round(score_marks / wpq)
        else:
            correct_qs = 0

        result["total"]         = total_qs
        result["correct_count"] = correct_qs

        # ── Parse the answers dict (question_id → answer) ──────────────────
        qdata = assessment.questions_data
        if isinstance(qdata, str):
            qdata = json.loads(qdata)
        if isinstance(qdata, str):
            qdata = json.loads(qdata)
        answers = qdata.get("answers", qdata) if isinstance(qdata, dict) else {}
        mcq_results = qdata.get("mcq_results", {}) if isinstance(qdata, dict) else {}

        # ── Load the unique question set shown to this trainee ─────────────
        if question_sets is None:
            tqs = _trainee_question_set_query(
                db,
                assessment.trainee_id,
                assessment.course_id,
                assessment.module_index,
            ).order_by(TraineeQuestionSet.created_at.asc()).first()
        else:
            tqs = question_sets.get(
                (assessment.trainee_id, assessment.course_id, assessment.module_index)
            )

        if tqs:
            unique_qs = json.loads(tqs.questions_json)
        else:
            ref_qs = db.query(AssessmentQuestion).filter(
                AssessmentQuestion.course_id == assessment.course_id,
                AssessmentQuestion.module_index == assessment.module_index
            ).all()
            unique_qs = [_question_to_dict(q) for q in ref_qs]

        for q in unique_qs:
            qid         = str(q['id'])
            qtype       = q.get('type', q.get('question_type', 'mcq'))
            qtext       = q.get('question', q.get('question_text', ''))[:120]
            correct_ans = str(q.get('correct_answer') or '').strip().upper()
            explanation = (q.get('explanation') or '')[:200]
            user_ans    = str(answers.get(qid, '')).strip().upper()

            if qtype == 'mcq':
                stored_flag = mcq_results.get(qid, None) if isinstance(mcq_results, dict) else None
                if isinstance(stored_flag, bool):
                    is_correct = stored_flag
                elif isinstance(stored_flag, str):
                    is_correct = stored_flag.strip().lower() == 'true'
                else:
                    user_sel = _normalize_mcq_selections(user_ans, q)
                    correct_sel = _normalize_mcq_selections(correct_ans, q)
                    is_correct = (user_sel == correct_sel and len(user_sel) > 0) if correct_sel else None
            else:
                is_correct = None  # subjective — scored by AI, not re-evaluated here

            entry = {
                "question":       qtext,
                "user_answer":    answers.get(qid, ''),
                "correct_answer": q.get('correct_answer', ''),
                "is_correct":     is_correct,
                "explanation":    explanation,
            }
            if is_correct is True:
                result["correct"].append(entry)
            elif is_correct is False:
                result["incorrect"].append(entry)

    except Exception as e:
        logger.warning('[PARSE ASSESSMENT] Failed: %s', e)
    return result


@router.get("/trainer/batches/{batch_id}/trainee/{trainee_id}/summary", tags=["Batch Management"])
def get_trainee_batch_summary(batch_id: str, trainee_id: str, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Get detailed AI-generated summary for a specific trainee in a batch, including wrong answer analysis."""
    batch = db.query(TraineeBatch).filter(TraineeBatch.id == batch_id, TraineeBatch.trainer_id == user.id, TraineeBatch.is_active == True).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    trainee = db.query(User).filter(User.id == trainee_id).first()
    if not trainee:
        raise HTTPException(status_code=404, detail="Trainee not found")

    batch_course_ids = [row[0] for row in db.query(TraineeAttempt.course_id).filter(TraineeAttempt.batch_id == batch_id).distinct().all()]
    voice_scenarios = list_scenarios_published_to_batch(db, batch_id, batch.process_id)
    voice_scenario_ids = [s["id"] for s in voice_scenarios]

    # Captured as a plain string, not read off the instance later: the pooled session is handed
    # back before the Nova Pro call below, and the fallback message needs this after that point.
    trainee_email = trainee.email

    lines = [f"Trainee: {trainee.email}", ""]
    all_wrong_questions = []

    # Courses, progress counts, assessments and configs for the whole batch up front: each of
    # these used to be a query per course inside the loop, and the totals below repeated them.
    courses_by_id = {}
    completed_by_course = {}
    assessments_by_course = defaultdict(list)
    configs_by_course = {}
    question_sets = {}
    if batch_course_ids:
        courses_by_id = {
            c.id: c
            for c in db.query(Course).filter(Course.id.in_(batch_course_ids)).all()
        }
        completed_by_course = dict(
            db.query(CourseProgress.course_id, func.count(CourseProgress.id))
            .filter(
                CourseProgress.user_id == trainee_id,
                CourseProgress.course_id.in_(batch_course_ids),
            )
            .group_by(CourseProgress.course_id)
            .all()
        )
        for row in (
            db.query(TraineeAssessment)
            .filter(
                TraineeAssessment.trainee_id == trainee_id,
                TraineeAssessment.course_id.in_(batch_course_ids),
            )
            .order_by(TraineeAssessment.completed_at.desc())
            .all()
        ):
            assessments_by_course[row.course_id].append(row)
        configs_by_course = {
            cfg.course_id: cfg
            for cfg in db.query(AssessmentConfig)
            .filter(AssessmentConfig.course_id.in_(batch_course_ids))
            .all()
        }
        question_sets = build_trainee_question_sets(db, [trainee_id], batch_course_ids)

    for cid in batch_course_ids:
        c = courses_by_id.get(cid)
        if not c:
            continue
        course_title = course_display_label(c)

        # Count modules. Read from the per-request metadata index rather than re-scanning S3:
        # this runs once per course in the batch, and the old scan made the count cost one full
        # S3 pass per course. Strict course_id matching only, exactly as before.
        total_mods = sum(
            1
            for _s3_key, meta_data in author_report_metadata(db, c.author_id)
            if meta_data.get('course_id') == c.id
        )

        completed = completed_by_course.get(cid, 0)

        # Latest assessment per module, taken from the newest-first rows fetched above.
        assessments = assessments_by_course.get(cid, [])

        latest = {}
        for a in assessments:
            if a.module_index not in latest:
                latest[a.module_index] = a

        lines.append(f"Course: '{course_title}'")
        lines.append(f"  Modules completed: {completed}/{total_mods}")
        assessment_config_obj = configs_by_course.get(cid)
        is_fully_done = _is_course_fully_completed(
            trainee_id, cid, total_mods, assessment_config_obj, db,
            completed_modules=completed, assessments=assessments,
        )
        modules_done = completed >= total_mods and total_mods > 0
        if is_fully_done:
            status_str = "Completed (modules + assessments)"
        elif modules_done:
            status_str = "Modules done — assessment pending"
        else:
            status_str = "In Progress"
        lines.append(f"  Status: {status_str}")

        for mod_idx, assessment in sorted(latest.items(), key=lambda x: (x[0] is None, x[0])):
            mod_label = f"Module {mod_idx}" if mod_idx is not None else "Final Assessment"
            q_breakdown = _parse_assessment_questions(
                assessment, db, configs_by_course=configs_by_course, question_sets=question_sets
            )
            lines.append(f"  {mod_label}: {assessment.percentage}% ({'PASS' if assessment.passed else 'FAIL'})")
            for wrong in q_breakdown["incorrect"][:3]:  # top 3 wrong per module
                lines.append(f"    WRONG concept area: question='{wrong['question']}', answered='{wrong['user_answer']}', correct='{wrong['correct_answer']}'")
                if wrong["explanation"]:
                    lines.append(f"    Context: {wrong['explanation'][:150]}")
                # Derive a short concept label from the question text (first 60 chars, cleaned)
                concept_hint = wrong["question"][:80].rstrip("?").strip()
                all_wrong_questions.append({
                    "course": course_title,
                    "module": mod_label,
                    "question": wrong["question"],
                    "concept": concept_hint,
                    "user_answer": wrong["user_answer"],
                    "correct_answer": wrong["correct_answer"],
                    "explanation": wrong.get("explanation", "")
                })
        lines.append("")

    voice_items = build_trainee_voice_progress(db, trainee_id, voice_scenarios)
    lines.extend(voice_summary_lines_for_trainee(trainee.email, voice_items, include_header=False))

    prompt = (
        "You are a training coach AI. Analyze this trainee's performance data carefully and write a detailed, personalized coaching report.\n"
        "Include BOTH course progress AND voice training call practice/assessment results when present.\n"
        "If there are no courses but voice scenarios exist, focus on voice training performance.\n"
        "Use markdown **bold** only for emphasis. Do NOT use HTML tags like <strong>, <b>, or <em>.\n"
        "Format your response using this EXACT markdown structure:\n\n"
        "**Performance Summary**\n"
        "2-3 sentences naming specific courses and/or voice scenarios completed with exact scores and current status.\n\n"
        "**Strengths**\n"
        "- Specific strength with course or voice scenario name, module/attempt, and score\n"
        "- Specific strength 2\n"
        "- Specific strength 3 (if applicable)\n\n"
        "**Areas to Improve**\n"
        "- **CONCEPT NAME**: explanation of why this concept matters and what the trainee should improve\n"
        "- **CONCEPT NAME**: explanation\n"
        "IMPORTANT: Use **CONCEPT NAME** in markdown bold, NOT HTML tags. Write concept names NOT question text.\n\n"
        "**Recommended Study Topics**\n"
        "- Topic 1 with specific resource, module, or voice scenario to revisit\n"
        "- Topic 2\n"
        "- Topic 3\n\n"
        "**Action Plan**\n"
        "1. Specific action tied to concept gap 1\n"
        "2. Specific action tied to concept gap 2\n"
        "3. Specific action tied to concept gap 3\n"
        "4. General study habit recommendation\n"
        "5. Next milestone to achieve\n\n"
        "CRITICAL RULES:\n"
        "- In 'Areas to Improve', use **CONCEPT NAME** in markdown, NEVER HTML tags or question text\n"
        "- Be specific: use course names, voice scenario titles, module numbers, exact scores\n"
        "- Infer concepts from: wrong answers, voice assessment gaps, or scenario performance\n\n"
        + "\n".join(lines)
    )

    # ── If trainee has no activity, skip AI and return factual summary ─────
    total_completed_mods = sum(completed_by_course.get(cid, 0) for cid in batch_course_ids)
    total_assessments_done = db.query(func.count(TraineeAssessment.id)).filter(
        TraineeAssessment.trainee_id == trainee_id,
        TraineeAssessment.course_id.in_(batch_course_ids)
    ).scalar() or 0
    has_voice_activity = trainee_has_voice_activity(db, trainee_id, voice_scenario_ids)

    if total_completed_mods == 0 and total_assessments_done == 0 and not has_voice_activity:
        course_names = [
            course_display_label(courses_by_id[cid])
            for cid in batch_course_ids
            if cid in courses_by_id
        ]
        voice_names = [s["title"] for s in voice_scenarios]
        if course_names and voice_names:
            content_str = f"courses ({', '.join(repr(n) for n in course_names)}) and voice scenarios ({', '.join(repr(n) for n in voice_names)})"
        elif voice_names:
            content_str = f"voice scenarios ({', '.join(repr(n) for n in voice_names)})"
        else:
            content_str = f"courses ({', '.join(repr(n) for n in course_names)})" if course_names else "assigned content"
        summary = f"""**Performance Summary**

{trainee.email} has not yet started any modules or voice practice in {content_str}. No progress or assessment data is available at this time.

**Strengths**

- No activity recorded yet — strengths will be identified once the trainee begins training.

**Areas to Improve**

- No assessment data is available. Areas to improve will be identified after the trainee completes practice or assessments.

**Recommended Study Topics**

- Begin with the first assigned module or voice practice scenario.
- Follow the training sequence as designed by the trainer.

**Trainer / Admin Action Plan**

1. Follow up with this trainee to understand why training has not been started.
2. Send a reminder or nudge to encourage the trainee to begin.
3. Verify the trainee has correct access and can log in to the platform.
4. Check if any technical or scheduling barriers are preventing the trainee from starting.
5. Set a check-in date to review progress after the trainee begins."""
        return {"success": True, "summary": summary, "wrong_questions": [], "voice_scenarios": voice_items}

    # Every query this endpoint makes is done by here, and the Nova Pro call below can take tens of
    # seconds. Depends(get_db) finalizes only after the response has been sent, so without this the
    # connection stays checked out for the whole generation while doing no database work - the same
    # shape that exhausted the pool for the streaming chat endpoints. Closing here is safe to do
    # twice: get_db's finally block closes again, and Session.close() is idempotent. Everything
    # used after this point is a plain value (trainee_email, dicts), never a lazy ORM attribute.
    db.close()

    try:
        response = sync_bedrock_client.invoke_model(
            modelId=BEDROCK_GENERATION_MODEL,
            body=json.dumps({
                "messages": [{"role": "user", "content": [{"text": prompt}]}],
                "inferenceConfig": {"maxTokens": 4096, "temperature": 0.3, "topP": 0.9},
            }),
            contentType="application/json",
            accept="application/json",
        )
        response_body = json.loads(response["body"].read())
        content_blocks = (
            response_body.get("output", {}).get("message", {}).get("content") or []
        )
        summary = ""
        if content_blocks and isinstance(content_blocks[0], dict):
            summary = str(content_blocks[0].get("text") or "").strip()
        if not summary:
            raise ValueError("Empty Nova Pro summary response")
    except Exception as e:
        logger.warning('[TRAINEE SUMMARY] AI failed: %s', log_safe(e))
        summary = f"{trainee_email} has completed modules across {len(batch_course_ids)} course(s) in this batch."

    return {
        "success": True,
        "summary": summary,
        "wrong_questions": all_wrong_questions[:10],  # top 10 for frontend display
        "voice_scenarios": voice_items,
    }


@router.get("/trainer/batches/{batch_id}/analytics", tags=["Batch Management"])
def get_batch_analytics(batch_id: str, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Get rich analytics for a batch: per-trainee progress, assessment scores, AI summary."""
    batch = db.query(TraineeBatch).filter(
        TraineeBatch.id == batch_id,
        TraineeBatch.trainer_id == user.id,
        TraineeBatch.is_active == True
    ).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    members = db.query(BatchMember, User).join(User, BatchMember.trainee_id == User.id).filter(BatchMember.batch_id == batch_id).all()

    # Courses published via this batch
    batch_course_ids = [row[0] for row in db.query(TraineeAttempt.course_id).filter(TraineeAttempt.batch_id == batch_id).distinct().all()]
    courses_by_id = {}
    if batch_course_ids:
        courses_by_id = {
            c.id: c
            for c in db.query(Course).filter(Course.id.in_(batch_course_ids)).all()
        }
    # A batch can carry courses from more than one author, so the prefetch covers every author
    # represented here — one S3 pass each, rather than one index filter per course.
    prefetch_s3_module_counts(
        db, {c.author_id for c in courses_by_id.values() if c.author_id}
    )
    batch_courses = []
    for cid in batch_course_ids:
        c = courses_by_id.get(cid)
        if c and c.is_live:
            total_mods = _count_modules_for_course(c.id, db)
            batch_courses.append({"id": c.id, "title": course_display_label(c), "total_modules": total_mods})

    # Progress counts, attempts and assessment configs for every trainee/course pair in the
    # batch, fetched once. These used to be four queries per pair inside the nested loop below,
    # which is what made a large batch slow enough for the gateway to time the request out.
    live_course_ids = [course_info["id"] for course_info in batch_courses]
    member_ids = [trainee.id for _bm, trainee in members]
    progress_counts = {}
    assessments_by_pair = defaultdict(list)
    configs_by_course = {}
    question_sets = {}
    if live_course_ids and member_ids:
        progress_counts = {
            (row[0], row[1]): row[2]
            for row in db.query(
                CourseProgress.user_id, CourseProgress.course_id, func.count(CourseProgress.id)
            ).filter(
                CourseProgress.user_id.in_(member_ids),
                CourseProgress.course_id.in_(live_course_ids),
            ).group_by(CourseProgress.user_id, CourseProgress.course_id).all()
        }
        for row in (
            db.query(TraineeAssessment)
            .filter(
                TraineeAssessment.trainee_id.in_(member_ids),
                TraineeAssessment.course_id.in_(live_course_ids),
            )
            .order_by(TraineeAssessment.completed_at.desc())
            .all()
        ):
            assessments_by_pair[(row.trainee_id, row.course_id)].append(row)
        configs_by_course = {
            cfg.course_id: cfg
            for cfg in db.query(AssessmentConfig)
            .filter(AssessmentConfig.course_id.in_(live_course_ids))
            .all()
        }
        question_sets = build_trainee_question_sets(db, member_ids, live_course_ids)

    # Per-trainee analytics
    trainees_analytics = []
    batch_total_modules_completed = 0
    batch_total_modules_possible = 0
    batch_assessment_scores = []
    batch_completed_count = 0

    for bm, trainee in members:
        trainee_data = {
            "id": trainee.id,
            "email": trainee.email,
            "courses": []
        }
        trainee_total_completed = 0
        trainee_total_modules = 0
        trainee_scores = []
        trainee_fully_completed = 0

        for course_info in batch_courses:
            cid = course_info["id"]
            total_mods = course_info["total_modules"]
            if cid not in courses_by_id:
                continue

            completed_mods = progress_counts.get((trainee.id, cid), 0)

            # Assessment scores for this course — with per-question breakdown
            assessments = assessments_by_pair.get((trainee.id, cid), [])

            # Latest attempt per module_index
            latest_by_module = {}
            for a in assessments:
                key = a.module_index
                if key not in latest_by_module:
                    latest_by_module[key] = a

            course_scores = []
            for k, v in latest_by_module.items():
                q_breakdown = _parse_assessment_questions(
                    v, db, configs_by_course=configs_by_course, question_sets=question_sets
                )
                course_scores.append({
                    "module_index": k,
                    "score": v.percentage,
                    "passed": v.passed,
                    "attempt": v.attempt_number,
                    "correct_count": q_breakdown["correct_count"],
                    "total_questions": q_breakdown["total"],
                    "wrong_questions": [{"question": w["question"][:80], "correct_answer": w["correct_answer"]} for w in q_breakdown["incorrect"][:3]]
                })
            avg_score = round(sum(s["score"] for s in course_scores) / len(course_scores), 1) if course_scores else None

            progress_pct = round(completed_mods / total_mods * 100, 1) if total_mods > 0 else 0
            # CRITICAL FIX: Use proper completion check — modules done AND assessments passed
            assessment_config = configs_by_course.get(cid)
            is_completed = _is_course_fully_completed(
                trainee.id, cid, total_mods, assessment_config, db,
                completed_modules=completed_mods, assessments=assessments,
            )
            # Assessment pending flag — modules done but assessment not yet passed
            modules_done = completed_mods >= total_mods and total_mods > 0
            assessment_pending = modules_done and not is_completed

            trainee_data["courses"].append({
                "id": cid,
                "title": course_info["title"],
                "total_modules": total_mods,
                "completed_modules": completed_mods,
                "progress_pct": progress_pct,
                "is_completed": is_completed,
                "assessment_pending": assessment_pending,
                "avg_score": avg_score,
                "assessment_details": course_scores
            })

            trainee_total_completed += completed_mods
            trainee_total_modules += total_mods
            if avg_score is not None:
                trainee_scores.append(avg_score)
            if is_completed:
                trainee_fully_completed += 1

        overall_progress = round(trainee_total_completed / trainee_total_modules * 100, 1) if trainee_total_modules > 0 else 0
        overall_avg_score = round(sum(trainee_scores) / len(trainee_scores), 1) if trainee_scores else None

        trainee_data["overall_progress"] = overall_progress
        trainee_data["overall_avg_score"] = overall_avg_score
        trainee_data["completed_courses"] = trainee_fully_completed
        trainee_data["total_courses"] = len(batch_courses)
        trainee_data["total_completed_modules"] = trainee_total_completed
        trainee_data["total_modules"] = trainee_total_modules

        trainees_analytics.append(trainee_data)
        batch_total_modules_completed += trainee_total_completed
        batch_total_modules_possible += trainee_total_modules
        if overall_avg_score is not None:
            batch_assessment_scores.append(overall_avg_score)
        if trainee_fully_completed == len(batch_courses) and len(batch_courses) > 0:
            batch_completed_count += 1

    # Voice training published to this batch
    voice_scenarios = list_scenarios_published_to_batch(db, batch_id, batch.process_id)

    # Batch-level summary stats
    batch_avg_progress = round(batch_total_modules_completed / batch_total_modules_possible * 100, 1) if batch_total_modules_possible > 0 else 0
    batch_avg_score = round(sum(batch_assessment_scores) / len(batch_assessment_scores), 1) if batch_assessment_scores else None

    preliminary = {
        "success": True,
        "batch_name": batch.batch_name,
        "batch_id": batch_id,
        "courses": batch_courses,
        "trainees": trainees_analytics,
        "summary": {
            "total_trainees": len(members),
            "total_courses": len(batch_courses),
            "avg_progress": batch_avg_progress,
            "avg_score": batch_avg_score,
            "fully_completed_trainees": batch_completed_count,
            "ai_summary": None,
        },
    }
    enrich_batch_analytics_with_voice(db, batch_id, batch.process_id, preliminary)
    s = preliminary["summary"]

    # Last query of the endpoint is above. The Nova Pro call below can take tens of seconds, and
    # Depends(get_db) finalizes only after the response has been sent, so holding on here keeps a
    # pooled connection checked out for the whole generation while doing no database work. Read the
    # one ORM attribute the prompt and the fallback both need first, then hand the connection back;
    # a second close from get_db's finally block is a no-op.
    batch_name = batch.batch_name
    db.close()

    # AI summary using Bedrock — detailed with question-level insights
    ai_summary = None
    try:
        summary_lines = [
            f"Batch: {batch_name}",
            f"Total Trainees: {len(members)}, Courses: {len(batch_courses)}, Voice Scenarios: {s.get('total_voice_scenarios', 0)}",
            f"Avg Course Progress: {batch_avg_progress}%, Avg Course Assessment Score: {batch_avg_score if batch_avg_score is not None else 'N/A'}%",
            f"Avg Voice Assessment Score: {s.get('avg_voice_score') if s.get('avg_voice_score') is not None else 'N/A'}%",
            f"Fully Completed (courses): {batch_completed_count}/{len(members)} trainees",
            f"Voice Passed (trainee-scenario pairs): {s.get('voice_passed_count', 0)}",
            ""
        ]
        for t in preliminary["trainees"]:
            summary_lines.append(f"Trainee: {t['email']}")
            summary_lines.append(
                f"  Overall: {t['overall_progress']}% course progress, "
                f"{t['overall_avg_score'] if t['overall_avg_score'] is not None else 'N/A'}% avg course score, "
                f"{t['completed_courses']}/{t['total_courses']} courses done"
            )
            for c in t["courses"]:
                c_status = "Completed" if c['is_completed'] else ("Assessment Pending" if c.get('assessment_pending') else ("Not Started" if c['completed_modules'] == 0 else "In Progress"))
                summary_lines.append(f"  Course '{c['title']}': {c['completed_modules']}/{c['total_modules']} modules, score={c['avg_score']}%, {c_status}")
                for a in c.get("assessment_details", []):
                    mod_label = f"Module {a['module_index']}" if a['module_index'] is not None else "Final"
                    wrong_str = "; ".join([f"\"{w['question'][:60]}\"" for w in a.get("wrong_questions", [])]) or "none"
                    summary_lines.append(f"    {mod_label}: {a['score']}% ({'PASS' if a['passed'] else 'FAIL'}), wrong: {wrong_str}")
            summary_lines.extend(voice_summary_lines_for_trainee(t["email"], t.get("voice_scenarios", []), include_header=False))

        prompt = (
            "You are a training analytics AI. Analyze this batch training data and write a structured report using markdown.\n"
            "Include BOTH course progress AND voice training call practice/assessment results when present.\n"
            "If there are no courses but voice scenarios exist, focus the report on voice training performance.\n"
            "Use markdown **bold** only. Do NOT use HTML tags like <strong> or <b>.\n"
            "Format your response EXACTLY like this:\n\n"
            "**Batch Performance Overview**\n"
            "2-3 sentences with specific numbers on course progress, course scores, AND voice assessment scores.\n\n"
            "**Voice Training Results**\n"
            "- List each voice scenario with trainee practice completion, assessment scores, pass/fail status\n"
            "- If no voice data, write 'No voice training activity yet.'\n\n"
            "**Top Performers**\n"
            "- Name trainees excelling with course scores and/or voice assessment scores\n\n"
            "**Trainees Needing Support**\n"
            "- Name struggling trainees with specific course or voice training issues\n\n"
            "**Common Weak Areas**\n"
            "- CONCEPT NAME: which trainees struggled and why it matters\n"
            "- CONCEPT NAME: explanation\n"
            "IMPORTANT: Write CONCEPT NAMES (e.g. 'Empathy and Active Listening') NOT question text\n\n"
            "**Recommended Focus Areas**\n"
            "- Topic 1 that the batch should revisit (courses or voice scenarios)\n"
            "- Topic 2\n"
            "- Topic 3\n\n"
            "**Recommendations**\n"
            "1. Specific action for the trainer to take\n"
            "2. Specific action\n"
            "3. Specific action\n"
            "4. Long-term improvement suggestion\n"
            "5. Next milestone for the batch\n\n"
            "CRITICAL: In 'Common Weak Areas', ALWAYS write concept names, NEVER quote question text. "
            "Use specific trainee names, course titles, voice scenario titles, and scores throughout.\n\n"
            + "\n".join(summary_lines)
        )
        response = sync_bedrock_client.invoke_model(
            modelId=BEDROCK_GENERATION_MODEL,
            body=json.dumps({
                "messages": [{"role": "user", "content": [{"text": prompt}]}],
                "inferenceConfig": {"maxTokens": 4096, "temperature": 0.3, "topP": 0.9},
            }),
            contentType="application/json",
            accept="application/json",
        )
        response_body = json.loads(response["body"].read())
        content_blocks = (
            response_body.get("output", {}).get("message", {}).get("content") or []
        )
        ai_summary = ""
        if content_blocks and isinstance(content_blocks[0], dict):
            ai_summary = str(content_blocks[0].get("text") or "").strip()
        if not ai_summary:
            raise ValueError("Empty Nova Pro batch summary response")
    except Exception as e:
        logger.warning('[BATCH ANALYTICS] AI summary failed: %s', log_safe(e))
        voice_note = ""
        if s.get("total_voice_scenarios"):
            voice_note = (
                f" Voice scenarios: {s['total_voice_scenarios']}, "
                f"avg voice score {s.get('avg_voice_score') if s.get('avg_voice_score') is not None else 'N/A'}%, "
                f"passed {s.get('voice_passed_count', 0)}."
            )
        ai_summary = (
            f"**Batch Performance Overview**\n"
            f"Batch '{batch_name}' has {len(members)} trainee(s) with {batch_avg_progress}% average course progress "
            f"and {batch_avg_score if batch_avg_score is not None else 'N/A'}% average course score across {len(batch_courses)} course(s).{voice_note}\n\n"
            f"**Voice Training Results**\n"
        )
        if s.get("total_voice_scenarios"):
            for t in preliminary["trainees"]:
                for v in t.get("voice_scenarios", []):
                    score = v.get("best_score")
                    score_txt = f"{score}%" if score is not None else "N/A"
                    status = "Passed" if v.get("has_passed") else v.get("status", "not started")
                    ai_summary += f"- {t['email']}: '{v.get('title')}' — practice {v.get('practice_completed')}/{v.get('practice_required')}, best score {score_txt}, {status}\n"
        else:
            ai_summary += "- No voice training scenarios published to this batch.\n"

    preliminary["summary"]["ai_summary"] = ai_summary
    return preliminary


@router.post("/trainer/publish-course-enhanced", tags=["Batch Management"])
def publish_course_enhanced(
    req: EnhancedPublishCourseRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db),
):
    """
    Publish a course to either all trainees in process, a single batch, or multiple batches.
    
    ENHANCED: Now supports multi-batch publishing.
    OPTIMIZED: Trainers can only publish to batches unless super admin enables global sharing.
    Tutor pre-generation runs in the background to avoid gateway 504 timeouts.
    """
    if not user.process_id:
        raise HTTPException(status_code=400, detail="Trainer must be assigned to a process")
    
    try:
        # OPTIMIZATION: Check if global sharing is enabled by super admin
        global_sharing_enabled = get_system_setting(db, "allow_global_sharing", "false") == "true"
        
        # Track batch_ids for later use
        batch_ids_used = []
        trainee_dict = {}
        
        # Get target trainees based on publication type
        if req.publication_type == "all_process":
            # Only allow if super admin has enabled global sharing
            if not global_sharing_enabled:
                raise HTTPException(
                    status_code=403, 
                    detail="Publishing to all trainees is disabled. Please publish to a specific batch. Contact your administrator to enable global sharing."
                )
            
            trainees = db.query(User).filter(
                User.role == "trainee",
                User.process_id == user.process_id
            ).all()
            logger.info("[PUBLISH] Publishing to all trainees in process (global sharing enabled)")
            
        elif req.publication_type == "batch":
            # Single batch (legacy support)
            if not req.batch_id:
                raise HTTPException(status_code=400, detail="Batch ID is required for batch publication")
            
            batch = db.query(TraineeBatch).filter(
                TraineeBatch.id == req.batch_id,
                TraineeBatch.trainer_id == user.id,
                TraineeBatch.is_active == True
            ).first()
            if not batch:
                raise HTTPException(status_code=404, detail="Batch not found")
            
            batch_members = db.query(BatchMember, User).join(
                User, BatchMember.trainee_id == User.id
            ).filter(BatchMember.batch_id == req.batch_id).all()
            trainees = [m.User for m in batch_members]
            batch_ids_used = [req.batch_id]
            
        elif req.publication_type == "multi_batch":
            # NEW: Multiple batches
            if not req.batch_ids or len(req.batch_ids) == 0:
                raise HTTPException(status_code=400, detail="At least one batch ID is required for multi-batch publication")
            
            # Verify all batches belong to this trainer
            batches = db.query(TraineeBatch).filter(
                TraineeBatch.id.in_(req.batch_ids),
                TraineeBatch.trainer_id == user.id,
                TraineeBatch.is_active == True
            ).all()
            
            if len(batches) != len(req.batch_ids):
                raise HTTPException(status_code=404, detail="One or more batches not found or not owned by you")
            
            # Members of every selected batch in one query. The loop below still walks
            # `req.batch_ids` in order, so `trainee_dict` and each trainee's `batch_ids` list
            # are built exactly as they were when each batch had its own query.
            members_by_batch = defaultdict(list)
            for member_row, member_user in db.query(BatchMember, User).join(
                User, BatchMember.trainee_id == User.id
            ).filter(BatchMember.batch_id.in_(req.batch_ids)).all():
                members_by_batch[member_row.batch_id].append((member_row, member_user))

            # Get all unique trainees from all batches
            for batch_id in req.batch_ids:
                batch_members = members_by_batch.get(batch_id, [])

                for member, trainee_user in batch_members:
                    if trainee_user.id not in trainee_dict:
                        trainee_dict[trainee_user.id] = {
                            'user': trainee_user,
                            'batch_ids': []
                        }
                    trainee_dict[trainee_user.id]['batch_ids'].append(batch_id)
            
            trainees = [data['user'] for data in trainee_dict.values()]
            batch_ids_used = req.batch_ids
            logger.info('[PUBLISH] Publishing to %s batches with %s unique trainees', len(req.batch_ids), len(trainees))
            
        else:
            raise HTTPException(status_code=400, detail="Invalid publication type. Use 'all_process', 'batch', or 'multi_batch'")
        
        if not trainees:
            raise HTTPException(status_code=400, detail="No trainees found for publication")
        
        # ASSESSMENT VALIDATION: Check if assessment is configured and approved
        # For multi-document courses, look up by course_id if provided, otherwise by source_doc_name
        course_check = None
        
        # Try to find course by source_doc_name first (for backward compatibility)
        course_check = db.query(Course).filter(
            Course.author_id == user.id,
            Course.source_doc_name == req.source_doc_name
        ).first()
        
        # If not found and source_doc_name looks like a course title, try to find by title
        if not course_check:
            course_check = db.query(Course).filter(
                Course.author_id == user.id,
                Course.course_title == req.source_doc_name
            ).first()
            logger.info(
                "[PUBLISH] Course not found by source_doc_name, trying by course_title: %s",
                ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
            )
        
        if course_check:
            logger.info('[PUBLISH] Found course: %s, source_doc_name: %s, course_title: %s', ("" if course_check.id is None else str(course_check.id).replace("\n", "").replace("\r", "")), ("" if course_check.source_doc_name is None else str(course_check.source_doc_name).replace("\n", "").replace("\r", "")), ("" if course_check.course_title is None else str(course_check.course_title).replace("\n", "").replace("\r", "")))
            
            # Check if assessment is configured
            assessment_config = db.query(AssessmentConfig).filter(
                AssessmentConfig.course_id == course_check.id
            ).first()
            
            if not assessment_config:
                raise HTTPException(
                    status_code=400, 
                    detail="Assessment must be configured before publishing. Please configure assessment settings first."
                )
            
            if not assessment_config.is_approved:
                # Check if questions are generated and approved
                approved_questions = db.query(AssessmentQuestion).filter(
                    AssessmentQuestion.course_id == course_check.id,
                    AssessmentQuestion.is_approved == True
                ).count()
                
                total_questions = db.query(AssessmentQuestion).filter(
                    AssessmentQuestion.course_id == course_check.id
                ).count()
                
                logger.info('[PUBLISH] Assessment check - Total questions: %s, Approved: %s', ("" if total_questions is None else str(total_questions).replace("\n", "").replace("\r", "")), ("" if approved_questions is None else str(approved_questions).replace("\n", "").replace("\r", "")))
                
                if approved_questions == 0:
                    raise HTTPException(
                        status_code=400,
                        detail="Assessment questions must be generated and approved before publishing. Please review and approve questions first."
                    )
                
                # Auto-approve config if all questions are approved
                if approved_questions == total_questions:
                    assessment_config.is_approved = True
                    logger.info('[PUBLISH] Auto-approved assessment config (all %s questions approved)', ("" if total_questions is None else str(total_questions).replace("\n", "").replace("\r", "")))
        else:
            logger.error(
                "[PUBLISH] Course not found by source_doc_name: %s",
                ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
            )
            raise HTTPException(
                status_code=400,
                detail="Course not found. Please generate modules first, then configure assessment before publishing."
            )
        
        # Create or update course
        # CRITICAL FIX: Use max_attempts from assessment config, not hardcoded 1
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course_check.id
        ).first()
        
        initial_attempts = assessment_config.max_attempts if assessment_config else 1
        logger.info('[PUBLISH] Setting initial attempts to %s (from assessment config)', ("" if initial_attempts is None else str(initial_attempts).replace("\n", "").replace("\r", "")))
        
        # Use the course_check we already found
        course = course_check

        # req.source_doc_name is the randomized storage name (VAPT #4); the publish confirmation
        # is read by the trainer, so it quotes the course title / uploaded filename.
        doc_label = course_display_label(course)

        # Prefer DB module count (fast) over full S3 metadata walk during publish
        total_modules = _count_modules_for_course(course.id, db) or 0
        if total_modules == 0:
            try:
                logger.info(
                    "Counting modules for course %s in S3...",
                    ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
                )
                paginator = s3_client.get_paginator('list_objects_v2')
                pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=f"{user.id}/generated_reports/")
                for page in pages:
                    for obj in page.get('Contents', []):
                        if not obj['Key'].endswith('.json'):
                            continue
                        try:
                            meta_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
                            meta_data = json.loads(meta_obj['Body'].read().decode('utf-8'))
                            source_doc = meta_data.get('source_doc', '')
                            if source_doc == req.source_doc_name:
                                total_modules += 1
                        except Exception as e:
                            logger.error(
                                'Error reading module metadata %s: %s',
                                log_safe(obj.get('Key')),
                                log_safe(e),
                            )
                logger.info(
                    "Found %s modules for %s",
                    total_modules,
                    ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
                )
            except Exception as e:
                logger.error('Error counting modules: %s', log_safe(e), exc_info=True)
        
        if course:
            course.is_live = True
            course.initial_attempts = initial_attempts
            message = f"Course '{doc_label}' updated and re-published."
        else:
            course = Course(
                author_id=user.id,
                source_doc_name=req.source_doc_name,
                process_id=user.process_id,
                initial_attempts=initial_attempts,
                is_live=True
            )
            db.add(course)
            message = f"Course '{doc_label}' published successfully."
        
        db.flush()
        
        from services.token_tracking import persist_course_generation_tokens

        gen_totals = persist_course_generation_tokens(
            db,
            user.id,
            course.id,
            req.source_doc_name,
            total_modules,
        )
        logger.info(
            "Tracked module generation for %s: %s text + %s vision = %s tokens",
            log_safe(req.source_doc_name),
            gen_totals["module_text"],
            gen_totals["module_vision"],
            gen_totals["total"],
        )

        schedule_tutor_pre_gen = total_modules > 0
        if not schedule_tutor_pre_gen:
            logger.warning(
                "No modules to pre-generate for course %s",
                ("" if req.source_doc_name is None else str(req.source_doc_name).replace("\n", "").replace("\r", "")),
            )
        
        # Resolve display name BEFORE the trainee loop (used in notifications)
        course_display_name_enh = course_display_label(course)

        # Existing attempt rows for every target trainee, in one query. Publishing to a large
        # batch previously cost one lookup per trainee before the first row was written, which is
        # what pushed a wide publish towards the gateway timeout. `setdefault` keeps the first row
        # returned, matching the `.first()` this replaces.
        existing_attempt_by_trainee = {}
        target_trainee_ids = [trainee.id for trainee in trainees]
        if target_trainee_ids:
            for attempt_row in db.query(TraineeAttempt).filter(
                TraineeAttempt.trainee_id.in_(target_trainee_ids),
                TraineeAttempt.course_id == course.id,
            ).all():
                existing_attempt_by_trainee.setdefault(attempt_row.trainee_id, attempt_row)

        # Assign to trainees
        for trainee in trainees:
            attempt = existing_attempt_by_trainee.get(trainee.id)

            # Determine batch_id to store
            batch_id_to_store = None
            if req.publication_type == "batch" and req.batch_id:
                batch_id_to_store = req.batch_id
            elif req.publication_type == "multi_batch" and batch_ids_used:
                # For multi-batch, store the first batch this trainee belongs to
                if trainee.id in trainee_dict:
                    batch_id_to_store = trainee_dict[trainee.id]['batch_ids'][0] if trainee_dict[trainee.id]['batch_ids'] else None
            
            if attempt:
                attempt.attempts_left = initial_attempts
                # Update batch_id
                if batch_id_to_store:
                    attempt.batch_id = batch_id_to_store
            else:
                new_attempt = TraineeAttempt(
                    trainee_id=trainee.id,
                    course_id=course.id,
                    attempts_left=initial_attempts,
                    batch_id=batch_id_to_store
                )
                db.add(new_attempt)
                # A trainee listed twice (duplicate batch membership) must not get a second row;
                # the per-trainee query this replaced saw the pending insert through autoflush.
                existing_attempt_by_trainee[trainee.id] = new_attempt

            safe_course_notify = (course_display_name_enh or "a course").replace("\r", " ").replace("\n", " ")
            db.add(Notification(
                user_id=trainee.id,
                message=f"📚 New course assigned: '{safe_course_notify}' — {total_modules} module(s) ready for you. Start learning now!",
                related_link="/trainee-dashboard",
                course_id=course_notification_id(course)
            ))
        
        # Notify the trainer (course owner) with full publish summary
        pub_type_label = {"all_process": "all trainees", "batch": "a batch", "multi_batch": "multiple batches"}.get(req.publication_type, req.publication_type)
        batch_label = ""
        if req.publication_type == "batch" and req.batch_id:
            batch_obj = db.query(TraineeBatch).filter(TraineeBatch.id == req.batch_id).first()
            if batch_obj:
                batch_label = f" (Batch: {batch_obj.batch_name})"
        elif req.publication_type == "multi_batch" and batch_ids_used:
            batch_objs = db.query(TraineeBatch).filter(TraineeBatch.id.in_(batch_ids_used)).all()
            batch_names = ", ".join(b.batch_name for b in batch_objs)
            batch_label = f" (Batches: {batch_names})"
        safe_course_trainer = (course_display_name_enh or "your course").replace("\r", " ").replace("\n", " ")
        trainer_notification_msg = (
            f"✅ Course '{safe_course_trainer}' published to {pub_type_label}{batch_label} — "
            f"{total_modules} module(s) ready, {len(trainees)} trainee(s) enrolled. "
            f"Tutor responses are being prepared in the background."
        )
        db.add(Notification(user_id=user.id, message=trainer_notification_msg, related_link="/trainer-dashboard", course_id=course_notification_id(course)))

        log_activity(db, user.id, "PUBLISH_COURSE_ENHANCED",
                    f"Published '{doc_label}' to {req.publication_type} ({len(trainees)} trainees)")
        db.commit()

        if schedule_tutor_pre_gen:
            background_tasks.add_task(
                _run_tutor_pre_generation_background,
                course.author_id,
                course.id,
                req.source_doc_name,
            )
        
        return {
            "success": True,
            "message": (
                f"{message} Published to {len(trainees)} trainees via {req.publication_type}. "
                "Tutor responses are being prepared in the background."
            ),
            "trainee_count": len(trainees),
            "module_count": total_modules,
            "publication_type": req.publication_type
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error('Error publishing course: %s', log_safe(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to publish course")

@router.post("/trainer/add-trainees-to-course", tags=["Batch Management"])
def add_trainees_to_course(req: AddTraineeToCourseRequest, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Add individual trainees to an already published course.
    Useful when a trainer forgets to add someone to a batch before publishing.
    Works for both trainers and super admins.
    """
    try:
        # Get the course
        course = db.query(Course).filter(Course.id == req.course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Authorization check
        if user.role == 'trainer' and course.author_id != user.id:
            raise HTTPException(status_code=403, detail="You can only add trainees to your own courses")
        elif user.role not in ['trainer', 'super_admin']:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Check if course is published
        if not course.is_live:
            raise HTTPException(status_code=400, detail="Course must be published before adding trainees")
        
        # Get assessment config for attempts
        assessment_config = db.query(AssessmentConfig).filter(
            AssessmentConfig.course_id == course.id
        ).first()
        initial_attempts = assessment_config.max_attempts if assessment_config else 1
        
        # Verify trainees exist and get their details
        trainees = db.query(User).filter(
            User.id.in_(req.trainee_ids),
            User.role == "trainee"
        ).all()
        
        if len(trainees) != len(req.trainee_ids):
            raise HTTPException(status_code=404, detail="One or more trainee IDs not found or not trainees")
        
        added_count = 0
        already_assigned_count = 0
        
        # Which of the selected trainees already have access, in one query. Adding a long
        # selection previously cost one round-trip per trainee before anything was written.
        already_assigned_ids = set()
        selected_trainee_ids = [trainee.id for trainee in trainees]
        if selected_trainee_ids:
            already_assigned_ids = {
                row[0]
                for row in db.query(TraineeAttempt.trainee_id).filter(
                    TraineeAttempt.trainee_id.in_(selected_trainee_ids),
                    TraineeAttempt.course_id == course.id,
                ).all()
            }

        for trainee in trainees:
            # Check if trainee already has access
            if trainee.id in already_assigned_ids:
                already_assigned_count += 1
                continue

            # Add new TraineeAttempt
            # Note: batch_id will be NULL since we're adding manually
            db.add(TraineeAttempt(
                trainee_id=trainee.id,
                course_id=course.id,
                attempts_left=initial_attempts,
                batch_id=None  # Manual addition, not through batch
            ))
            
            # Send notification to trainee
            db.add(Notification(
                user_id=trainee.id,
                message=f"You have been assigned to course '{course_display_label(course)}' by {user.email}.",
                related_link="/trainee-dashboard",
                course_id=course_notification_id(course)
            ))
            
            added_count += 1
        
        # If super admin added trainees, notify the course owner (trainer)
        if user.role == 'super_admin' and added_count > 0 and course.author_id != user.id:
            db.add(Notification(
                user_id=course.author_id,
                message=f"Super admin {user.email} added {added_count} trainee(s) to your course '{course_display_label(course)}'.",
                related_link="/trainer-dashboard",
                course_id=course_notification_id(course)
            ))
        
        log_activity(db, user.id, "ADD_TRAINEES_TO_COURSE", 
                    f"Added {added_count} trainees to course '{course.source_doc_name}'")
        db.commit()
        
        message = f"Successfully added {added_count} trainee(s) to the course."
        if already_assigned_count > 0:
            message += f" {already_assigned_count} trainee(s) were already assigned."
        
        return {
            "success": True,
            "message": message,
            "added_count": added_count,
            "already_assigned_count": already_assigned_count
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error('Error adding trainees to course: %s', e)
        raise HTTPException(status_code=500, detail="Failed to add trainees to course")

@router.get("/trainer/trainees", tags=["Batch Management"])
def get_trainer_trainees(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Get all trainees in the trainer's process."""
    if not user.process_id:
        raise HTTPException(status_code=400, detail="Trainer must be assigned to a process")
    
    trainees = db.query(User).filter(
        User.role == "trainee",
        User.process_id == user.process_id
    ).all()
    
    return {
        "success": True,
        "trainees": [
            {
                "id": t.id,
                "email": t.email
            }
            for t in trainees
        ]
    }

@router.get("/trainer/available-trainees/{course_id}", tags=["Batch Management"])
def get_available_trainees_for_course(
    course_id: str,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Get trainees who are NOT already assigned to the specified course.
    Works for both trainers and super admins.
    """
    try:
        # Get the course
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        # Authorization check
        if user.role == 'trainer':
            if not user.process_id:
                raise HTTPException(status_code=400, detail="Trainer must be assigned to a process")
            if course.author_id != user.id:
                raise HTTPException(status_code=403, detail="You can only view trainees for your own courses")
        elif user.role != 'super_admin':
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Get all trainees in the process
        if user.role == 'trainer':
            all_trainees = db.query(User).filter(
                User.role == "trainee",
                User.process_id == user.process_id
            ).all()
        else:  # super_admin
            # For super admin, get trainees from the course author's process
            course_author = db.query(User).filter(User.id == course.author_id).first()
            if not course_author or not course_author.process_id:
                return {"success": True, "data": []}
            
            all_trainees = db.query(User).filter(
                User.role == "trainee",
                User.process_id == course_author.process_id
            ).all()
        
        logger.info('Total trainees in process: %s', len(all_trainees))
        
        # Get trainees already assigned to this course
        assigned_trainee_ids = set(
            db.query(TraineeAttempt.trainee_id)
            .filter(TraineeAttempt.course_id == course_id)
            .all()
        )
        assigned_trainee_ids = {tid[0] for tid in assigned_trainee_ids}
        
        logger.info('Already assigned trainee IDs: %s', ("" if assigned_trainee_ids is None else str(assigned_trainee_ids).replace("\n", "").replace("\r", "")))
        
        # Filter out assigned trainees
        available_trainees = [
            {"id": t.id, "email": t.email}
            for t in all_trainees
            if t.id not in assigned_trainee_ids
        ]
        
        logger.info('Available trainees to add: %s', len(available_trainees))
        
        return {
            "success": True,
            "data": available_trainees
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error getting available trainees: %s', e)
        raise HTTPException(status_code=500, detail="Failed to get available trainees")

@router.get("/course/{course_id}/batches", tags=["Batch Management"])
def get_course_batches(course_id: str, user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Get all batches and manually added trainees for a published course.
    Returns both batch-published trainees and individually added trainees.
    """
    try:
        course = db.query(Course).filter(Course.id == course_id).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
        
        if user.role == 'trainer' and course.author_id != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        elif user.role not in ['trainer', 'super_admin']:
            raise HTTPException(status_code=403, detail="Access denied")
        
        all_attempts = db.query(TraineeAttempt).filter(
            TraineeAttempt.course_id == course_id
        ).all()
        
        if not all_attempts:
            return {"success": True, "batches": [], "manual_trainees": [], "total_batches": 0, "total_trainees": 0}
        
        attempts_with_batch = [a for a in all_attempts if a.batch_id is not None]
        attempts_without_batch = [a for a in all_attempts if a.batch_id is None]

        # Every trainee named by an attempt, in one query. The loops below used to read each
        # trainee back on its own, so a course published to a large batch spent the whole
        # request on round-trips.
        enrolled_trainee_ids = {a.trainee_id for a in all_attempts if a.trainee_id}
        users_by_id = {}
        if enrolled_trainee_ids:
            users_by_id = {
                u.id: u for u in db.query(User).filter(User.id.in_(enrolled_trainee_ids)).all()
            }

        batch_dict = {}

        # Process batch-published trainees
        if attempts_with_batch:
            batch_ids = set([a.batch_id for a in attempts_with_batch])
            active_batches_by_id = {
                b.id: b
                for b in db.query(TraineeBatch).filter(
                    TraineeBatch.id.in_(batch_ids),
                    TraineeBatch.is_active == True
                ).all()
            }
            for batch_id in batch_ids:
                batch = active_batches_by_id.get(batch_id)
                if batch:
                    batch_trainee_ids = set([a.trainee_id for a in attempts_with_batch if a.batch_id == batch_id])
                    # Get trainee details for this batch
                    batch_trainees = []
                    for tid in batch_trainee_ids:
                        t = users_by_id.get(tid)
                        if t:
                            batch_trainees.append({"id": t.id, "email": t.email})
                    batch_dict[batch.id] = {
                        "batch_id": batch.id,
                        "batch_name": batch.batch_name,
                        "batch_description": batch.batch_description,
                        "trainee_count": len(batch_trainee_ids),
                        "trainees": sorted(batch_trainees, key=lambda x: x["email"]),
                        "created_at": batch.created_at.isoformat() if batch.created_at else None
                    }
        
        # SMART FALLBACK: legacy records without batch_id — check if they match a batch
        if attempts_without_batch and not attempts_with_batch:
            trainee_ids_set = set([a.trainee_id for a in attempts_without_batch])
            total_trainees_count = len(trainee_ids_set)
            trainer_batches = db.query(TraineeBatch).filter(
                TraineeBatch.trainer_id == course.author_id,
                TraineeBatch.is_active == True
            ).all()
            # Membership of all the trainer's batches in one query rather than one per batch.
            members_by_batch = defaultdict(set)
            if trainer_batches:
                for row in db.query(BatchMember.batch_id, BatchMember.trainee_id).filter(
                    BatchMember.batch_id.in_([b.id for b in trainer_batches])
                ).all():
                    members_by_batch[row[0]].add(row[1])
            matched_batch_trainee_ids = set()
            for batch in trainer_batches:
                batch_member_ids = members_by_batch.get(batch.id, set())
                trainees_in_batch = trainee_ids_set.intersection(batch_member_ids)
                if len(trainees_in_batch) == total_trainees_count:
                    batch_trainees = []
                    for tid in trainees_in_batch:
                        t = users_by_id.get(tid)
                        if t:
                            batch_trainees.append({"id": t.id, "email": t.email})
                    batch_dict[batch.id] = {
                        "batch_id": batch.id,
                        "batch_name": batch.batch_name,
                        "batch_description": batch.batch_description,
                        "trainee_count": len(trainees_in_batch),
                        "trainees": sorted(batch_trainees, key=lambda x: x["email"]),
                        "created_at": batch.created_at.isoformat() if batch.created_at else None
                    }
                    matched_batch_trainee_ids.update(trainees_in_batch)
            # Remaining trainees not matched to any batch are manual
            attempts_without_batch = [a for a in attempts_without_batch if a.trainee_id not in matched_batch_trainee_ids]
        
        # Collect manually added trainees (no batch_id)
        manual_trainees = []
        seen_manual_ids = set()
        for attempt in attempts_without_batch:
            if attempt.trainee_id not in seen_manual_ids:
                t = users_by_id.get(attempt.trainee_id)
                if t:
                    manual_trainees.append({"id": t.id, "email": t.email})
                    seen_manual_ids.add(attempt.trainee_id)
        manual_trainees.sort(key=lambda x: x["email"])
        
        batch_list = list(batch_dict.values())
        all_trainee_ids = set()
        for b in batch_list:
            for tr in b["trainees"]:
                all_trainee_ids.add(tr["id"])
        for mt in manual_trainees:
            all_trainee_ids.add(mt["id"])
        
        return {
            "success": True,
            "batches": batch_list,
            "manual_trainees": manual_trainees,
            "total_batches": len(batch_list),
            "total_trainees": len(all_trainee_ids)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Error getting course batches: %s', e)
        raise HTTPException(status_code=500, detail="Failed to get course batches")

@router.get("/trainer/analytics", tags=["Trainer Analytics"])
def get_trainer_analytics(user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """Get analytics for the trainer's trainees (only trainees enrolled in their published courses)."""
    if not user.process_id:
        raise HTTPException(status_code=400, detail="Trainer must be assigned to a process")
    
    # Get only THIS user's PUBLISHED courses
    my_courses = db.query(Course).filter(
        Course.author_id == user.id,
        Course.is_live == True  # Only published courses
    ).all()
    my_course_ids = {c.id for c in my_courses}
    
    logger.info('[TRAINER ANALYTICS] User %s has %s published courses', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")), len(my_courses))
    
    if not my_course_ids:
        logger.info('[TRAINER ANALYTICS] No published courses found for user %s', ("" if user.email is None else str(user.email).replace("\n", "").replace("\r", "")))
        return {"success": True, "trainees": []}
    
    # Get only trainees who have attempts for THIS user's PUBLISHED courses
    trainee_ids_with_attempts = db.query(TraineeAttempt.trainee_id).filter(
        TraineeAttempt.course_id.in_(my_course_ids)
    ).distinct().all()
    trainee_ids = {t[0] for t in trainee_ids_with_attempts}
    
    logger.info("[TRAINER ANALYTICS] Found %s trainees with attempts in user's published courses", len(trainee_ids))
    
    if not trainee_ids:
        return {"success": True, "trainees": []}
    
    # Get only these specific trainees
    trainees = db.query(User).filter(
        User.id.in_(trainee_ids),
        User.role == "trainee"
    ).all()
    
    logger.info('[TRAINER ANALYTICS] Processing analytics for %s trainees', len(trainees))

    # Enrolments, progress counts, assessment configs and assessment attempts for every
    # trainee/course pair on this tab, in four queries. This was four queries *per pair*:
    # 40 trainees across 10 courses meant ~1,600 round-trips, well past the request timeout.
    course_id_list = list(my_course_ids)
    # Module counts for the same pairs: one S3 pass for this trainer instead of re-filtering the
    # index the first time each course is seen inside the trainee loop below.
    prefetch_s3_module_counts(db, {user.id})
    enrolled_pairs = {
        (row[0], row[1])
        for row in db.query(TraineeAttempt.trainee_id, TraineeAttempt.course_id)
        .filter(TraineeAttempt.course_id.in_(course_id_list))
        .distinct()
        .all()
    }
    progress_counts = {
        (row[0], row[1]): row[2]
        for row in db.query(
            CourseProgress.user_id, CourseProgress.course_id, func.count(CourseProgress.id)
        )
        .filter(CourseProgress.course_id.in_(course_id_list))
        .group_by(CourseProgress.user_id, CourseProgress.course_id)
        .all()
    }
    configs_by_course = {
        cfg.course_id: cfg
        for cfg in db.query(AssessmentConfig)
        .filter(AssessmentConfig.course_id.in_(course_id_list))
        .all()
    }
    # Newest first, so the first row seen for a module is that module's latest attempt - the
    # same row the per-module comparison used to pick.
    assessments_by_pair = defaultdict(list)
    for row in (
        db.query(TraineeAssessment)
        .filter(TraineeAssessment.course_id.in_(course_id_list))
        .order_by(TraineeAssessment.completed_at.desc())
        .all()
    ):
        assessments_by_pair[(row.trainee_id, row.course_id)].append(row)

    trainee_analytics = []
    for trainee in trainees:
        total_courses = 0
        completed_courses = 0
        in_progress_courses = 0

        # Only count THIS user's published courses
        for course in my_courses:
            if (trainee.id, course.id) in enrolled_pairs:
                total_courses += 1
                completed_count = progress_counts.get((trainee.id, course.id), 0)

                # Get total modules for this course
                total_modules = _count_modules_for_course(course.id, db)

                logger.debug('[TRAINER ANALYTICS] Trainee %s, Course %s: %s/%s modules', log_safe(trainee.email), log_safe(course.source_doc_name), completed_count, total_modules)

                # A course is only "completed" when BOTH study AND assessment are done
                study_done = completed_count >= total_modules and total_modules > 0
                assessment_config = configs_by_course.get(course.id)
                pair_assessments = assessments_by_pair.get((trainee.id, course.id), [])
                assessment_passed = False
                if assessment_config:
                    if assessment_config.assessment_scope == 'end_of_course':
                        assessment_passed = any(
                            a.module_index is None and a.passed for a in pair_assessments
                        )
                    elif assessment_config.assessment_scope == 'end_of_module':
                        module_latest = {}
                        for a in pair_assessments:
                            if a.module_index is not None and a.module_index not in module_latest:
                                module_latest[a.module_index] = a
                        if module_latest and total_modules > 0:
                            assessment_passed = (len(module_latest) == total_modules and all(a.passed for a in module_latest.values()))
                else:
                    # No assessment configured - study completion is enough
                    assessment_passed = True

                if study_done and assessment_passed:
                    completed_courses += 1
                elif completed_count > 0:
                    in_progress_courses += 1
        
        logger.info('[TRAINER ANALYTICS] Trainee %s: %s total, %s completed, %s in progress', log_safe(trainee.email), total_courses, completed_courses, in_progress_courses)
        
        trainee_analytics.append({
            "id": trainee.id,
            "email": trainee.email,
            "total_courses": total_courses,
            "completed_courses": completed_courses,
            "in_progress_courses": in_progress_courses,
            "not_started_courses": total_courses - completed_courses - in_progress_courses
        })
    
    return {"success": True, "trainees": trainee_analytics}

@router.post("/trainer/course/{doc_name:path}/toggle-live", tags=["Trainer Dashboard & Courses"])
def toggle_course_live_status(doc_name: str, user: User = Depends(trainer_required), db: Session = Depends(get_db)):
    """
    Toggles the live/published status of a course.

    Unpublishing a course makes it inaccessible to trainees.
    When unpublishing, all TraineeAttempt records are removed so trainees no longer see the course.
    - **Required Role**: Trainer or Super Admin.
    - **doc_name**: The filename of the source document for the course.
    - **Raises 404**: If the course is not found.
    """
    course = db.query(Course).filter(Course.author_id == user.id, Course.source_doc_name == doc_name).first()
    if not course: raise HTTPException(status_code=404, detail="Course not found.")
    course.is_live = not course.is_live
    status = "re-published" if course.is_live else "unpublished"
    course_display = course_display_label(course)

    removed_count = 0
    if not course.is_live:
        # FIX: When unpublishing, remove all TraineeAttempt records so trainees no longer see the course
        affected_trainees = db.query(TraineeAttempt).filter(TraineeAttempt.course_id == course.id).all()
        removed_count = len(affected_trainees)
        for attempt in affected_trainees:
            # Notify each affected trainee
            db.add(Notification(
                user_id=attempt.trainee_id,
                message=f"The course '{course_display}' has been unpublished by your trainer and is no longer available.",
                related_link="/trainee-dashboard",
                course_id=course_notification_id(course)
            ))
            db.delete(attempt)
        logger.info("[UNPUBLISH] Removed %s TraineeAttempt records for course '%s'", ("" if removed_count is None else str(removed_count).replace("\n", "").replace("\r", "")), ("" if doc_name is None else str(doc_name).replace("\n", "").replace("\r", "")))

    # Notify super admins
    for admin in db.query(User).filter(User.role == 'super_admin').all():
        db.add(Notification(user_id=admin.id, message=f"Trainer {user.email} has {status} the course '{course_display}' ({removed_count} trainee(s) affected).", course_id=course_notification_id(course)))

    log_activity(db, user.id, "TOGGLE_COURSE_STATUS", f"{status.capitalize()} course '{doc_name}' ({removed_count} trainees affected)")
    db.commit()
    return {"message": f"Course has been {status}.", "is_live": course.is_live, "removed_count": removed_count}


class SelectiveUnpublishRequest(BaseModel):
    trainee_ids: List[str] = []
    fully_unpublish: bool = False


@router.post("/trainer/course/{course_id}/unpublish-selective", tags=["Trainer Dashboard & Courses"])
def unpublish_course_selective(
    course_id: str,
    req: SelectiveUnpublishRequest,
    user: User = Depends(trainer_required),
    db: Session = Depends(get_db)
):
    """
    Selectively unpublish a course for specific trainees, or fully unpublish it.
    - If fully_unpublish=True: removes all TraineeAttempt records and sets is_live=False.
    - Otherwise: removes TraineeAttempt records only for the given trainee_ids.
      If no trainees remain after removal, also sets is_live=False.
    """
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    # Authorization: trainer must own the course, super_admin can do anything
    if user.role == 'trainer' and course.author_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied.")

    removed_count = 0
    course_display = course_display_label(course)
    affected_trainee_ids = []

    if req.fully_unpublish:
        # Collect affected trainee IDs before deleting
        affected_attempts = db.query(TraineeAttempt).filter(
            TraineeAttempt.course_id == course_id
        ).all()
        affected_trainee_ids = [a.trainee_id for a in affected_attempts]
        removed_count = len(affected_trainee_ids)
        # Remove all attempts and mark unpublished
        db.query(TraineeAttempt).filter(
            TraineeAttempt.course_id == course_id
        ).delete(synchronize_session=False)
        course.is_live = False
        message = f"Course fully unpublished. {removed_count} trainee(s) removed."
    else:
        # Remove only selected trainees. The rows are read first so the response can still report
        # which trainees actually lost access, then deleted in one statement instead of one per
        # trainee - the same rows, without a round-trip per id.
        if req.trainee_ids:
            doomed_attempts = db.query(TraineeAttempt).filter(
                TraineeAttempt.course_id == course_id,
                TraineeAttempt.trainee_id.in_(req.trainee_ids),
            ).all()
            removed_count = len(doomed_attempts)
            removed_ids = {a.trainee_id for a in doomed_attempts}
            # Ordered by the request and listed once per trainee, as the per-id loop produced.
            for tid in req.trainee_ids:
                if tid in removed_ids and tid not in affected_trainee_ids:
                    affected_trainee_ids.append(tid)
            if removed_ids:
                db.query(TraineeAttempt).filter(
                    TraineeAttempt.course_id == course_id,
                    TraineeAttempt.trainee_id.in_(list(removed_ids)),
                ).delete(synchronize_session=False)

        # Check if any trainees remain
        remaining = db.query(TraineeAttempt).filter(
            TraineeAttempt.course_id == course_id
        ).count()
        if remaining == 0:
            course.is_live = False
            message = f"All trainees removed. Course has been unpublished."
        else:
            message = f"{removed_count} trainee(s) removed from course access."

    # FIX: Notify each affected trainee that the course was removed
    for tid in affected_trainee_ids:
        db.add(Notification(
            user_id=tid,
            message=f"The course '{course_display}' has been unpublished and is no longer available to you.",
            related_link="/trainee-dashboard",
            course_id=course_notification_id(course)
        ))

    # FIX: Notify the trainer themselves when they unpublish their own course
    if user.role == 'trainer' and course.author_id == user.id:
        db.add(Notification(
            user_id=user.id,
            message=f"✅ You have unpublished course '{course_display}'. {removed_count} trainee(s) have been removed and notified.",
            related_link="/trainer-dashboard",
            course_id=course_notification_id(course)
        ))

    # Notify super admins when a trainer performs the action
    if user.role == 'trainer':
        for admin in db.query(User).filter(User.role == 'super_admin').all():
            db.add(Notification(
                user_id=admin.id,
                message=f"Trainer {user.email} unpublished course '{course_display}' for {removed_count} trainee(s).",
                course_id=course_notification_id(course)
            ))

    # Notify the course owner when a super admin performs the action
    if user.role == 'super_admin' and course.author_id != user.id:
        db.add(Notification(
            user_id=course.author_id,
            message=f"Super Admin ({user.email}) has unpublished your course '{course_display}' for {removed_count} trainee(s).",
            course_id=course_notification_id(course)
        ))

    log_activity(db, user.id, "SELECTIVE_UNPUBLISH", f"Unpublished '{course.source_doc_name}' for {removed_count} trainee(s)")
    db.commit()
    return {"success": True, "message": message, "is_live": course.is_live, "removed_count": removed_count}

@router.get("/trainee/dashboard", response_model=List[TraineeCourseInfo], tags=["Trainee Dashboard & Quizzes"])
def get_trainee_dashboard(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves the dashboard view for a trainee, showing their assigned courses.
    
    IMPORTANT: Only shows PUBLISHED courses to trainees for security and quality control.

    - **Required Role**: Trainee.
    - **Raises 403**: If a non-trainee attempts to access.
    """
    if user.role != 'trainee': raise HTTPException(status_code=403, detail="Access denied")

    from sqlalchemy.orm import joinedload

    # CRITICAL FIX: Only show PUBLISHED courses to trainees (newest courses first)
    # joinedload keeps course and author from being lazy-loaded once per attempt below.
    attempts = (
        db.query(TraineeAttempt)
        .join(Course)
        .options(joinedload(TraineeAttempt.course).joinedload(Course.author))
        .filter(
            TraineeAttempt.trainee_id == user.id,
            Course.is_live == True,  # Only published courses
        )
        .order_by(Course.created_at.desc())
        .all()
    )

    # The per-course counts and config below were a query each inside the loop. They are now
    # one grouped query apiece for the whole dashboard, which together with the shared S3
    # metadata index is what brings this endpoint back inside the client's request timeout.
    course_ids = [att.course.id for att in attempts if att.course]
    completed_modules_by_course = {}
    config_by_course = {}
    course_assessment_counts = {}
    module_assessment_counts = {}
    course_attempts_left_by_course = {}
    latest_assessments_by_course = {}
    if course_ids:
        latest_assessments_by_course = build_trainee_latest_assessments(user.id, course_ids, db)
        completed_modules_by_course = dict(
            db.query(CourseProgress.course_id, func.count(CourseProgress.id))
            .filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id.in_(course_ids),
            )
            .group_by(CourseProgress.course_id)
            .all()
        )
        config_by_course = {
            cfg.course_id: cfg
            for cfg in db.query(AssessmentConfig)
            .filter(AssessmentConfig.course_id.in_(course_ids))
            .all()
        }
        course_assessment_counts = dict(
            db.query(TraineeAssessment.course_id, func.count(TraineeAssessment.id))
            .filter(
                TraineeAssessment.trainee_id == user.id,
                TraineeAssessment.course_id.in_(course_ids),
                TraineeAssessment.module_index.is_(None),
            )
            .group_by(TraineeAssessment.course_id)
            .all()
        )
        module_assessment_counts = dict(
            db.query(TraineeAssessment.course_id, func.count(TraineeAssessment.id))
            .filter(
                TraineeAssessment.trainee_id == user.id,
                TraineeAssessment.course_id.in_(course_ids),
                TraineeAssessment.module_index.isnot(None),
            )
            .group_by(TraineeAssessment.course_id)
            .all()
        )
        # The end-of-course "Attempts Left" figure, batched the same way as the counts above. The
        # card used to show TraineeAttempt.attempts_left, which is seeded from
        # Course.initial_attempts and so disagreed with the cap the server actually enforces.
        course_attempts_left_by_course = end_of_course_attempts_left_by_course(
            db, user.id, course_ids, config_by_course
        )
        # One S3 pass per distinct course author — avoids N× module-count scans (504 risk).
        prefetch_s3_module_counts(
            db, {att.course.author_id for att in attempts if att.course and att.course.author_id}
        )

    courses_info = []
    attempts_synced = False
    for att in attempts:
        if att.course:
            total_modules = _count_modules_for_course(att.course.id, db)
            completed_modules = completed_modules_by_course.get(att.course.id, 0)

            # Get assessment configuration for this course
            assessment_config = config_by_course.get(att.course.id)

            # Auto-sync legacy attempts_left==1 rows for MODULE-scope only. End-of-course
            # remaining is computed from stored assessments (+ grants); rewriting the column
            # here was how a trainee with 3/3 used still showed "2 Attempts Left" after a sync.
            if (
                assessment_config
                and assessment_config.assessment_scope == "end_of_module"
                and att.attempts_left != assessment_config.max_attempts
                and att.attempts_left == 1
            ):
                logger.warning('[TRAINEE DASHBOARD] Auto-syncing attempts for trainee %s in course %s: %s -> %s', log_safe(user.email), log_safe(att.course.source_doc_name), log_safe(att.attempts_left), log_safe(assessment_config.max_attempts))
                att.attempts_left = assessment_config.max_attempts
                attempts_synced = True

            assessment_state = _get_trainee_course_assessment_state(
                user.id,
                att.course.id,
                assessment_config,
                total_modules,
                completed_modules,
                db,
                latest_assessments=latest_assessments_by_course,
            )
            assessment_status = assessment_state["assessment_status"]
            assessment_score = assessment_state["assessment_score"]
            module_assessment_details = assessment_state["module_assessment_details"]
            course_completed = assessment_state["course_completed"]
            assessment_attempts_used = None
            # attempts_left drives the "Attempts Left" card tile and the take/retake button. For an
            # end-of-course course it has to be the same arithmetic the load and submit gates use,
            # or the card offers a retake the server refuses. Module-scope courses keep the legacy
            # column: their real budget is per module (each module carries its own
            # attempts_remaining), a single course-level number would be meaningless, and this field
            # also gates whether the module content block renders at all.
            course_attempts_left = att.attempts_left
            if assessment_config and assessment_config.assessment_scope == "end_of_course":
                assessment_attempts_used = course_assessment_counts.get(att.course.id, 0)
                course_attempts_left = course_attempts_left_by_course.get(
                    att.course.id, course_attempts_left
                )
            elif assessment_config and assessment_config.assessment_scope == "end_of_module":
                assessment_attempts_used = module_assessment_counts.get(att.course.id, 0)

            course_info = TraineeCourseInfo(
                course_id=att.course.id, 
                source_doc_name=att.course.source_doc_name,
                course_title=course_display_label(att.course),
                author_email=att.course.author.email, 
                attempts_left=course_attempts_left,
                author_id=att.course.author.id,
                total_modules=total_modules, 
                completed_modules=completed_modules, 
                is_published=att.course.is_live,
                # Assessment info
                assessment_configured=assessment_config is not None,
                assessment_scope=assessment_config.assessment_scope if assessment_config else None,
                max_attempts_per_module=assessment_config.max_attempts if assessment_config else None,
                pass_percentage=assessment_config.pass_percentage if assessment_config else None,
                total_questions=assessment_config.total_questions if assessment_config else None,
                course_completed=course_completed,
                assessment_status=assessment_status,
                assessment_score=assessment_score,
                module_assessment_details=module_assessment_details if assessment_config and assessment_config.assessment_scope == 'end_of_module' else [],
                assessment_attempts_used=assessment_attempts_used,
                course_created_at=att.course.created_at,
            )
            courses_info.append(course_info)

    # Single commit for any attempts_left values the loop re-synced above.
    if attempts_synced:
        db.commit()
    return courses_info

@router.get("/trainee/my-stats", response_model=TraineeStats, tags=["Trainee Dashboard & Quizzes"])
def get_trainee_stats(user: User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """
    Retrieves overall statistics for the logged-in trainee.
    
    IMPORTANT: Only counts PUBLISHED courses for accurate statistics.

    - **Required Role**: Trainee.
    """
    if user.role != 'trainee': 
        raise HTTPException(status_code=403, detail="Access denied")
    
    from sqlalchemy.orm import joinedload

    # CRITICAL FIX: Only count PUBLISHED courses in statistics
    # joinedload replaces the per-attempt Course lookup the loop below used to do, even though
    # the join above had already read the row.
    all_course_attempts = db.query(TraineeAttempt).join(Course).options(
        joinedload(TraineeAttempt.course)
    ).filter(
        TraineeAttempt.trainee_id == user.id,
        Course.is_live == True  # Only published courses
    ).all()

    courses_assigned = len(all_course_attempts)
    courses_completed = 0
    courses_in_progress = 0
    courses_not_started = 0
    total_attempts_left = 0
    total_modules_completed = 0
    total_modules_assigned = 0

    # Progress counts, assessment configs and assessment rows for every course at once, rather
    # than three queries per course inside the loop.
    stats_course_ids = [a.course_id for a in all_course_attempts if a.course_id]
    progress_by_course = {}
    stats_config_by_course = {}
    stats_attempts_left_by_course = {}
    assessments_by_course = defaultdict(list)
    if stats_course_ids:
        progress_by_course = dict(
            db.query(CourseProgress.course_id, func.count(CourseProgress.id))
            .filter(
                CourseProgress.user_id == user.id,
                CourseProgress.course_id.in_(stats_course_ids),
            )
            .group_by(CourseProgress.course_id)
            .all()
        )
        stats_config_by_course = {
            cfg.course_id: cfg
            for cfg in db.query(AssessmentConfig)
            .filter(AssessmentConfig.course_id.in_(stats_course_ids))
            .all()
        }
        for assessment in (
            db.query(TraineeAssessment)
            .filter(
                TraineeAssessment.trainee_id == user.id,
                TraineeAssessment.course_id.in_(stats_course_ids),
            )
            .all()
        ):
            assessments_by_course[assessment.course_id].append(assessment)
        # Counted end-of-course remaining, so this total agrees with the per-course cards instead of
        # summing a legacy column the admin reset/sync endpoints rewrite.
        stats_attempts_left_by_course = end_of_course_attempts_left_by_course(
            db, user.id, stats_course_ids, stats_config_by_course
        )
        prefetch_s3_module_counts(
            db,
            {
                a.course.author_id
                for a in all_course_attempts
                if a.course and a.course.author_id
            },
        )

    for attempt in all_course_attempts:
        course = attempt.course
        if not course:
            continue

        try:
            # Use the same reliable module count helper as the admin panel
            module_count = _count_modules_for_course(course.id, db)
            total_modules_assigned += module_count

            completed_count = progress_by_course.get(course.id, 0)
            total_modules_completed += completed_count

            if completed_count == 0:
                courses_not_started += 1
            elif completed_count >= module_count and module_count > 0:
                # Mirror admin logic: also require assessment to be passed (if configured)
                assessment_config_obj = stats_config_by_course.get(course.id)
                assessment_passed = False
                if assessment_config_obj:
                    course_assessments = assessments_by_course.get(course.id, [])
                    if assessment_config_obj.assessment_scope == 'end_of_course':
                        assessment_passed = any(
                            a.module_index is None and a.passed for a in course_assessments
                        )
                    elif assessment_config_obj.assessment_scope == 'end_of_module':
                        mod_assessments = [
                            a for a in course_assessments if a.module_index is not None
                        ]
                        if mod_assessments and module_count > 0:
                            mod_latest = {}
                            for a in mod_assessments:
                                if a.module_index not in mod_latest or a.completed_at > mod_latest[a.module_index].completed_at:
                                    mod_latest[a.module_index] = a
                            assessment_passed = (len(mod_latest) == module_count and all(a.passed for a in mod_latest.values()))
                else:
                    assessment_passed = True  # No assessment configured

                if assessment_passed:
                    courses_completed += 1
                else:
                    courses_in_progress += 1
            else:
                courses_in_progress += 1

            total_attempts_left += stats_attempts_left_by_course.get(
                course.id, max(0, attempt.attempts_left)
            )

        except Exception as e:
            logger.error('Error calculating stats for course %s: %s', log_safe(course.id), log_safe(e))


    return TraineeStats(
        courses_assigned=courses_assigned,
        courses_completed=courses_completed,
        courses_in_progress=courses_in_progress,
        courses_not_started=courses_not_started,
        total_attempts_left=total_attempts_left,
        total_modules_completed=total_modules_completed,
        total_modules_assigned=total_modules_assigned
    )
