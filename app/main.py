"""TraceX FastAPI control plane."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.analysis_routes import router as analysis_router
from app.api.analytics_routes import router as analytics_router
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
    # The Vite dev server; extra origins (e.g. a LAN hostname) via TRACEX_CORS_ORIGINS.
    allow_origins=["http://localhost:5173", *filter(None, os.environ.get("TRACEX_CORS_ORIGINS", "").split(","))],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Findings/graph/export payloads are large, highly repetitive JSON; compressing
# them cuts transfer time far more than it costs to deflate. SSE is excluded by
# Starlette's GZip middleware for text/event-stream.
app.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=5)
app.include_router(router)
app.include_router(analytics_router)
app.include_router(analysis_router)


def _mount_web_ui(directory: str | None) -> None:
    """Serve the built React app from the same origin (the offline appliance).

    Set TRACEX_WEB_DIR to a `vite build` output directory. Files are served as-is;
    any other non-API path returns index.html so client-side routes (e.g.
    /cases/<id>/graph) survive a page reload. API routes keep priority because
    they are registered first.
    """
    if not directory:
        return
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    root = Path(directory).resolve()
    index = root / "index.html"
    if not index.is_file():
        logger.warning("TRACEX_WEB_DIR=%s has no index.html; the web UI is not served", directory)
        return
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="web-assets")

    from fastapi import HTTPException

    @app.api_route("/v1/{unmatched:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], include_in_schema=False)
    def missing_api(unmatched: str) -> None:
        # Without this, an unknown POST partially matches the GET SPA catch-all
        # and returns 405 instead of an unambiguous missing API resource.
        raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/{path:path}", include_in_schema=False)
    def web_ui(path: str) -> FileResponse:
        if path == "v1" or path.startswith("v1/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (root / path).resolve()
        if path and root in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


_mount_web_ui(os.environ.get("TRACEX_WEB_DIR"))
