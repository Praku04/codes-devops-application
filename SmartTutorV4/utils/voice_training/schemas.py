from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


from utils.input_validation import (
    MAX_QUERY_LENGTH,
    MAX_TITLE_LENGTH,
    PUBLICATION_TYPES,
    coerce_safe_text,
    safe_choice,
    safe_identifier,
    safe_identifier_list,
    safe_optional_free_text,
    safe_optional_name,
)
from utils.timezone_helpers import now_ist


def _ist_now() -> datetime:
    return now_ist()


class SessionMode(str, Enum):
    PRACTICE = "practice"
    ASSESSMENT = "assessment"


class ScenarioCategory(str, Enum):
    BILLING_DISPUTES = "billing_disputes"
    TECHNICAL_TROUBLESHOOTING = "technical_troubleshooting"
    ESCALATIONS = "escalations"
    ACCOUNT_ACTIVATION = "account_activation"
    CUSTOM = "custom"


class SentimentLevel(str, Enum):
    FRUSTRATED = "frustrated"
    NEUTRAL = "neutral"
    SATISFIED = "satisfied"


class ConversationTurn(BaseModel):
    speaker: str
    text: str
    timestamp: datetime = Field(default_factory=_ist_now)
    audio_url: Optional[str] = None

    # Clamped rather than rejected: this model also wraps generated script turns
    # and stored sessions being rehydrated. See coerce_safe_text.
    @field_validator("speaker")
    @classmethod
    def _validate_speaker(cls, value: str) -> str:
        return coerce_safe_text(
            value, max_length=80, allow_newlines=False, fallback="customer"
        )

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return coerce_safe_text(value, max_length=MAX_QUERY_LENGTH)


class SOPActivity(BaseModel):
    id: str
    name: str
    description: str
    required: bool = True
    completed: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return safe_identifier(value, field="Activity ID")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return coerce_safe_text(
            value, max_length=MAX_TITLE_LENGTH, allow_newlines=False, fallback="Activity"
        )

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return coerce_safe_text(value, max_length=MAX_QUERY_LENGTH)


class ScenarioScript(BaseModel):
    id: str
    category: ScenarioCategory
    title: str
    description: str
    customer_persona: str
    success_script: list[ConversationTurn]
    sop_activities: list[SOPActivity]
    opening_sentiment: SentimentLevel = SentimentLevel.FRUSTRATED
    generated_at: datetime = Field(default_factory=_ist_now)
    custom_category_label: str | None = None
    custom_use_case: str | None = None
    source_documents: list[str] = Field(default_factory=list)
    archived: bool = False
    archived_at: datetime | None = None
    script_editor_preserved: bool = False


class MetricScore(BaseModel):
    category: str
    parameter: str
    score: float = Field(ge=1, le=5)
    max_score: float = 5.0
    weight: float = 0.0
    weighted_score: float = 0.0
    remarks: str = ""
    passed: bool = True


class AssessmentResult(BaseModel):
    assessment_id: str
    session_id: str
    user_id: str
    scenario_id: str
    scenario_title: str
    category: ScenarioCategory
    mode: SessionMode = SessionMode.ASSESSMENT
    created_at: datetime = Field(default_factory=_ist_now)
    metrics: list[MetricScore]
    final_score: float = Field(ge=0, le=5)
    final_percentage: float = Field(ge=0, le=100)
    grade: str = ""
    remarks: str = ""
    sop_compliance_rate: float = 0.0
    sentiment_start: SentimentLevel = SentimentLevel.NEUTRAL
    sentiment_end: SentimentLevel = SentimentLevel.NEUTRAL
    sentiment_resolved: bool = False
    fcr_achieved: bool = False
    conversation: list[ConversationTurn] = []
    pdf_url: Optional[str] = None
    metadata: dict[str, Any] = {}


class SessionState(BaseModel):
    session_id: str
    user_id: str
    scenario_id: str
    mode: SessionMode
    conversation: list[ConversationTurn] = []
    current_sentiment: SentimentLevel = SentimentLevel.NEUTRAL
    hints_enabled: bool = True
    started_at: datetime = Field(default_factory=_ist_now)


class GenerateScenarioRequest(BaseModel):
    category: ScenarioCategory | None = None
    force_regenerate: bool = False
    custom_prompt: str | None = None
    generation_mode: str = "ai_auto"
    custom_category_label: str | None = None
    custom_use_case: str | None = None
    document_context: str | None = None
    count: int = Field(default=1, ge=1, le=5)

    @field_validator("custom_prompt", "custom_use_case")
    @classmethod
    def _validate_prompt_fields(cls, value: str | None) -> str | None:
        return safe_optional_free_text(
            value, field="Scenario prompt", max_length=MAX_QUERY_LENGTH
        )

    @field_validator("generation_mode")
    @classmethod
    def _validate_generation_mode(cls, value: str) -> str:
        return safe_choice(
            value,
            ("ai_auto", "custom_prompt", "document"),
            field="Generation mode",
        )

    @field_validator("custom_category_label")
    @classmethod
    def _validate_category_label(cls, value: str | None) -> str | None:
        return safe_optional_name(value, field="Category label")

    @field_validator("document_context")
    @classmethod
    def _validate_document_context(cls, value: str | None) -> str | None:
        return safe_optional_free_text(value, field="Document context")


class PromptExamplesRequest(BaseModel):
    topic_hint: str | None = None

    @field_validator("topic_hint")
    @classmethod
    def _validate_topic_hint(cls, value: str | None) -> str | None:
        return safe_optional_name(
            value, field="Topic hint", max_length=MAX_TITLE_LENGTH
        )


class UpdateScenarioRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    customer_persona: str | None = None
    opening_sentiment: SentimentLevel | None = None
    success_script: list[ConversationTurn] | None = None
    sop_activities: list[SOPActivity] | None = None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        return safe_optional_name(value, field="Title", max_length=MAX_TITLE_LENGTH)

    @field_validator("description", "customer_persona")
    @classmethod
    def _validate_long_fields(cls, value: str | None) -> str | None:
        return safe_optional_free_text(
            value, field="Scenario detail", max_length=MAX_QUERY_LENGTH
        )


class StartSessionRequest(BaseModel):
    scenario_id: str
    mode: SessionMode

    @field_validator("scenario_id")
    @classmethod
    def _validate_scenario_id(cls, value: str) -> str:
        return safe_identifier(value, field="Scenario ID")


class PublishVoiceScenarioRequest(BaseModel):
    publication_type: str = Field(
        default="batch",
        description="all_process | batch | multi_batch",
    )
    batch_ids: list[str] = Field(default_factory=list)
    trainee_ids: list[str] = Field(default_factory=list)
    excluded_trainee_ids: list[str] = Field(default_factory=list)

    @field_validator("publication_type")
    @classmethod
    def _validate_publication_type(cls, value: str) -> str:
        return safe_choice(value, PUBLICATION_TYPES, field="Publication type")

    @field_validator("batch_ids", "trainee_ids", "excluded_trainee_ids")
    @classmethod
    def _validate_id_lists(cls, value: list[str]) -> list[str]:
        return safe_identifier_list(value, field="IDs")


class ManagePublishAudienceRequest(BaseModel):
    batch_ids: list[str] = Field(default_factory=list)
    trainee_ids: list[str] = Field(default_factory=list)
    excluded_trainee_ids: list[str] = Field(default_factory=list)

    @field_validator("batch_ids", "trainee_ids", "excluded_trainee_ids")
    @classmethod
    def _validate_id_lists(cls, value: list[str]) -> list[str]:
        return safe_identifier_list(value, field="IDs")


class BulkScenarioIdsRequest(BaseModel):
    scenario_ids: list[str] = Field(..., min_length=1, max_length=100)

    @field_validator("scenario_ids")
    @classmethod
    def _validate_scenario_ids(cls, value: list[str]) -> list[str]:
        return safe_identifier_list(value, field="Scenario IDs", max_items=100)


class BulkAssessmentIdsRequest(BaseModel):
    assessment_ids: list[str] = Field(..., min_length=1, max_length=100)

    @field_validator("assessment_ids")
    @classmethod
    def _validate_assessment_ids(cls, value: list[str]) -> list[str]:
        return safe_identifier_list(value, field="Assessment IDs", max_items=100)


class VoiceAssessmentConfigRequest(BaseModel):
    min_practice_sessions: int = Field(default=1, ge=0, le=20)
    max_assessment_attempts: int = Field(default=3, ge=1, le=20)
    pass_percentage: int = Field(default=70, ge=1, le=100)


class EvaluateRequest(BaseModel):
    session_id: str

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return safe_identifier(value, field="Session ID")


class DashboardStats(BaseModel):
    scenarios_mastered: int
    average_empathy_score: float
    sop_compliance_rate: float
    total_practice_sessions: int
    total_assessments: int
    recent_assessments: list[dict[str, Any]] = []
