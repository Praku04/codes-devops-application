"""Logging and exception helpers."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.state_machine import IllegalTransitionError

logger = logging.getLogger("agentcraft")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Any = None):
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    logger.exception("Unhandled error request_id=%s path=%s", request_id, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": str(exc), "request_id": request_id},
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "app_error",
            "message": exc.message,
            "details": exc.details,
            "request_id": request_id,
        },
    )


async def illegal_transition_handler(request: Request, exc: IllegalTransitionError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=409,
        content={
            "error": "illegal_transition",
            "message": str(exc),
            "action": exc.action,
            "current_state": exc.current.value,
            "allowed_from": [s.value for s in exc.allowed],
            "request_id": request_id,
        },
    )
