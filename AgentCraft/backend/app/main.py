"""AgentCraft FastAPI application."""

from __future__ import annotations

import uuid             
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import ENV_FILE_ENCODING, ROOT_ENV

# Load the repo-root .env (HexaAgent/.env) into the process environment, so libraries
# reading os.environ directly — boto3 picking up AWS_* — see the same values Settings does.
# Imported from config rather than recomputed here, so there is one definition of where
# the file lives.
#
# There used to be a second load_dotenv for backend/.env. It is gone: two files meant the
# effective config depended on which one a code path happened to read, and a stale
# backend/.env silently pinned CORS to an old port. `ROOT_ENV` is the one source now.
# `encoding=` is not decoration: without it python-dotenv decodes strict UTF-8 and a single
# cp1252 byte in the file -- from an editor that saved `--` as an em dash -- killed the API here
# with a UnicodeDecodeError from dotenv/parser.py that named no file at all. config picks the
# encoding that actually reads this .env and says so on stderr when it is not UTF-8.
load_dotenv(ROOT_ENV, encoding=ENV_FILE_ENCODING)

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.exceptions import (
    AppError,
    app_error_handler,
    illegal_transition_handler,
    setup_logging,
    unhandled_exception_handler,
)
from app.core.state_machine import IllegalTransitionError
from app.db.session import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    init_db()
    yield


#: Shown at the top of /docs. Kept short deliberately — enough to tell a newcomer what the
#: API is for and how to get a token, with the detail left to the README.
API_DESCRIPTION = """
**AgentCraft Studio** turns a problem statement into IDE-ready **agents, skills and rules**
for Claude Code, Cursor and Windsurf — either from uploaded documents (a PRD, notes, a
spec) or from a guided interview when there is nothing to upload.

The same API powers the Angular UI and the `agentcraft` CLI, so both stay in step.

### Signing in

1. `POST /api/v1/auth/signup` — requests an account, which a super admin must approve.
2. Once approved, click **Authorize** above and enter that account's email as the
   **username**, with its password. Leave `client_id` and `client_secret` blank.
3. Every padlocked endpoint below is now callable as that user.

Authorize signs in through `POST /api/v1/auth/token` (the OAuth2 password flow) and keeps
the token for the rest of the page. From code, use `POST /api/v1/auth/login` instead — it
takes JSON and returns the user object alongside the token.

### Roles

| Role | Can do |
| --- | --- |
| **User** | Own workspaces only — create, generate, review, export |
| **Super admin** | Everything a user can, plus the `admin` endpoints: approve, reject, delete, restore and purge accounts, and inspect any workspace |

Endpoints under **admin** return `403 Admin access required` for a normal user, so the
padlock opening a section is not the same as being allowed to call it.
"""

#: Per-tag blurbs, so the sections in /docs say what they are for and who may use them.
API_TAGS = [
    {
        "name": "auth",
        "description": (
            "Signup, login and the current user. `signup`, `login` and `token` are the "
            "only endpoints that need no token — `token` is the one **Authorize** calls."
        ),
    },
    {
        "name": "projects",
        "description": (
            "The whole workspace lifecycle: create, upload documents or answer the "
            "interview, pick an IDE, generate the plan, review, then export. Scoped to "
            "the caller — a user never sees another user's workspaces."
        ),
    },
    {
        "name": "admin",
        "description": "**Super admin only.** User approval and the session archive.",
    },
    {
        "name": "settings",
        "description": "The Bedrock model configuration the generator runs on.",
    },
]


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AgentCraft Studio API",
        version="1.0.0",
        summary="Generate IDE agents, skills and rules from documents or an interview.",
        description=API_DESCRIPTION,
        openapi_tags=API_TAGS,
        lifespan=lifespan,
    )
    # allow_origin_regex covers the UI on FRONTEND_PORT at whatever hostname it was opened
    # from, so the same build works on localhost and on a deployed box; the explicit list
    # still carries PUBLIC_HOST and API_CORS_ORIGINS. Set API_CORS_STRICT=true to drop the
    # regex and allow only the listed origins. See config.cors_origin_regex.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(IllegalTransitionError, illegal_transition_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.include_router(api_router)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "agentcraft"}

    return app


app = create_app()
