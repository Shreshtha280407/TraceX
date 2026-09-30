"""TraceX FastAPI control plane."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # An existing database created before a nullable column was added (e.g.
    # import_jobs.total_records) must be brought forward before the first
    # request touches that model. Best-effort: a database that is not up yet
    # surfaces through /healthz instead of preventing the API from booting.
    try:
        from app.db import ensure_schema

        added = ensure_schema()
        if added:
            logger.warning("added missing columns at startup: %s", ", ".join(added))
    except Exception as error:  # noqa: BLE001
        logger.warning("schema check skipped at startup: %s", error)
    yield


app = FastAPI(title="TraceX", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Findings/graph/export payloads are large, highly repetitive JSON; compressing
# them cuts transfer time far more than it costs to deflate. SSE is excluded by
# Starlette's GZip middleware for text/event-stream.
app.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=5)
app.include_router(router)
