from __future__ import annotations

from core.runtime import *
from sqlalchemy import false

from utils.timezone_helpers import now_ist


class Process(Base):
    __tablename__ = "processes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, unique=True, index=True, nullable=False)
    users = relationship("User", back_populates="process")


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="trainee", nullable=False)
    is_approved = Column(Boolean, default=False, nullable=False)
    # Set on every account an admin creates (single or bulk): it starts on the predictable
    # St@<EmployeeID>#2026 login, so get_current_active_user refuses everything but the
    # password change until the user has replaced it. Added by Alembic revision 0002.
    must_change_password = Column(Boolean, default=False, nullable=False, server_default=false())
    process_id = Column(String, ForeignKey("processes.id"), nullable=True)
    process = relationship("Process", back_populates="users")
    sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    shared_reports_sent = relationship(
        "SharedReport",
        foreign_keys="[SharedReport.sharer_id]",
        back_populates="sharer",
        cascade="all, delete-orphan",
    )
    shared_reports_received = relationship(
        "SharedReport",
        foreign_keys="[SharedReport.recipient_id]",
        back_populates="recipient",
        cascade="all, delete-orphan",
    )
    edits_made = relationship("SharedReportEdit", back_populates="editor", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    authored_courses = relationship("Course", back_populates="author", cascade="all, delete-orphan")
    attempts = relationship("TraineeAttempt", back_populates="trainee", cascade="all, delete-orphan")
    progress = relationship("CourseProgress", back_populates="user", cascade="all, delete-orphan")


class Course(Base):
    __tablename__ = "courses"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    source_doc_name = Column(String, nullable=False)
    author_id = Column(String, ForeignKey("users.id"), nullable=False)
    process_id = Column(String, ForeignKey("processes.id"), nullable=False)
    initial_attempts = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_live = Column(Boolean, default=False, nullable=False)
    tokens_used = Column(Integer, default=0, nullable=False)
    generation_cost = Column(Integer, default=0, nullable=False)
    pdf_hash = Column(String, nullable=True)
    course_title = Column(String, nullable=True)
    # STORAGE names (randomized "<uuid>.<ext>" from randomized_storage_name) - the join key
    # for S3 objects under {author_id}/uploaded_docs/ and for module metadata "source_doc".
    source_documents = Column(Text, nullable=True)
    # DISPLAY names the user actually uploaded, JSON list, positionally aligned with
    # source_documents. Kept separate because the storage name is deliberately randomized
    # (VAPT #4: Double Extension File Upload) and the original name cannot be derived from
    # it. Never join on this column - it is user-controlled text, for rendering only.
    source_document_display_names = Column(Text, nullable=True)
    author = relationship("User", back_populates="authored_courses")
    process = relationship("Process")
    attempts = relationship("TraineeAttempt", back_populates="course", cascade="all, delete-orphan")
    progress = relationship("CourseProgress", back_populates="course", cascade="all, delete-orphan")


class TraineeAttempt(Base):
    __tablename__ = "trainee_attempts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    trainee_id = Column(String, ForeignKey("users.id"), nullable=False)
    course_id = Column(String, ForeignKey("courses.id"), nullable=False)
    attempts_left = Column(Integer, nullable=False)
    batch_id = Column(String, ForeignKey("trainee_batches.id"), nullable=True)
    trainee = relationship("User", back_populates="attempts")
    course = relationship("Course", back_populates="attempts")


class CourseProgress(Base):
    __tablename__ = "course_progress"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    course_id = Column(String, ForeignKey("courses.id"), nullable=False)
    completed_module_index = Column(Integer, nullable=False)
    user = relationship("User", back_populates="progress")
    course = relationship("Course", back_populates="progress")
    __table_args__ = (
        UniqueConstraint("user_id", "course_id", "completed_module_index", name="_user_course_module_uc"),
    )


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    session_name = Column(String, default="New Chat Session")
    chat_type = Column(String, default="general", nullable=False)
    kb_namespace_user_id = Column(String, nullable=True)
    video_history_ids = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    user = relationship("User", back_populates="sessions")
    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.timestamp",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, ForeignKey("chat_sessions.id"), nullable=False)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    sources = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    session = relationship("ChatSession", back_populates="messages")


class TutorResponseCache(Base):
    __tablename__ = "tutor_response_cache"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    course_id = Column(String, ForeignKey("courses.id"), nullable=False)
    module_index = Column(Integer, nullable=False)
    query_hash = Column(String, nullable=False)
    response_s3_key = Column(String, nullable=False)
    tokens_used = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("course_id", "module_index", "query_hash", name="_cache_lookup_uc"),)


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    course_id = Column(String, ForeignKey("courses.id"), nullable=True)
    operation_type = Column(String, nullable=False)
    tokens_used = Column(Integer, nullable=False)
    cost_cents = Column(Integer, default=0)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    user = relationship("User")
    course = relationship("Course")


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True)
    setting_key = Column(String, unique=True, nullable=False)
    setting_value = Column(String, nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SharedReport(Base):
    __tablename__ = "shared_reports"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    source_doc_name = Column(String, nullable=False)
    sharer_id = Column(String, ForeignKey("users.id"), nullable=False)
    recipient_id = Column(String, ForeignKey("users.id"), nullable=False)
    shared_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    status = Column(String, default="pending")
    feedback = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    sharer = relationship("User", foreign_keys=[sharer_id], back_populates="shared_reports_sent")
    recipient = relationship("User", foreign_keys=[recipient_id], back_populates="shared_reports_received")
    edits = relationship("SharedReportEdit", back_populates="shared_report", cascade="all, delete-orphan")


class SharedReportEdit(Base):
    __tablename__ = "shared_report_edits"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shared_report_id = Column(String, ForeignKey("shared_reports.id"), nullable=False)
    editor_id = Column(String, ForeignKey("users.id"), nullable=False)
    edit_type = Column(String, default="module", nullable=False)
    report_filename = Column(String, nullable=False)
    original_content = Column(Text, nullable=False)
    proposed_content = Column(Text, nullable=False)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    shared_report = relationship("SharedReport", back_populates="edits")
    editor = relationship("User", back_populates="edits_made")


class SharedCourse(Base):
    __tablename__ = "shared_courses"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    course_id = Column(String, ForeignKey("courses.id"), nullable=False)
    sharer_id = Column(String, ForeignKey("users.id"), nullable=False)
    recipient_id = Column(String, ForeignKey("users.id"), nullable=False)
    shared_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    status = Column(String, default="pending")
    feedback = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    course = relationship("Course")
    sharer = relationship("User", foreign_keys=[sharer_id])
    recipient = relationship("User", foreign_keys=[recipient_id])
    edits = relationship("SharedCourseEdit", back_populates="shared_course", cascade="all, delete-orphan")


class SharedCourseEdit(Base):
    __tablename__ = "shared_course_edits"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shared_course_id = Column(String, ForeignKey("shared_courses.id"), nullable=False)
    editor_id = Column(String, ForeignKey("users.id"), nullable=False)
    edit_type = Column(String, default="module", nullable=False)
    report_filename = Column(String, nullable=False)
    original_content = Column(Text, nullable=False)
    proposed_content = Column(Text, nullable=False)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    shared_course = relationship("SharedCourse", back_populates="edits")
    editor = relationship("User")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    message = Column(String, nullable=False)
    related_link = Column(String, nullable=True)
    # The course this notification is about, so deleting a course also removes the
    # notifications that talk about it. Nullable because most notifications (account
    # approval, voice training, sharing invitations) belong to no course, and SET NULL so a
    # notification can never keep a course row alive or block its delete.
    course_id = Column(
        String, ForeignKey("courses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    user = relationship("User", back_populates="notifications")


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    action = Column(String, nullable=False)
    details = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    user = relationship("User")


class VideoAnalysisHistory(Base):
    __tablename__ = "video_analysis_history"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    session_id = Column(String, nullable=False)
    video_filename = Column(String, nullable=False)
    transcript_filename = Column(String, nullable=False)
    pdf_title = Column(String, nullable=True)
    pdf_s3_key = Column(String, nullable=True)
    docx_s3_key = Column(String, nullable=True)
    export_format = Column(String, nullable=True)  # 'pdf' or 'docx' — last generated export
    markdown_content = Column(Text, nullable=True)
    source_doc_name = Column(String, nullable=True)
    embedding_status = Column(String, default="pending", nullable=True)
    embedded_at = Column(DateTime, nullable=True)
    chunk_count = Column(Integer, nullable=True)
    video_s3_key = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    user = relationship("User")


class RateLimitSetting(Base):
    __tablename__ = "rate_limit_settings"

    role = Column(String, primary_key=True)
    limit = Column(String, nullable=False)


class TraineeBatch(Base):
    __tablename__ = "trainee_batches"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    trainer_id = Column(String, ForeignKey("users.id"), nullable=False)
    batch_name = Column(String, nullable=False)
    batch_description = Column(Text, nullable=True)
    process_id = Column(String, ForeignKey("processes.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    trainer = relationship("User")
    process = relationship("Process")
    members = relationship("BatchMember", back_populates="batch", cascade="all, delete-orphan")


class BatchMember(Base):
    __tablename__ = "batch_members"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id = Column(String, ForeignKey("trainee_batches.id"), nullable=False)
    trainee_id = Column(String, ForeignKey("users.id"), nullable=False)
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    batch = relationship("TraineeBatch", back_populates="members")
    trainee = relationship("User")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    session_token = Column(String, nullable=False, unique=True)
    # Previous JWT kept briefly after /auth/refresh so in-flight requests that still
    # carry the old Bearer token are not 401'd mid-flight (notification poll race).
    previous_session_token = Column(String, nullable=True)
    login_time = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    last_activity = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    logout_time = Column(DateTime, nullable=True)
    user = relationship("User")


class LoginAttempt(Base):
    """
    Failed-login ledger for brute-force throttling (VAPT: Password Bruteforcible).

    Stored in the DB rather than in process memory because the container runs
    ``uvicorn --workers 3``: an in-memory counter gives each worker its own tally,
    so the effective limit is silently multiplied by the worker count.

    One row per failed attempt; successful logins clear the account's rows.
    ``scope`` is ``"account"`` (identifier = lower-cased email) or ``"ip"``
    (identifier = client address), so per-account and per-IP limits are counted
    independently as the remediation requires.
    """

    __tablename__ = "login_attempts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    scope = Column(String, nullable=False, index=True)
    identifier = Column(String, nullable=False, index=True)
    # Naive UTC, unlike the tz-aware defaults elsewhere in this file. Those columns are
    # only ever compared in Python (after `_as_utc()` normalization); this one is
    # filtered in SQL against a rolling window, and on PostgreSQL a `DateTime` column is
    # TIMESTAMP WITHOUT TIME ZONE. Mixing a tz-aware bind into that comparison makes the
    # server apply its own TimeZone setting, so on a non-UTC RDS instance the window
    # would shift by the offset. Keep both sides naive UTC - see utils/login_throttle._now().
    attempted_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        Index("ix_login_attempts_scope_identifier_time", "scope", "identifier", "attempted_at"),
    )


class JobStatus(Base):
    __tablename__ = "job_statuses"

    job_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")
    progress_message = Column(String, default="Job is queued...")
    result = Column(JSON, nullable=True)
    error = Column(String, nullable=True)
    token_usage = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    cleanup_scheduled = Column(Boolean, default=False)
    user = relationship("User")


class AssessmentConfig(Base):
    __tablename__ = "assessment_configs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    course_id = Column(String, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    assessment_scope = Column(String, nullable=False)
    question_type = Column(String, nullable=False)
    total_questions = Column(Integer, nullable=False)
    mcq_count = Column(Integer, default=0)
    subjective_count = Column(Integer, default=0)
    weightage_per_question = Column(Integer, nullable=False)
    pass_percentage = Column(Integer, nullable=False)
    max_attempts = Column(Integer, nullable=False)
    ai_suggested_time = Column(Integer, nullable=False)
    trainer_time_override = Column(Integer, nullable=True)
    is_approved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    course = relationship("Course")


class AssessmentQuestion(Base):
    __tablename__ = "assessment_questions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    course_id = Column(String, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    module_index = Column(Integer, nullable=True)
    source_doc_name = Column(String, nullable=True)
    question_type = Column(String, nullable=False)
    question_text = Column(Text, nullable=False)
    correct_answer = Column(Text, nullable=True)
    option_a = Column(String, nullable=True)
    option_b = Column(String, nullable=True)
    option_c = Column(String, nullable=True)
    option_d = Column(String, nullable=True)
    explanation = Column(Text, nullable=True)
    is_approved = Column(Boolean, default=False)
    is_rejected = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    course = relationship("Course")


class TraineeAssessment(Base):
    __tablename__ = "trainee_assessments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    trainee_id = Column(String, ForeignKey("users.id"), nullable=False)
    course_id = Column(String, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    module_index = Column(Integer, nullable=True)
    attempt_number = Column(Integer, nullable=False)
    questions_data = Column(Text, nullable=False)
    score = Column(Integer, nullable=False)
    total_possible = Column(Integer, nullable=False)
    percentage = Column(Integer, nullable=False)
    passed = Column(Boolean, nullable=False)
    time_taken = Column(Integer, nullable=False)
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    trainee = relationship("User")
    course = relationship("Course")


class TraineeQuestionSet(Base):
    __tablename__ = "trainee_question_sets"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    trainee_id = Column(String, ForeignKey("users.id"), nullable=False)
    course_id = Column(String, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    module_index = Column(Integer, nullable=True)
    attempt_number = Column(Integer, nullable=False, default=1)
    questions_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    trainee = relationship("User")
    course = relationship("Course")


class AdditionalAttemptRequest(Base):
    __tablename__ = "additional_attempt_requests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    trainee_id = Column(String, ForeignKey("users.id"), nullable=False)
    course_id = Column(String, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    module_index = Column(Integer, nullable=True)
    reason = Column(Text, nullable=False)
    status = Column(String, default="pending")
    # How many extra attempts the approval granted. The attempt caps are computed by counting the
    # attempts actually stored, so the grant has to be recorded here to be counted at all: it used
    # to live only in TraineeAttempt.attempts_left, a course-level column that /admin/reset-attempts
    # and /admin/sync-course-attempts overwrite. NULL on rows approved before this column existed,
    # which reads as the one attempt a row has always been worth.
    granted_attempts = Column(Integer, nullable=True)
    requested_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    trainer_notes = Column(Text, nullable=True)
    trainee = relationship("User", foreign_keys=[trainee_id])
    course = relationship("Course")
    reviewer = relationship("User", foreign_keys=[reviewed_by])


class VoiceScenario(Base):
    __tablename__ = "voice_scenarios"

    id = Column(String, primary_key=True)
    author_id = Column(String, ForeignKey("users.id"), nullable=False)
    process_id = Column(String, ForeignKey("processes.id"), nullable=True)
    category = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=lambda: now_ist(), nullable=False)
    author = relationship("User")
    process = relationship("Process")


class VoiceSession(Base):
    __tablename__ = "voice_sessions"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    scenario_id = Column(String, ForeignKey("voice_scenarios.id"), nullable=False)
    mode = Column(String, nullable=False)
    content = Column(JSON, nullable=False)
    status = Column(String, default="active", nullable=False)
    created_at = Column(DateTime, default=lambda: now_ist(), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: now_ist(),
        onupdate=lambda: now_ist(),
        nullable=False,
    )
    user = relationship("User")
    scenario = relationship("VoiceScenario")


class VoiceAssessment(Base):
    __tablename__ = "voice_assessments"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    session_id = Column(String, ForeignKey("voice_sessions.id"), nullable=False)
    scenario_id = Column(String, ForeignKey("voice_scenarios.id"), nullable=False)
    content = Column(JSON, nullable=False)
    pdf_s3_key = Column(String, nullable=True)
    final_percentage = Column(Integer, default=0, nullable=False)
    grade = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: now_ist(), nullable=False)
    user = relationship("User")
    session = relationship("VoiceSession")
    scenario = relationship("VoiceScenario")


__all__ = [
    "Process",
    "User",
    "Course",
    "TraineeAttempt",
    "CourseProgress",
    "ChatSession",
    "ChatMessage",
    "TutorResponseCache",
    "TokenUsage",
    "SystemSettings",
    "SharedReport",
    "SharedReportEdit",
    "SharedCourse",
    "SharedCourseEdit",
    "Notification",
    "ActivityLog",
    "VideoAnalysisHistory",
    "RateLimitSetting",
    "TraineeBatch",
    "BatchMember",
    "UserSession",
    "JobStatus",
    "AssessmentConfig",
    "AssessmentQuestion",
    "TraineeAssessment",
    "TraineeQuestionSet",
    "AdditionalAttemptRequest",
    "VoiceScenario",
    "VoiceSession",
    "VoiceAssessment",
]
