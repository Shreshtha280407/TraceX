"""Regression tests for the central middleware/exception-handling design in `app/core/errors.py`.

Uses a small synthetic FastAPI app -- wired with the exact same
`RequestIDMiddleware` + three exception handlers `app/main.py` registers --
so every scenario below is unambiguous and independent of the real app's
database/Redis/auth dependencies. See `docs/architecture/security-boundaries-v1.md`
for the full investigation this file's tests were written to prove.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    get_request_id,
    http_exception_handler,
    request_validation_exception_handler,
    unhandled_exception_handler,
)

_FAKE_SECRET = "hunter2-super-secret-db-password"  # noqa: S105 - test fixture, not a real credential


class _ValidateRequestBody(BaseModel):
    email: str
    password: str


def _build_synthetic_app() -> FastAPI:
    """The exact middleware/handler wiring `app/main.py` uses, on a tiny app."""
    test_app = FastAPI()
    test_app.add_middleware(RequestIDMiddleware)
    test_app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    test_app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    test_app.add_exception_handler(Exception, unhandled_exception_handler)

    @test_app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @test_app.get("/boom-http")
    async def boom_http() -> None:
        raise HTTPException(status_code=status.HTTP_418_IM_A_TEAPOT, detail="short and stout")

    @test_app.get("/boom-value")
    async def boom_value() -> None:
        raise ValueError(f"simulated internal failure: {_FAKE_SECRET}")

    @test_app.get("/boom-connection")
    async def boom_connection() -> None:
        raise ConnectionError(f"postgresql://tracex:{_FAKE_SECRET}@db:5432/tracex")

    def _dependency_raises_http() -> None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")

    def _dependency_raises_unexpected() -> None:
        raise RuntimeError(f"simulated dependency failure: {_FAKE_SECRET}")

    @test_app.get("/boom-dependency-http")
    async def boom_dependency_http(_: None = Depends(_dependency_raises_http)) -> dict[str, str]:
        return {"status": "unreachable"}

    @test_app.get("/boom-dependency-unexpected")
    async def boom_dependency_unexpected(
        _: None = Depends(_dependency_raises_unexpected),
    ) -> dict[str, str]:
        return {"status": "unreachable"}

    # `_ValidateRequestBody` is defined at module scope, not nested here:
    # with `from __future__ import annotations`, every type hint below is a
    # plain string at runtime, and FastAPI resolves it via
    # `typing.get_type_hints()` against this function's *module* globals --
    # a class defined inside this function's own local scope is invisible
    # to that lookup and silently misparses as something else entirely
    # (confirmed directly: it was interpreted as a missing query parameter
    # named "body" instead of a JSON request body).
    @test_app.post("/validate")
    async def validate(body: _ValidateRequestBody) -> dict[str, str]:
        return {"email": body.email}

    @test_app.get("/request-id")
    async def echo_request_id() -> dict[str, str]:
        return {"request_id": get_request_id()}

    return test_app


_app = _build_synthetic_app()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # Matches real client/server behavior -- see `tests/conftest.py::client`'s
    # comment for the full explanation of why the httpx default
    # (`raise_app_exceptions=True`) does not.
    transport = ASGITransport(app=_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# --- Scenario 1: the original failure mode, captured as a regression test --


async def test_default_transport_setting_reproduces_the_original_symptom() -> None:
    """Documents the *actual* root cause: not `BaseHTTPMiddleware`, a test-transport default.

    With httpx's default `raise_app_exceptions=True`, an unhandled
    exception is re-raised to the *caller* of `client.get(...)` instead of
    the safe response being returned -- reproducing exactly what earlier
    investigation (see ADR-003, Decision 10) observed and mistakenly
    attributed to `BaseHTTPMiddleware`. This reproduces with the app's
    *pure ASGI* `RequestIDMiddleware`, proving the middleware
    implementation was never the cause.
    """
    transport = ASGITransport(app=_app)  # default: raise_app_exceptions=True
    async with AsyncClient(transport=transport, base_url="http://testserver") as raw_client:
        with pytest.raises(ValueError, match="simulated internal failure"):
            await raw_client.get("/boom-value")


async def test_corrected_transport_setting_returns_the_safe_response_instead(
    client: AsyncClient,
) -> None:
    """The same route, the same middleware, only the transport setting differs."""
    response = await client.get("/boom-value")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


# --- Scenario 2: HTTPException from a route -----------------------------


async def test_route_http_exception_returns_safe_envelope_and_status(client: AsyncClient) -> None:
    response = await client.get("/boom-http")
    assert response.status_code == status.HTTP_418_IM_A_TEAPOT
    assert response.json() == {
        "error": {
            "code": "validation_error",
            "message": "short and stout",
            "request_id": response.headers[REQUEST_ID_HEADER],
        }
    }


# --- Scenario 3: HTTPException from a dependency -------------------------


async def test_dependency_http_exception_returns_the_same_safe_envelope(
    client: AsyncClient,
) -> None:
    response = await client.get("/boom-dependency-http")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["error"]["code"] == "forbidden"
    assert response.json()["error"]["message"] == "access denied"


# --- Scenario 4: request validation failure -------------------------------


async def test_request_validation_failure_returns_safe_envelope(client: AsyncClient) -> None:
    response = await client.post("/validate", json={"email": "a@b.com"})  # missing password
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["errors"] == [
        {"loc": ["body", "password"], "type": "missing", "msg": "Field required"}
    ]


async def test_request_validation_failure_never_echoes_a_sibling_field_value(
    client: AsyncClient,
) -> None:
    # A missing `email` alongside a real `password` value: FastAPI's
    # *default* handler would echo the whole partial body (including the
    # password) as "input" on the missing-field error. This handler must not.
    response = await client.post("/validate", json={"password": _FAKE_SECRET})
    assert response.status_code == 422
    assert _FAKE_SECRET not in response.text
    assert "input" not in response.text


# --- Scenario 5 & 6: unexpected exception -> generic safe 500 -------------


async def test_unexpected_value_error_returns_generic_safe_500(client: AsyncClient) -> None:
    response = await client.get("/boom-value")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An internal error occurred",
            "request_id": response.headers[REQUEST_ID_HEADER],
        }
    }


async def test_unexpected_exception_from_a_dependency_also_returns_generic_safe_500(
    client: AsyncClient,
) -> None:
    response = await client.get("/boom-dependency-unexpected")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert _FAKE_SECRET not in response.text


async def test_no_traceback_or_exception_group_ever_reaches_the_client(
    client: AsyncClient,
) -> None:
    for path in ("/boom-value", "/boom-connection", "/boom-dependency-unexpected"):
        response = await client.get(path)
        text = response.text
        assert "Traceback" not in text
        assert "ExceptionGroup" not in text
        assert "BaseExceptionGroup" not in text
        assert "ValueError" not in text
        assert "ConnectionError" not in text
        assert "RuntimeError" not in text
        assert _FAKE_SECRET not in text


# --- Scenario 7: correlation ID present for success and every error path --


async def test_request_id_present_on_success(client: AsyncClient) -> None:
    response = await client.get("/ok")
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER]


async def test_request_id_present_on_handled_http_exception(client: AsyncClient) -> None:
    response = await client.get("/boom-http")
    assert response.headers[REQUEST_ID_HEADER]
    assert response.json()["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_request_id_present_on_validation_error(client: AsyncClient) -> None:
    response = await client.post("/validate", json={})
    assert response.headers[REQUEST_ID_HEADER]


async def test_request_id_present_on_the_deep_unhandled_exception_path(
    client: AsyncClient,
) -> None:
    """The critical case: Starlette dispatches the bare-`Exception` handler from its
    outermost `ServerErrorMiddleware`, bypassing every user-added middleware (including
    `RequestIDMiddleware`) for that one response -- see `app/core/errors.py`'s module
    docstring. The handler must therefore carry the ID itself; this proves it does.
    """
    response = await client.get("/boom-value")
    assert response.headers[REQUEST_ID_HEADER]
    assert response.json()["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_incoming_request_id_is_honored_even_on_the_unhandled_path(
    client: AsyncClient,
) -> None:
    response = await client.get("/boom-value", headers={REQUEST_ID_HEADER: "caller-supplied-id"})
    assert response.headers[REQUEST_ID_HEADER] == "caller-supplied-id"
    assert response.json()["error"]["request_id"] == "caller-supplied-id"


# --- Scenario 12: middleware context is cleaned up and cannot leak --------


async def test_request_id_context_does_not_leak_between_requests(client: AsyncClient) -> None:
    first = await client.get("/request-id")
    second = await client.get("/request-id")
    assert first.json()["request_id"] != second.json()["request_id"]
    # And after an error response, the *next* request still gets a fresh ID
    # rather than reusing (or losing) whatever the errored request set.
    await client.get("/boom-value")
    third = await client.get("/request-id")
    assert third.json()["request_id"] not in (
        first.json()["request_id"],
        second.json()["request_id"],
    )


async def test_request_id_context_is_empty_outside_any_request(client: AsyncClient) -> None:
    # `_request_id_ctx` is reset in `finally` -- calling `get_request_id()`
    # from outside any request (e.g. this test's own process-level context)
    # must never see a stale value left over from a previous request.
    await client.get("/ok")
    assert get_request_id() == ""


# --- Scenario 11: non-HTTP ASGI scope is passed through safely -----------


async def test_lifespan_scope_is_passed_through_without_http_assumptions() -> None:
    events: list[tuple[str, ...]] = []

    async def inner_app(scope: dict, receive: object, send: object) -> None:
        events.append((scope["type"],))
        if scope["type"] == "lifespan":
            message = await receive()  # type: ignore[operator]
            events.append((message["type"],))
            await send({"type": "lifespan.startup.complete"})  # type: ignore[operator]

    middleware = RequestIDMiddleware(inner_app)  # type: ignore[arg-type]

    sent: list[dict[str, str]] = []

    async def fake_receive() -> dict[str, str]:
        return {"type": "lifespan.startup"}

    async def fake_send(message: dict[str, str]) -> None:
        sent.append(message)

    await middleware({"type": "lifespan"}, fake_receive, fake_send)  # type: ignore[arg-type]

    assert events == [("lifespan",), ("lifespan.startup",)]
    assert sent == [{"type": "lifespan.startup.complete"}]


# --- Security headers baseline (also see tests/unit/access_control/test_api.py) --


async def test_error_responses_carry_the_same_baseline_security_headers(
    client: AsyncClient,
) -> None:
    for path in ("/boom-http", "/boom-value"):
        response = await client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
