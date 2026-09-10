"""TraceX API entrypoint: FastAPI application assembly.

Run locally with `uv run uvicorn app.main:app --reload`, or via
`docker compose up`. Settings are loaded once, at import time, so a
misconfigured deployment fails fast on startup rather than on first request.
"""

from __future__ import annotations

import logging
import sys

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.errors import (
    RequestIDMiddleware,
    http_exception_handler,
    request_validation_exception_handler,
    unhandled_exception_handler,
)
from app.modules.access_control.api import SecurityHeadersMiddleware
from app.modules.access_control.api import router as auth_router


def _configure_logging(log_level: str) -> None:
    """Structured JSON logging to stdout, correlation-ID aware."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)

    application = FastAPI(title=settings.app_name, version="0.1.0")
    application.add_middleware(RequestIDMiddleware)
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.include_router(health_router)
    application.include_router(auth_router)
    return application


app = create_app()
