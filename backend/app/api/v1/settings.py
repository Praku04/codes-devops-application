"""LLM settings endpoints.

Both require a token. The model configuration is process-wide — one setting shared by every
user's generation — so reading it is for signed-in users and changing it is admin-only.
Before this, `PATCH /llm` was reachable with no token at all: anyone who could open the port
could repoint every user's generation at another Bedrock model, which on a deployment with
`8555` exposed means an open door to someone else's inference bill.
"""

from fastapi import APIRouter, Depends

from app.core.config import get_settings, update_llm_settings
from app.core.deps import get_current_admin, get_current_user
from app.models.schemas import LlmSettingsUpdate, LlmSettingsView, UserOut

router = APIRouter(prefix="/settings")


@router.get("/llm", response_model=LlmSettingsView)
def get_llm_settings(_: UserOut = Depends(get_current_user)) -> LlmSettingsView:
    s = get_settings()
    return LlmSettingsView(
        model=s.llm_model,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
        region=s.aws_region_name,
    )


@router.patch("/llm", response_model=LlmSettingsView)
def patch_llm_settings(
    body: LlmSettingsUpdate, _: UserOut = Depends(get_current_admin)
) -> LlmSettingsView:
    s = update_llm_settings(
        model=body.model,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )
    return LlmSettingsView(
        model=s.llm_model,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
        region=s.aws_region_name,
    )
