"""FastAPI-level tests for the Tidal backoff plumbing.

Covers the HTTP surface the frontend depends on:
  - `GET /api/tidal/backoff` returns the current gate state.
  - `TidalBackoffError` raised inside any endpoint is translated to
    a 503 with a Retry-After header and a JSON body the client can
    parse (the TidalBackoffBanner reads `active` / `seconds_remaining`
    / `reason`).
"""
import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import server

    # Backoff reset is handled by the autouse fixture in conftest.py.
    with TestClient(server.app) as c:
        yield c


def test_backoff_endpoint_inactive_by_default(client):
    r = client.get("/api/tidal/backoff")

    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False
    assert body["seconds_remaining"] == 0.0
    assert body["reason"] == ""
    assert "until_epoch" in body


def test_backoff_endpoint_reflects_active_state(client):
    from app import tidal_client

    tidal_client._trigger_tidal_backoff(
        120.0, "simulated rate-limit (HTTP 429)"
    )

    r = client.get("/api/tidal/backoff")

    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True
    assert 115 < body["seconds_remaining"] <= 120
    assert "rate-limit" in body["reason"]
    assert body["until_epoch"] > 0


def test_backoff_endpoint_has_stable_schema(client):
    """The frontend TidalBackoffBanner reads `active`, `reason`, and
    `seconds_remaining`. Pin the schema so nobody silently drops a
    field."""
    r = client.get("/api/tidal/backoff")
    body = r.json()

    assert set(body.keys()) == {
        "active",
        "seconds_remaining",
        "reason",
        "until_epoch",
    }
    assert isinstance(body["active"], bool)
    assert isinstance(body["seconds_remaining"], (int, float))
    assert isinstance(body["reason"], str)
    assert isinstance(body["until_epoch"], (int, float))


# --- 503 exception handler --------------------------------------------

def test_backoff_error_translates_to_503():
    """Direct unit test of the exception handler: construct a
    TidalBackoffError, call the handler, verify the response shape.
    Avoids requiring a real endpoint that would go through auth."""
    from app.tidal_client import TidalBackoffError
    from server import _tidal_backoff_handler
    import json

    exc = TidalBackoffError(
        seconds_remaining=180.5,
        reason="abuse-detected (HTTP 403)",
    )
    resp = asyncio.run(_tidal_backoff_handler(None, exc))

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "180"

    body = json.loads(resp.body.decode())
    assert body["tidal_backoff"] is True
    assert body["seconds_remaining"] == 180.5
    assert body["reason"] == "abuse-detected (HTTP 403)"
    assert "180s" in body["detail"]


def test_backoff_error_retry_after_floor():
    """Retry-After must never fall below 1 even when the remaining
    time is a fraction of a second — otherwise the browser's retry
    scheduler may spin tight on 0."""
    from app.tidal_client import TidalBackoffError
    from server import _tidal_backoff_handler

    exc = TidalBackoffError(seconds_remaining=0.3, reason="just tripped")
    resp = asyncio.run(_tidal_backoff_handler(None, exc))

    assert resp.headers["Retry-After"] == "1"
