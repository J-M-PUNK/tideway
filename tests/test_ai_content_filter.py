"""Tests for the Tidal AI-content filter.

Tidal's July 2026 policy tags every 100%-AI-generated track with an
`ai` boolean on the track payload and lets users hide that content.
Tideway mirrors that: capture the flag off the raw payload, expose it,
and hard-filter flagged tracks out of browse lists and downloads when
`hide_ai_content` is on.

Covered here:
  - the tidalapi parse patch that keeps the dropped `ai` field
  - track_to_dict surfacing `ai`
  - filter_ai_tracks (server browse filter)
  - the downloader's AI block
  - the setting default + PUT round-trip
"""

import copy
import types

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with settings redirected to a tmp file and the
    in-memory snapshot restored after the test. Mirrors the fixture in
    test_settings_endpoint.py; offline_mode is flipped on so the
    local-access guard in front of the settings endpoints allows the
    unauthenticated PUT."""
    import app.settings as _settings_mod
    import server

    monkeypatch.setattr(
        _settings_mod, "SETTINGS_FILE", tmp_path / "settings.json"
    )
    original_settings = copy.deepcopy(server.settings)
    server.settings.offline_mode = True
    server.downloader.settings = server.settings

    with TestClient(server.app) as c:
        yield c

    server.settings = original_settings
    server.downloader.settings = original_settings


def _fake_track(ai, name="Song", tid="1"):
    """A stand-in for a tidalapi Track carrying just what the code
    under test reads."""
    return types.SimpleNamespace(
        id=tid,
        name=name,
        duration=100,
        track_num=1,
        explicit=False,
        artists=[],
        album=None,
        share_url=None,
        mixes={},
        media_metadata_tags=[],
        isrc=None,
        ai=ai,
    )


# --------------------------------------------------------------------
# Parse patch: tidalapi drops `ai`; app.tidal_client puts it back.
# --------------------------------------------------------------------


def test_parse_patch_installed():
    import app.tidal_client  # noqa: F401  (import applies the patch)
    from tidalapi.media import Track

    assert Track.parse_track.__name__ == "parse_track_with_ai"
    # Class-level default so getattr on a pre-patch object is safe.
    assert Track.ai is None


@pytest.mark.parametrize(
    "payload, expected",
    [({"ai": True}, True), ({"ai": False}, False), ({}, None)],
)
def test_parse_patch_captures_ai(monkeypatch, payload, expected):
    """The wrapper reads `ai` off the raw json and sets it on both the
    returned copy and `self` (Track.__init__ keeps self and discards
    the returned copy; list parsing keeps the copy)."""
    import app.tidal_client  # noqa: F401
    from tidalapi.media import Track

    # Media.parse does the heavy artist/album parsing we don't care
    # about here; stub it so we can drive parse_track with a bare json.
    monkeypatch.setattr(
        "tidalapi.media.Media.parse", lambda self, j, a=None: None
    )

    fake = Track.__new__(Track)
    fake.session = types.SimpleNamespace(
        config=types.SimpleNamespace(
            listen_base_url="https://listen", share_base_url="https://share"
        )
    )
    fake.id = "42"
    fake.album = None
    fake.available = False
    fake.version = None
    fake.title = "Title"

    returned = Track.parse_track(fake, {**payload, "url": None})

    assert returned.ai is expected
    assert fake.ai is expected


# --------------------------------------------------------------------
# track_to_dict surfaces the flag.
# --------------------------------------------------------------------


@pytest.mark.parametrize("ai", [True, False, None])
def test_track_to_dict_includes_ai(ai):
    import server

    out = server.track_to_dict(_fake_track(ai))
    assert out["ai"] is ai


# --------------------------------------------------------------------
# filter_ai_tracks: the browse-list filter.
# --------------------------------------------------------------------


def test_filter_ai_tracks_off_is_passthrough(monkeypatch):
    import server

    monkeypatch.setattr(server.settings, "hide_ai_content", False)
    items = [_fake_track(True), _fake_track(False), _fake_track(None)]
    assert server.filter_ai_tracks(items) == items


def test_filter_ai_tracks_on_drops_only_true(monkeypatch):
    import server

    monkeypatch.setattr(server.settings, "hide_ai_content", True)
    ai_true = _fake_track(True, name="AI")
    ai_false = _fake_track(False, name="human")
    ai_none = _fake_track(None, name="unknown")
    out = server.filter_ai_tracks([ai_true, ai_false, ai_none])
    # Only an explicit True is dropped — a missing flag is kept so a
    # sparse payload never blanks a shelf.
    assert out == [ai_false, ai_none]


# --------------------------------------------------------------------
# Downloader AI block.
# --------------------------------------------------------------------


def _downloader_stub(hide_ai):
    """A DownloadManager-ish object exposing only what _filter_ai_pairs
    and _surface_ai_blocked touch."""
    from app.downloader import Downloader

    stub = Downloader.__new__(Downloader)
    stub.settings = types.SimpleNamespace(hide_ai_content=hide_ai)
    return stub


def test_filter_ai_pairs_off_keeps_all():
    stub = _downloader_stub(hide_ai=False)
    pairs = [(_fake_track(True), None, 0), (_fake_track(False), None, 0)]
    kept, dropped = stub._filter_ai_pairs(pairs)
    assert kept == pairs
    assert dropped == 0


def test_filter_ai_pairs_on_drops_ai():
    stub = _downloader_stub(hide_ai=True)
    ai_pair = (_fake_track(True), None, 0)
    human_pair = (_fake_track(False), None, 0)
    unknown_pair = (_fake_track(None), None, 0)
    kept, dropped = stub._filter_ai_pairs([ai_pair, human_pair, unknown_pair])
    assert kept == [human_pair, unknown_pair]
    assert dropped == 1


def test_surface_ai_blocked_adds_failed_row():
    from app.downloader import DownloadStatus

    stub = _downloader_stub(hide_ai=True)
    added = []
    stub.on_add = added.append
    stub._surface_ai_blocked()
    assert len(added) == 1
    row = added[0]
    assert row.status == DownloadStatus.FAILED
    assert "AI-generated" in row.error


# --------------------------------------------------------------------
# Setting default + PUT round-trip.
# --------------------------------------------------------------------


def test_hide_ai_content_defaults_on():
    """Tideway hides AI-generated tracks out of the box — a stricter
    default than Tidal's own client (which ships with AI allowed)."""
    from app.settings import Settings

    assert Settings().hide_ai_content is True


def test_put_hide_ai_content_persists(client):
    for value in (True, False):
        r = client.put("/api/settings", json={"hide_ai_content": value})
        assert r.status_code == 200, r.text
        assert r.json()["hide_ai_content"] is value


# --------------------------------------------------------------------
# One-time "AI content is now hidden" Home notice.
# --------------------------------------------------------------------


def test_fresh_install_suppresses_ai_notice(tmp_path, monkeypatch):
    """A brand-new install (no settings.json) starts with the notice
    already acknowledged — a new user shouldn't be told a default
    changed when they've never seen a different one."""
    import app.settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "SETTINGS_FILE", tmp_path / "settings.json"
    )
    loaded = settings_mod.load_settings()
    assert loaded.ai_filter_notice_ack is True


def test_existing_install_shows_ai_notice_once(tmp_path, monkeypatch):
    """An existing settings.json that predates the field loads with the
    notice unacknowledged, so the Home screen shows it once."""
    import json

    import app.settings as settings_mod

    settings_file = tmp_path / "settings.json"
    # A pre-existing file without the new field — what every install
    # upgrading into this version has on disk.
    settings_file.write_text(json.dumps({"volume": 80}))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)

    loaded = settings_mod.load_settings()
    assert loaded.ai_filter_notice_ack is False


def test_put_ai_filter_notice_ack_persists(client):
    r = client.put("/api/settings", json={"ai_filter_notice_ack": True})
    assert r.status_code == 200, r.text
    assert r.json()["ai_filter_notice_ack"] is True
