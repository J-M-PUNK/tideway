"""The cold taste-profile build reports where its time went.

Every For You row blocks on `_taste_profile()` behind `_taste_cache_lock`,
so a cold profile *is* the page's time-to-first-row. A real cold start was
measured at roughly 30 seconds with nothing in the app saying which part
owned it — five of the six phases are Last.fm round trips and the sixth
harvests a couple of years of AOTY pages to build the genre slug map.

Measurement had to be done by hand, from outside, against a running app:
restart it, open the page, poll a diagnostic endpoint and hope to catch
the window. These tests pin that the build says so itself instead — on
any machine, including a reporter's.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def profile_env(monkeypatch):
    """Stub every network edge of `_taste_profile` so it runs offline."""
    import server

    monkeypatch.setattr(
        server,
        "lastfm",
        SimpleNamespace(
            status=lambda: {"connected": True},
            get_chart_top_tags=lambda limit=250: [
                {"name": "shoegaze", "reach": 5000}
            ],
            get_artist_top_tags=lambda name, limit=None: [
                {"name": "shoegaze", "count": 100}
            ],
            get_chart_top_artists=lambda limit=500: [{"name": "slowdive"}],
        ),
    )
    monkeypatch.setattr(
        server, "_seed_artists", lambda: [{"name": "Slowdive", "score": 1.0}]
    )
    monkeypatch.setattr(
        server, "_aoty_genre_slug_map", lambda: {"shoegaze": ("26-shoegaze", "Shoegaze")}
    )
    return server


def test_reports_every_phase(profile_env, capsys):
    profile_env._taste_profile()
    line = capsys.readouterr().out

    assert "[perf] taste_profile" in line
    # All six phases, or the line can't tell us which one is slow — which
    # is the entire reason it exists.
    for phase in (
        "status=",
        "seed_artists=",
        "chart_tags=",
        "artist_tags=",
        "genre_slugs=",
        "chart_artists=",
    ):
        assert phase in line, f"{phase} missing from: {line!r}"
    assert "total=" in line


def test_disconnected_profile_reports_nothing(profile_env, capsys):
    """No Last.fm, no work worth timing — and no misleading 0ms line."""
    profile_env.lastfm.status = lambda: {"connected": False}

    out = profile_env._taste_profile()

    assert out["connected"] is False
    assert "[perf] taste_profile" not in capsys.readouterr().out


def test_perf_line_also_reaches_the_durable_log(profile_env, caplog):
    """A packaged launch discards stdout, so the print alone would be
    invisible exactly where the 30-second cold start was reported."""
    import logging

    with caplog.at_level(logging.INFO, logger="tideway.perf"):
        profile_env._taste_profile()

    assert any("[perf] taste_profile" in r.message for r in caplog.records)


def test_test_runs_do_not_write_to_the_users_perf_log():
    """The file handler must be off under pytest.

    The recs tests call `_taste_profile()` directly, so without this a
    suite run appends stub timings — artists=1, every phase 0ms — to the
    same perf.log a real cold start writes to. Reading it back, there is
    no way to tell which lines came from the app. That happened: 40
    lines of test output were mistaken for real measurements and nearly
    reported as a recommendations bug.
    """
    import logging

    import server

    # pytest's caplog attaches its own handlers to whichever logger a
    # test captures, so the assertion is about file handlers only.
    # pytest attaches its own handlers to a captured logger, including a
    # FileHandler aimed at the null device, so the assertion has to be
    # about the destination rather than the handler type.
    writers = [
        h
        for h in server.perf_log.handlers
        if str(getattr(h, "baseFilename", "")).endswith("perf.log")
    ]
    assert not writers, f"perf.log would be written during tests: {writers!r}"
