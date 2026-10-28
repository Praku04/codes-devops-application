from dataclasses import dataclass
from logging import Logger
from typing import Any, Callable


@dataclass(frozen=True)
class VoiceTrainingDependencies:
    logger: Logger
    get_current_user: Callable[..., Any]
    get_db: Callable[..., Any]
    log_activity: Callable[..., Any]
    user_model: Any
    process_model: Any
    scenario_model: Any
    session_model: Any
    assessment_model: Any
    s3_client: Any
    s3_bucket_name: str
    bedrock_client: Any
    generation_model: str
    polly_client: Any
    transcribe_client: Any
