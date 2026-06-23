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


def test_audio_section_always_carries_a_health_key(client):
    # The playback-health counters are how a remote stutter report gets
    # triaged, so the key must always be present: None when no player
    # has been constructed, otherwise the counter dict. Either way the
    # report stays useful in a degraded state.
    c, _ = client
    r = c.post("/api/diagnostics/save-activity-report")
    payload = json.loads(Path(r.json()["path"]).read_text())
    assert "health" in payload["audio"]
    health = payload["audio"]["health"]
    if health is not None and "error" not in health:
        for k in (
            "output_underruns",
            "queue_starvations",
            "callback_jitter_events",
            "worst_jitter_late_ms",
        ):
            assert k in health, f"health missing counter: {k}"


def test_audio_health_is_surfaced_when_a_player_exists(client, monkeypatch):
    # With a live player the report must carry its counters verbatim so
    # the reader can see which glitch class dominates.
    import server

    class _FakePlayer:
        def list_output_devices(self):
            return []

        def snapshot(self):
            return server._pcm_player_singleton  # unused; replaced below

        def audio_health(self):
            return {
                "output_underruns": 0,
                "queue_starvations": 12,
                "callback_jitter_events": 0,
                "worst_jitter_late_ms": 0.0,
                "samples_emitted": 999,
                "pcm_queue_depth": 3,
                "pcm_queue_max": 100,
            }

    fake = _FakePlayer()
    monkeypatch.setattr(server, "_pcm_player_singleton", fake)
    # The player section calls snapshot()+_snapshot_dict; stub that path
    # so this test stays focused on the health wiring.
    monkeypatch.setattr(server, "_snapshot_dict", lambda _s: {"state": "playing"})

    c, _ = client
    r = c.post("/api/diagnostics/save-activity-report")
    payload = json.loads(Path(r.json()["path"]).read_text())
    assert payload["audio"]["health"]["queue_starvations"] == 12
    assert payload["audio"]["health"]["samples_emitted"] == 999


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
