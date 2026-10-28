from core.runtime import (
    BEDROCK_GENERATION_MODEL,
    S3_BUCKET_NAME,
    app,
    logger,
    polly_client,
    s3_client,
    transcribe_client,
    sync_bedrock_client,
)
from features import admin_router, assessment_router, general_router, trainer_router
from models.domain import Notification, Process, User, VideoAnalysisHistory, VoiceAssessment, VoiceScenario, VoiceSession
from services import auth_platform  # noqa: F401 — startup hooks, DB seed, limiter state
from services.auth_platform import get_current_user, get_db, log_activity
from services.rate_limits import register_rate_limit_middleware
from slowapi.middleware import SlowAPIMiddleware
from utils.video_analysis import VideoAnalysisDependencies, create_video_analysis_router
from utils.voice_training import VoiceTrainingDependencies, create_voice_training_router

register_rate_limit_middleware(app)
if not getattr(app.state, "_slowapi_middleware_registered", False):
    app.add_middleware(SlowAPIMiddleware)
    app.state._slowapi_middleware_registered = True

app.include_router(general_router)
app.include_router(trainer_router)
app.include_router(assessment_router)
app.include_router(admin_router)

video_analysis_dependencies = VideoAnalysisDependencies(
    logger=logger,
    get_current_user=get_current_user,
    get_db=get_db,
    log_activity=log_activity,
    user_model=User,
    process_model=Process,
    notification_model=Notification,
    history_model=VideoAnalysisHistory,
    s3_client=s3_client,
    s3_bucket_name=S3_BUCKET_NAME,
    bedrock_client=sync_bedrock_client,
    generation_model=BEDROCK_GENERATION_MODEL,
)
app.include_router(create_video_analysis_router(video_analysis_dependencies))

voice_training_dependencies = VoiceTrainingDependencies(
    logger=logger,
    get_current_user=get_current_user,
    get_db=get_db,
    log_activity=log_activity,
    user_model=User,
    process_model=Process,
    scenario_model=VoiceScenario,
    session_model=VoiceSession,
    assessment_model=VoiceAssessment,
    s3_client=s3_client,
    s3_bucket_name=S3_BUCKET_NAME,
    bedrock_client=sync_bedrock_client,
    generation_model=BEDROCK_GENERATION_MODEL,
    polly_client=polly_client,
    transcribe_client=transcribe_client,
)
app.include_router(create_voice_training_router(voice_training_dependencies))


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting FastAPI development server with Uvicorn.")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_keep_alive=3600,
        h11_max_incomplete_event_size=None,
    )
