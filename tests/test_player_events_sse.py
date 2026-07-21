"""Tests for the /api/player/events SSE stream's delivery guarantees.

Auto-advance in Tideway is client-driven: the backend plays a track to
its end, parks at `state="ended"`, and emits that transition exactly
once. The frontend catches the edge and drives the next track. The
whole chain therefore hinges on the `ended` snapshot actually reaching
the client — if it's lost, the player sits at end-of-track forever with
no recovery ("the song ended and didn't go to the next song").

Two regressions this pins:

1. `_put_latest` must never drop the NEWEST snapshot under backpressure.
   The old producer did `put_nowait` inside a bare `except: pass`, so a
   full queue silently discarded the very snapshot (`ended`) that drives
   the advance.

2. The stream must keep re-advertising a parked `ended`/`error` state
   instead of deduping it away, so a client that missed the single edge
   still sees it on the next poll.
"""
from __future__ import annotations

import asyncio

import pytest

import server


# ---------------------------------------------------------------------------
# _put_latest — backpressure must sacrifice the OLDEST, keep the newest
# ---------------------------------------------------------------------------


def test_put_latest_keeps_newest_when_full():
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    server._put_latest(q, {"seq": 1, "state": "playing"})
    server._put_latest(q, {"seq": 2, "state": "playing"})
    # Queue is now full. The newest snapshot must still land — the old
    # code would have dropped this one on the floor.
    server._put_latest(q, {"seq": 3, "state": "ended"})

    drained = [q.get_nowait() for _ in range(q.qsize())]
    seqs = [p["seq"] for p in drained]
    assert seqs == [2, 3], f"expected oldest (1) dropped, newest kept: {seqs}"
    # The critical invariant: the terminal `ended` snapshot survived.
    assert drained[-1]["state"] == "ended"


def test_put_latest_normal_case_appends():
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    server._put_latest(q, {"seq": 1, "state": "playing"})
    server._put_latest(q, {"seq": 2, "state": "ended"})
    assert q.qsize() == 2
    assert q.get_nowait()["seq"] == 1
    assert q.get_nowait()["seq"] == 2


# ---------------------------------------------------------------------------
# _snapshot_needs_client_action — which states must keep flowing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["ended", "error"])
def test_states_that_require_a_client_response_are_actionable(state):
    assert server._snapshot_needs_client_action(state) is True


@pytest.mark.parametrize("state", ["playing", "paused", "idle", "loading", None])
def test_display_only_states_are_not_actionable(state):
    assert server._snapshot_needs_client_action(state) is False


# ---------------------------------------------------------------------------
# _should_forward_snapshot — the poll-loop gate that parks or re-sends
# ---------------------------------------------------------------------------
#
# This is the exact decision the SSE generator makes on every poll tick.
# A live end-to-end stream test can't cover it cleanly: the generator
# loops forever and Starlette's TestClient never satisfies
# `request.is_disconnected()` on teardown, so the client hangs. Pinning
# the pure decision instead keeps the regression covered deterministically.


def test_new_seq_is_always_forwarded():
    assert server._should_forward_snapshot(6, 5, "playing") is True


def test_repeat_seq_of_a_display_state_is_deduped():
    # Idle/paused/playing keepalive ticks with an unchanged seq stay off
    # the wire so we don't spam identical frames.
    assert server._should_forward_snapshot(5, 5, "playing") is False
    assert server._should_forward_snapshot(5, 5, "paused") is False


@pytest.mark.parametrize("state", ["ended", "error"])
def test_repeat_seq_of_an_actionable_state_is_re_advertised(state):
    # The regression: a client that missed the single `ended`/`error`
    # edge must keep being offered it on subsequent polls, even though
    # the seq hasn't advanced. Before the fix this returned False and
    # the player hung at end-of-track forever.
    assert server._should_forward_snapshot(9179, 9179, state) is True
