"""Tests for /api/diagnostics/save-activity-report.

The activity report is the user's escape hatch for filing a bug
when something has gone wrong enough that they can't sign in or
pinpoint the failure mode themselves. The button is in About,
unauthenticated, and writes a JSON snapshot to ~/Downloads.

These tests pin three contracts:
1. The endpoint requires no auth — it works for a signed-out user.
2. Settings credentials are stripped before the file lands on disk.
3. The file is written somewhere predictable that the user can find
   (we let the test redirect Path.home() so we don't actually scribble
   in the test runner's real Downloads folder).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with the user's home directory redirected to a tmp
    path. The endpoint writes to ~/Downloads, so faking Path.home()
    keeps the test isolated and deterministic across CI environments.
    """
    import app.settings as _settings_mod
    import server

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / "Downloads").mkdir()

    monkeypatch.setattr(_settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    # The server module imports Path at module scope, so we monkeypatch
    # Path.home where the endpoint sees it. Using setattr on the class
    # affects all callers in the test process — fine, no other test
    # in this module depends on the real home.
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: fake_home)
    )

    original_settings = copy.deepcopy(server.settings)
    # Spotify client_id stays populated so we can verify the redaction
    # codepath on a non-empty value rather than the trivial empty-
    # string case.
    server.settings.spotify_client_id = "actual-client-id-from-spotify"

    with TestClient(server.app) as c:
        yield c, fake_home

    server.settings = original_settings


def test_endpoint_does_not_require_auth(client):
    c, _ = client
    # Default test fixture is signed out (no patch on _is_logged_in).
    r = c.post("/api/diagnostics/save-activity-report")
    assert r.status_code == 200, r.text


def test_response_returns_path_to_written_file(client):
    c, fake_home = client
    r = c.post("/api/diagnostics/save-activity-report")
    body = r.json()
    assert "path" in body
    assert "size_bytes" in body
    written = Path(body["path"])
    # Written into the fake ~/Downloads, with the expected prefix.
    assert written.is_file()
    assert written.parent == fake_home / "Downloads"
    assert written.name.startswith("tideway-activity-")
    assert written.name.endswith(".json")


def test_credentials_are_stripped_in_the_written_file(client):
    c, _ = client
    r = c.post("/api/diagnostics/save-activity-report")
    written = Path(r.json()["path"])
    payload = json.loads(written.read_text())
    # The original value must not appear anywhere in the report —
    # including unexpected places like a debug dict copy.
    serialized = json.dumps(payload)
    assert "actual-client-id-from-spotify" not in serialized
    # And the redaction sentinel is in place where the original was.
    assert payload["settings"]["spotify_client_id"] == "<redacted>"


def test_report_includes_required_top_level_keys(client):
    c, _ = client
    r = c.post("/api/diagnostics/save-activity-report")
    payload = json.loads(Path(r.json()["path"]).read_text())
    for key in ("schema", "generated_at", "app", "platform", "auth", "settings", "player", "audio"):
        assert key in payload, f"missing top-level key: {key}"


def test_falls_back_to_home_when_downloads_missing(tmp_path, monkeypatch):
    """If ~/Downloads doesn't exist (locale, custom layout), the
    endpoint should still write somewhere — we want the file even
    if we can't put it in the canonical place."""
    import app.settings as _settings_mod
    import server

    fake_home = tmp_path / "home_no_downloads"
    fake_home.mkdir()
    # Deliberately do NOT create ~/Downloads. The endpoint's fallback
    # writes to home directly.

    monkeypatch.setattr(_settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    original_settings = copy.deepcopy(server.settings)
    try:
        with TestClient(server.app) as c:
            r = c.post("/api/diagnostics/save-activity-report")
        body = r.json()
        written = Path(body["path"])
        assert written.is_file()
        assert written.parent == fake_home, (
            f"expected fallback to home, got {written.parent}"
        )
    finally:
        server.settings = original_settings
