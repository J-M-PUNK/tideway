"""Where the For You cold load actually goes (#401 follow-on).

The page takes roughly thirty seconds cold. Instrumenting
`_taste_profile` accounted for 4.5 s of that — measured on a real
cold start, not a stub:

    total=4515ms artist_tags=1593ms genre_slugs=1297ms
    seed_artists=750ms chart_artists=500ms chart_tags=375ms

which leaves most of the wait unexplained. The two candidates left are
`_owned_album_ids()` and `_build_one_rec_section()`, and inside the
latter, resolving each AOTY album to a Tidal search on a cold cache.

These pin that both lines are emitted with the phases that can tell
those apart. Getting this wrong is expensive in a way tests usually
aren't: a missing phase means another cold start, and a cold start is
not something you can ask a user for repeatedly.
"""
from __future__ import annotations

import inspect

import server


def test_section_build_reports_its_three_phases():
    src = inspect.getsource(server.recommendations_section)

    assert "[perf] rec_section" in src
    for phase in ("taste_profile=", "owned_albums=", "build_section="):
        assert phase in src, f"{phase} missing — cannot attribute the time"


def test_section_line_records_what_it_produced():
    """A row that resolved nothing is fast for an uninteresting reason.
    Without the album count, a 0 ms build looks like good news."""
    src = inspect.getsource(server.recommendations_section)

    assert "albums=" in src


def test_resolve_step_is_timed():
    src = inspect.getsource(server._aoty_section)

    assert "[perf] rec_resolve" in src
    assert "listing_items=" in src
    assert "resolved=" in src


def test_resolve_line_is_thresholded():
    """Every row resolves on every build, warm or cold. Logging each
    one would bury the cold start that matters in hundreds of 0 ms
    lines — the same noise problem that made perf.log useless once."""
    src = inspect.getsource(server._aoty_section)

    assert "_resolve_ms >= 250.0" in src


def test_perf_lines_go_to_the_durable_log():
    """A packaged launch discards stdout, which is where these reports
    come from."""
    for fn in (server.recommendations_section, server._aoty_section):
        assert "perf_log.info" in inspect.getsource(fn)
