from __future__ import annotations

from pydantic import field_validator, model_validator

from core.runtime import *
from utils.initial_login import MAX_BULK_USERS, initial_login_for, safe_employee_id
from utils.input_validation import (
    ALL_ROLES,
    ASSESSMENT_SCOPES,
    MAX_QUERY_LENGTH,
    MAX_REASON_LENGTH,
    MAX_TITLE_LENGTH,
    PUBLICATION_TYPES,
    QUESTION_REVIEW_ACTIONS,
    QUESTION_TYPES,
    SIGNUP_ROLES,
    normalize_email,
    safe_answer_map,
    safe_choice,
    safe_description,
    safe_filename,
    safe_free_text,
    safe_identifier,
    safe_identifier_list,
    safe_int,
    safe_name,
    safe_optional_description,
    safe_optional_free_text,
    safe_optional_identifier,
    safe_optional_identifier_list,
    safe_optional_int,
    safe_optional_name,
    safe_password,
    safe_registration_email,
    safe_session_id,
)


class ProcessModel(BaseModel):
    id: str
    name: str
    model_config = ConfigDict(from_attributes=True)


class ProcessCreate(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return safe_name(value, field="Process name")


class UserBase(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        """
        Lower-case the whole address so casing never splits an account.

        `EmailStr` only lower-cases the domain, so `Phani@st.com` would arrive with its
        local part intact and never match a `phani@st.com` row on the exact-match user
        lookup. Normalizing here covers every model that inherits `UserBase`
        (`UserCreate`, `AdminUserCreate`, `UserInDB`), so signup, admin-create and the
        responses all agree on one canonical form.
        """
        normalized = normalize_email(value)
        if not normalized:
            raise ValueError("Email is required.")
        return normalized


class UserCreate(UserBase):
    password: str
    process_id: str
    role: str = "trainee"

    @field_validator("email")
    @classmethod
    def _validate_registration_email(cls, value: str) -> str:
        """
        Restrict *self-registration* to company domains (VAPT #12).

        Declared on `UserCreate`, not on `UserBase`, because `UserBase` is also the base of
        `UserInDB`, which serializes EXISTING users - validating there would make every
        response for an account already registered on another domain fail. Sign-in
        (`/auth/token`) does not use this model at all, so existing users on any domain keep
        logging in exactly as before.

        `UserBase._normalize_email` has already run by the time this is called (base-class
        validators run first), so `value` arrives canonical.
        """
        return safe_registration_email(value)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return safe_password(value)

    @field_validator("process_id")
    @classmethod
    def _validate_process_id(cls, value: str) -> str:
        return safe_identifier(value, field="Process ID")

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        return safe_choice(value, SIGNUP_ROLES, field="Role")


class AdminUserCreate(UserBase):
    # The Create User form sends employee_id, and the account starts on St@<EmployeeID>#2026
    # (utils.initial_login). An explicit password is still accepted from API callers. Either
    # way the account must change it at first login.
    employee_id: Optional[str] = None
    password: Optional[str] = None
    process_id: str
    role: str = "trainee"
    is_approved: bool = True

    @field_validator("email")
    @classmethod
    def _validate_registration_email(cls, value: str) -> str:
        """
        The super-admin Create User panel is held to the same domain policy (VAPT #12).

        Otherwise the finding would remain reachable through the admin path: an operator
        could still provision a throwaway address, which is exactly the "fraudulent or
        anonymous account creation" the finding describes. Same reasoning as the shared
        password validator - an admin should not be able to create an account a user could
        not create themselves.
        """
        return safe_registration_email(value)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return safe_password(value)

    # "before": an API caller may send a numeric ID, which the str type would reject first.
    @field_validator("employee_id", mode="before")
    @classmethod
    def _validate_employee_id(cls, value: Any) -> Optional[str]:
        return None if value in (None, "") else safe_employee_id(value)

    @field_validator("process_id")
    @classmethod
    def _validate_process_id(cls, value: str) -> str:
        return safe_identifier(value, field="Process ID")

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        return safe_choice(value, ALL_ROLES, field="Role")

    @model_validator(mode="after")
    def _initial_login(self) -> "AdminUserCreate":
        if self.employee_id:
            self.password = initial_login_for(self.employee_id)
        if not self.password:
            raise ValueError("Employee ID is required.")
        return self


class BulkUserRowIn(BaseModel):
    """One reviewed row from the bulk-create table. Content rules are applied server-side by
    services.bulk_users.validate_rows, exactly as for an uploaded file; this only bounds sizes."""

    row: int = Field(ge=1, le=100000)
    email: str = Field(default="", max_length=320)
    employee_id: str = Field(default="", max_length=64)
    role: str = Field(default="", max_length=40)
    process: str = Field(default="", max_length=200)


class BulkUsersRowsRequest(BaseModel):
    rows: List[BulkUserRowIn] = Field(min_length=1, max_length=MAX_BULK_USERS)
    is_approved: bool = True


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("current_password")
    @classmethod
    def _validate_current_password(cls, value: str) -> str:
        # Only bounded here: it is compared against the stored hash, never stored itself.
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("Enter your current password.")
        return value

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, value: str) -> str:
        return safe_password(value, field="New password")


class UserInDB(UserBase):
    id: str
    role: str
    is_approved: bool
    # True until an admin-created account replaces its starting login; the front end shows the
    # change-password screen instead of the app while it is set.
    must_change_password: bool = False
    process: Optional[ProcessModel] = None
    model_config = ConfigDict(from_attributes=True)


class UserSimple(BaseModel):
    id: str
    email: EmailStr
    role: str


class ReportModule(BaseModel):
    report_id: str
    doc_name: str
    seg_idx: int
    title: str
    edited_content: str
    document_order: int = 0
    deleted_seg_indices: Optional[List[int]] = None
    # The filename the user uploaded, for display in the curation UI. doc_name above is the
    # randomized storage name (VAPT #4: Double Extension File Upload) and remains the join
    # key for S3 objects and module metadata. Never persist this as a key.
    doc_display_name: Optional[str] = None


class ReportUpdateRequest(BaseModel):
    new_content: str

    @field_validator("new_content")
    @classmethod
    def _validate_new_content(cls, value: str) -> str:
        return safe_free_text(value, field="Module content")


class ModuleTitleUpdateRequest(BaseModel):
    new_title: str

    @field_validator("new_title")
    @classmethod
    def _validate_new_title(cls, value: str) -> str:
        return safe_name(value, field="Module title", max_length=MAX_TITLE_LENGTH)


class RegenerationRequest(BaseModel):
    source_doc_name: str
    follow_up_prompt: str
    course_id: Optional[str] = None

    @field_validator("source_doc_name")
    @classmethod
    def _validate_source_doc_name(cls, value: str) -> str:
        return safe_filename(value, field="Source document name")

    @field_validator("follow_up_prompt")
    @classmethod
    def _validate_follow_up_prompt(cls, value: str) -> str:
        return safe_free_text(
            value, field="Follow-up prompt", max_length=MAX_QUERY_LENGTH, min_length=1
        )

    @field_validator("course_id")
    @classmethod
    def _validate_course_id(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_identifier(value, field="Course ID")


class ProcessResponse(BaseModel):
    job_id: str
    message: str
    generated_reports: List[ReportModule]
    source_doc_name: Optional[str] = None
    deleted_seg_indices: Optional[List[int]] = None


class JobStatusResponse(BaseModel):
    status: str
    progress_message: str
    result: Optional[dict] = None
    error: Optional[str] = None


class CommitResponse(BaseModel):
    message: str
    indexed_chunk_count: int
    source_documents_added: List[str]


class ReportInfo(BaseModel):
    filename: str
    title: str
    seg_index: int
    content: Optional[str] = None
    is_completed: Optional[bool] = False
    course_id: Optional[str] = None
    image_count: Optional[int] = 0
    document_order: Optional[int] = 0


class GroupedReport(BaseModel):
    source_doc: str
    is_published: bool
    reports: List[ReportInfo]
    course_id: Optional[str] = None
    question_count: Optional[int] = 0
    rejected_question_count: Optional[int] = 0
    assessment_configured: Optional[bool] = False
    course_title: Optional[str] = None
    source_documents: Optional[List[str]] = None
    # Display counterparts of source_documents, same order. See ReportModule.doc_display_name.
    source_document_display_names: Optional[List[str]] = None


class KBSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=DEFAULT_RAG_TOP_K, gt=0)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        return safe_free_text(
            value, field="Query", max_length=MAX_QUERY_LENGTH, min_length=1
        )

    @field_validator("top_k")
    @classmethod
    def _validate_top_k(cls, value: int) -> int:
        return safe_int(value, field="Top K", minimum=1, maximum=100)


class Token(BaseModel):
    access_token: str
    token_type: str
    expires_in: Optional[int] = None  # seconds until JWT expiry (client idle helpers)


class ChatMessageModel(BaseModel):
    role: str
    content: str
    timestamp: datetime
    sources: Optional[str] = None
    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)


class SourceModel(BaseModel):
    content: str
    source: str


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    history: List[ChatMessageModel]
    sources: Optional[List[SourceModel]] = None


class ChatSessionModel(BaseModel):
    id: str
    session_name: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ChatSessionWithHistory(ChatSessionModel):
    messages: List[ChatMessageModel] = []


class SessionRenameRequest(BaseModel):
    new_name: str

    @field_validator("new_name")
    @classmethod
    def _validate_new_name(cls, value: str) -> str:
        return safe_name(value, field="Session name", max_length=MAX_TITLE_LENGTH)


class ChatRequest(BaseModel):
    query: str
    session_id: str
    session_name: Optional[str] = None
    context: Optional[str] = None
    target_user_id: Optional[str] = None
    module_title: Optional[str] = None

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        return safe_free_text(value, field="Query", max_length=MAX_QUERY_LENGTH)

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        # Not `safe_identifier`: a tutor raise-hand key is a composite of two UUIDs, the
        # course name and the module number, so the 64-character identifier ceiling
        # rejected every raise-hand question with a 422. Charset gate is identical.
        return safe_session_id(value, field="Session ID")

    @field_validator("session_name")
    @classmethod
    def _validate_session_name(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_name(
            value, field="Session name", max_length=MAX_TITLE_LENGTH
        )

    @field_validator("context")
    @classmethod
    def _validate_context(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_free_text(value, field="Context")

    @field_validator("target_user_id")
    @classmethod
    def _validate_target_user_id(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_identifier(value, field="Target user ID")

    @field_validator("module_title")
    @classmethod
    def _validate_module_title(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_name(
            value, field="Module title", max_length=MAX_TITLE_LENGTH
        )


class ShareRequest(BaseModel):
    source_doc_name: str
    recipient_id: str
    reason: Optional[str] = None

    @field_validator("source_doc_name")
    @classmethod
    def _validate_source_doc_name(cls, value: str) -> str:
        return safe_filename(value, field="Source document name")

    @field_validator("recipient_id")
    @classmethod
    def _validate_recipient_id(cls, value: str) -> str:
        return safe_identifier(value, field="Recipient ID")

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_description(
            value, field="Reason", max_length=MAX_REASON_LENGTH
        )


class PublishCourseRequest(BaseModel):
    source_doc_name: str
    target_process_id: Optional[str] = None

    @field_validator("source_doc_name")
    @classmethod
    def _validate_source_doc_name(cls, value: str) -> str:
        return safe_filename(value, field="Source document name")

    @field_validator("target_process_id")
    @classmethod
    def _validate_target_process_id(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_identifier(value, field="Target process ID")


class AdminShareRequest(ShareRequest):
    owner_id: str

    @field_validator("owner_id")
    @classmethod
    def _validate_owner_id(cls, value: str) -> str:
        return safe_identifier(value, field="Owner ID")


class NotificationModel(BaseModel):
    id: str
    message: str
    related_link: Optional[str] = None
    created_at: datetime
    is_read: bool
    model_config = ConfigDict(from_attributes=True)


class SharedReportInfo(BaseModel):
    id: str
    source_doc_name: str
    sharer_email: str
    shared_at: datetime
    status: str
    feedback: Optional[str] = None
    author_id: str
    reason: Optional[str] = None


class ShareCourseRequest(BaseModel):
    course_id: str
    recipient_id: str
    reason: Optional[str] = None

    @field_validator("course_id")
    @classmethod
    def _validate_course_id(cls, value: str) -> str:
        return safe_identifier(value, field="Course ID")

    @field_validator("recipient_id")
    @classmethod
    def _validate_recipient_id(cls, value: str) -> str:
        return safe_identifier(value, field="Recipient ID")

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_description(
            value, field="Reason", max_length=MAX_REASON_LENGTH
        )


class SharedCourseInfo(BaseModel):
    id: str
    course_id: str
    course_name: str
    course_title: Optional[str] = None
    sharer_email: str
    shared_at: datetime
    status: str
    feedback: Optional[str] = None
    reason: Optional[str] = None
    author_id: str


class FeedbackRequest(BaseModel):
    feedback: str

    @field_validator("feedback")
    @classmethod
    def _validate_feedback(cls, value: str) -> str:
        return safe_description(value, field="Feedback", max_length=MAX_REASON_LENGTH)


class EditProposalRequest(BaseModel):
    report_filename: str
    proposed_content: str

    @field_validator("report_filename")
    @classmethod
    def _validate_report_filename(cls, value: str) -> str:
        return safe_filename(value, field="Report file name")

    @field_validator("proposed_content")
    @classmethod
    def _validate_proposed_content(cls, value: str) -> str:
        return safe_free_text(value, field="Proposed content")


class EditProposalInfo(BaseModel):
    id: str
    report_filename: str
    module_title: Optional[str] = None
    editor_email: str
    created_at: datetime
    status: str
    original_content: str
    proposed_content: str
    edit_type: str


class SynthesizeRequest(BaseModel):
    text: str
    voiceId: str = "Matthew"

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        # Polly hard-limits a synthesis request; cap before the AWS call.
        return safe_free_text(value, field="Text", max_length=6000, min_length=1)

    @field_validator("voiceId")
    @classmethod
    def _validate_voice_id(cls, value: str) -> str:
        # Polly voice ids are alphanumeric; allow-list the charset, AWS validates
        # the specific voice.
        return safe_identifier(value, field="Voice ID", max_length=40)


class VoiceAskResponse(BaseModel):
    transcribed_query: str
    answer: str
    history: List[ChatMessageModel]
    sources: Optional[List[SourceModel]] = None


class TutorLectureRequest(BaseModel):
    module_content: str
    course_id: Optional[str] = None
    module_index: Optional[int] = None

    @field_validator("module_content")
    @classmethod
    def _validate_module_content(cls, value: str) -> str:
        return safe_free_text(value, field="Module content")

    @field_validator("course_id")
    @classmethod
    def _validate_course_id(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_identifier(value, field="Course ID")

    @field_validator("module_index")
    @classmethod
    def _validate_module_index(cls, value: Optional[int]) -> Optional[int]:
        return safe_optional_int(value, field="Module index", minimum=0, maximum=10_000)


class ProgressUpdateRequest(BaseModel):
    course_id: str
    module_index: int

    @field_validator("course_id")
    @classmethod
    def _validate_course_id(cls, value: str) -> str:
        return safe_identifier(value, field="Course ID")

    @field_validator("module_index")
    @classmethod
    def _validate_module_index(cls, value: int) -> int:
        return safe_int(value, field="Module index", minimum=0, maximum=10_000)


class TraineeCourseInfo(BaseModel):
    course_id: str
    source_doc_name: str
    course_title: Optional[str] = None
    author_email: str
    attempts_left: int
    author_id: str
    total_modules: int
    completed_modules: int
    is_published: bool
    assessment_configured: bool = False
    assessment_scope: Optional[str] = None
    max_attempts_per_module: Optional[int] = None
    pass_percentage: Optional[int] = None
    total_questions: Optional[int] = None
    course_completed: bool = False
    assessment_status: Optional[str] = None
    assessment_score: Optional[float] = None
    module_assessment_details: Optional[list] = []
    assessment_attempts_used: Optional[int] = None
    course_created_at: Optional[datetime] = None


class TrainerTraineeInfo(BaseModel):
    trainee_email: str
    course_name: str
    course_title: Optional[str] = None
    attempts_left: int
    total_modules: int
    completed_modules: int
    assessment_scope: Optional[str] = None
    max_attempts_per_module: Optional[int] = None
    course_completed: bool = False
    assessment_status: Optional[str] = None


class ActivityLogInfo(BaseModel):
    user_email: str
    user_role: str
    action: str
    details: Optional[str] = None
    timestamp: datetime


class TraineeStats(BaseModel):
    courses_assigned: int
    courses_completed: int
    courses_in_progress: int
    courses_not_started: int
    total_attempts_left: int
    total_modules_completed: int
    total_modules_assigned: int


class SystemStats(BaseModel):
    total_users: int
    total_trainers: int
    total_trainees: int
    total_courses: int
    total_modules: int = 0
    total_shares: int = 0
    pending_approvals: int = 0
    total_batches: int = 0
    total_processes: int = 0
    active_trainees_30d: int = 0
    shares_by_type: dict = {}
    shares_by_status: dict = {}
    shares_by_process: list = []
    top_shared_courses: list = []
    activity_by_day: list = []
    user_growth_by_month: list = []
    course_completion_rate: float = 0.0
    total_assessments_passed: int = 0
    total_assessments_failed: int = 0


class RateLimitModel(BaseModel):
    role: str
    limit: str
    model_config = ConfigDict(from_attributes=True)


class QuestionGenerationRequest(BaseModel):
    module_content: str

    @field_validator("module_content")
    @classmethod
    def _validate_module_content(cls, value: str) -> str:
        return safe_free_text(value, field="Module content")


class AnswerValidationRequest(BaseModel):
    module_content: str
    question: str
    trainee_answer: str

    @field_validator("module_content")
    @classmethod
    def _validate_module_content(cls, value: str) -> str:
        return safe_free_text(value, field="Module content")

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        return safe_free_text(value, field="Question", max_length=MAX_QUERY_LENGTH)

    @field_validator("trainee_answer")
    @classmethod
    def _validate_trainee_answer(cls, value: str) -> str:
        return safe_free_text(value, field="Answer", max_length=MAX_QUERY_LENGTH)


class AnswerValidationResponse(BaseModel):
    is_correct: bool
    explanation: str


class CreateBatchRequest(BaseModel):
    batch_name: str
    batch_description: str = ""
    trainee_ids: List[str] = []

    @field_validator("batch_name")
    @classmethod
    def _validate_batch_name(cls, value: str) -> str:
        return safe_name(value, field="Batch name")

    @field_validator("batch_description")
    @classmethod
    def _validate_batch_description(cls, value: str) -> str:
        return safe_description(value, field="Batch description")

    @field_validator("trainee_ids")
    @classmethod
    def _validate_trainee_ids(cls, value: List[str]) -> List[str]:
        return safe_identifier_list(value, field="Trainee IDs")


class UpdateBatchRequest(BaseModel):
    batch_name: Optional[str] = None
    batch_description: Optional[str] = None
    trainee_ids: Optional[List[str]] = None

    @field_validator("batch_name")
    @classmethod
    def _validate_batch_name(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_name(value, field="Batch name")

    @field_validator("batch_description")
    @classmethod
    def _validate_batch_description(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_description(value, field="Batch description")

    @field_validator("trainee_ids")
    @classmethod
    def _validate_trainee_ids(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return safe_optional_identifier_list(value, field="Trainee IDs")


class EnhancedPublishCourseRequest(BaseModel):
    source_doc_name: str
    publication_type: str
    batch_id: Optional[str] = None
    batch_ids: Optional[List[str]] = None

    @field_validator("source_doc_name")
    @classmethod
    def _validate_source_doc_name(cls, value: str) -> str:
        return safe_filename(value, field="Source document name")

    @field_validator("publication_type")
    @classmethod
    def _validate_publication_type(cls, value: str) -> str:
        return safe_choice(value, PUBLICATION_TYPES, field="Publication type")

    @field_validator("batch_id")
    @classmethod
    def _validate_batch_id(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_identifier(value, field="Batch ID")

    @field_validator("batch_ids")
    @classmethod
    def _validate_batch_ids(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return safe_optional_identifier_list(value, field="Batch IDs")


class AddTraineeToCourseRequest(BaseModel):
    course_id: str
    trainee_ids: List[str]

    @field_validator("course_id")
    @classmethod
    def _validate_course_id(cls, value: str) -> str:
        return safe_identifier(value, field="Course ID")

    @field_validator("trainee_ids")
    @classmethod
    def _validate_trainee_ids(cls, value: List[str]) -> List[str]:
        return safe_identifier_list(value, field="Trainee IDs")


class RegenerateQuestionRequest(BaseModel):
    question_id: str
    custom_prompt: Optional[str] = None

    @field_validator("question_id")
    @classmethod
    def _validate_question_id(cls, value: str) -> str:
        return safe_identifier(value, field="Question ID")

    @field_validator("custom_prompt")
    @classmethod
    def _validate_custom_prompt(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_free_text(
            value, field="Custom prompt", max_length=MAX_QUERY_LENGTH
        )


class AssessmentConfigRequest(BaseModel):
    assessment_scope: str
    question_type: str
    total_questions: int
    mcq_count: Optional[int] = 0
    subjective_count: Optional[int] = 0
    weightage_per_question: int
    pass_percentage: int
    max_attempts: int
    trainer_time_override: Optional[int] = None

    @field_validator("assessment_scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        return safe_choice(value, ASSESSMENT_SCOPES, field="Assessment scope")

    @field_validator("question_type")
    @classmethod
    def _validate_question_type(cls, value: str) -> str:
        return safe_choice(value, QUESTION_TYPES, field="Question type")

    @field_validator("total_questions")
    @classmethod
    def _validate_total_questions(cls, value: int) -> int:
        return safe_int(value, field="Total questions", minimum=1, maximum=500)

    @field_validator("mcq_count", "subjective_count")
    @classmethod
    def _validate_counts(cls, value: Optional[int]) -> Optional[int]:
        return safe_optional_int(value, field="Question count", minimum=0, maximum=500)

    @field_validator("weightage_per_question")
    @classmethod
    def _validate_weightage(cls, value: int) -> int:
        return safe_int(value, field="Weightage per question", minimum=1, maximum=1000)

    @field_validator("pass_percentage")
    @classmethod
    def _validate_pass_percentage(cls, value: int) -> int:
        return safe_int(value, field="Pass percentage", minimum=1, maximum=100)

    @field_validator("max_attempts")
    @classmethod
    def _validate_max_attempts(cls, value: int) -> int:
        return safe_int(value, field="Maximum attempts", minimum=1, maximum=100)

    @field_validator("trainer_time_override")
    @classmethod
    def _validate_time_override(cls, value: Optional[int]) -> Optional[int]:
        return safe_optional_int(
            value, field="Trainer time override", minimum=1, maximum=1440
        )


class QuestionReviewRequest(BaseModel):
    action: str
    question_text: Optional[str] = None
    correct_answer: Optional[str] = None
    option_a: Optional[str] = None
    option_b: Optional[str] = None
    option_c: Optional[str] = None
    option_d: Optional[str] = None
    explanation: Optional[str] = None

    @field_validator("action")
    @classmethod
    def _validate_action(cls, value: str) -> str:
        return safe_choice(value, QUESTION_REVIEW_ACTIONS, field="Action")

    @field_validator(
        "question_text",
        "correct_answer",
        "option_a",
        "option_b",
        "option_c",
        "option_d",
        "explanation",
    )
    @classmethod
    def _validate_question_fields(cls, value: Optional[str]) -> Optional[str]:
        return safe_optional_free_text(
            value, field="Question field", max_length=MAX_QUERY_LENGTH
        )


class AssessmentSubmissionRequest(BaseModel):
    answers: Dict[str, str]
    time_taken: int

    @field_validator("answers")
    @classmethod
    def _validate_answers(cls, value: Dict[str, str]) -> Dict[str, str]:
        return safe_answer_map(value, field="Answers")

    @field_validator("time_taken")
    @classmethod
    def _validate_time_taken(cls, value: int) -> int:
        # Seconds; capped at 24h so a forged value cannot skew analytics.
        return safe_int(value, field="Time taken", minimum=0, maximum=86_400)


class AssessmentResponse(BaseModel):
    id: str
    questions: List[Dict]
    time_limit: int
    attempt_number: int
    max_attempts: int


class AdditionalAttemptRequestModel(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        return safe_description(value, field="Reason", max_length=MAX_REASON_LENGTH)


class ApproveAttemptRequestModel(BaseModel):
    additional_attempts: int

    @field_validator("additional_attempts")
    @classmethod
    def _validate_additional_attempts(cls, value: int) -> int:
        return safe_int(value, field="Additional attempts", minimum=1, maximum=100)


class DenyAttemptRequestModel(BaseModel):
    denial_reason: str

    @field_validator("denial_reason")
    @classmethod
    def _validate_denial_reason(cls, value: str) -> str:
        return safe_description(
            value, field="Denial reason", max_length=MAX_REASON_LENGTH
        )


class AdditionalAttemptRequestResponse(BaseModel):
    id: str
    trainee_email: str
    course_name: str
    module_index: Optional[int]
    reason: str
    status: str
    requested_at: datetime
    reviewed_at: Optional[datetime]
    trainer_notes: Optional[str]


class ResumeAssessmentResponse(BaseModel):
    assessment_id: str
    questions: List[Dict]
    partial_answers: Dict[str, str]
    time_remaining: int
    attempt_number: int


__all__ = [name for name in globals() if not name.startswith("__")]
