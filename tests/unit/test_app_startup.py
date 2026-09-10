"""Scenario 17: FastAPI startup smoke test — the app assembles and serves its routes."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient

from app.main import app


async def test_app_starts_and_serves_expected_routes(client: AsyncClient) -> None:
    assert isinstance(app, FastAPI)
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/api/v1/meta/contracts")).status_code == 200
    # /readyz depends on live infra; a non-404 response proves the route is
    # registered and the app didn't crash on startup.
    assert (await client.get("/readyz")).status_code != 404
