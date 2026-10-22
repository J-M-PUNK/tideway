"""Cold-start prefetch warms the persisted track at the restore's quality.

The manifest cache is keyed on (track, quality) and the frontend's
restore loads at the user's streaming quality. Warming at the session
default instead (quality=None) was a guaranteed miss, so the first play
after launch paid the full resolve the prefetch exists to save.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def warm(monkeypatch):
    import server

    player = MagicMock()
    monkeypatch.setattr(server, "_native_player", lambda: player)

    def run(state):
        monkeypatch.setattr(server.now_playing_state, "read_state", lambda: state)
        server._prefetch_persisted_now_playing()
        return player.prefetch

    return run


def test_warms_at_the_persisted_quality(warm):
    prefetch = warm({"trackId": "123", "quality": "hi_res_lossless"})
    prefetch.assert_called_once_with("123", "hi_res_lossless", warm_bytes=True)


def test_snapshot_from_before_quality_was_persisted_keeps_the_default(warm):
    prefetch = warm({"trackId": "123"})
    prefetch.assert_called_once_with("123", None, warm_bytes=True)


@pytest.mark.parametrize("state", [None, {}, {"trackId": ""}, {"trackId": 5}])
def test_nothing_to_warm(warm, state):
    warm(state).assert_not_called()
