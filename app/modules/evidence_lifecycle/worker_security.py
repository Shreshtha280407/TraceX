"""Transport and payload controls for authenticated worker-control routes."""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings
from app.core.errors import ErrorCode, error_body

_PREFIX = "/api/v1/internal/worker-jobs"


class WorkerControlPlaneGuardMiddleware:
    """Reject insecure or oversized worker control requests before parsing.

    ``X-Forwarded-Proto`` is consulted only when the immediate peer is an
    operator-configured trusted proxy.  A missing/invalid content length is
    left to FastAPI's typed validation; a declared oversized body is rejected
    without reading it.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self._secure_required = settings.worker_secure_transport_required
        self._max_bytes = settings.worker_internal_request_max_bytes
        self._trusted_proxies = {
            value.strip() for value in settings.worker_trusted_proxy_ips.split(",") if value.strip()
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(_PREFIX):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if self._secure_required and not self._is_secure(scope, headers):
            response = JSONResponse(
                status_code=403,
                content=error_body(ErrorCode.FORBIDDEN, "secure worker transport required", ""),
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                oversized = int(content_length) > self._max_bytes
            except ValueError:
                oversized = True
            if oversized:
                response = JSONResponse(
                    status_code=413,
                    content=error_body(
                        ErrorCode.VALIDATION_ERROR, "worker request payload too large", ""
                    ),
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _is_secure(self, scope: Scope, headers: Headers) -> bool:
        if scope.get("scheme") == "https":
            return True
        client = scope.get("client")
        client_host = client[0] if client else None
        return client_host in self._trusted_proxies and headers.get("x-forwarded-proto") == "https"
