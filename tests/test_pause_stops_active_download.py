"""Pause stops an in-progress transfer, not just the queue (#398).

`_run_event` was only consulted before the concurrency gate, so Pause
stopped the queue advancing while whatever was already downloading ran
to completion. The button showed the paused state throughout, which is
what made it a bug rather than a documented limitation.

On Linux it cost more than a mismatched label: a Hi-Res transfer
competing with PipeWire produced `[audio] callback status=output
underflow`, and pausing downloads is the obvious way to stop that.

The transfer aborts rather than blocking. Holding the socket open while
idle would trip the 30 s inactivity read timeout, turning a pause
longer than that into a failure. Tidal serves no Range header, so a
stopped transfer cannot resume from its partial anyway — the item is
re-queued and restarts on resume, which is how every other interrupted
download already behaves.
"""
from __future__ import annotations

import pytest

from app.downloader import Downloader, _Cancelled, _PausedMidTransfer


class _Probe:
    """Just the pause/cancel machinery `_check_paused` touches."""

    def __init__(self, paused: bool):
        import threading

        self._run_event = threading.Event()
        if not paused:
            self._run_event.set()
        self._cancelled_ids: set = set()
        self._cancelled_lock = threading.Lock()


def _probe(paused: bool) -> _Probe:
    return _Probe(paused)


def test_running_queue_does_not_interrupt_a_transfer():
    """The hot path: one Event.is_set() per chunk, and it must not
    raise while the queue is running."""
    Downloader._check_paused(_probe(paused=False))


def test_paused_queue_aborts_the_transfer():
    with pytest.raises(_PausedMidTransfer):
        Downloader._check_paused(_probe(paused=True))


def test_pause_is_checked_in_both_chunk_loops():
    """A single track uses the first loop; a DASH hi-res track streams
    per-segment through the second. Missing either would leave Pause
    working on one quality tier and not the other.
    """
    import inspect

    src = inspect.getsource(Downloader._download)

    assert src.count("self._check_paused()") == 2, (
        "expected the pause check in both the first-URL and per-segment "
        "chunk loops"
    )


def test_pause_is_not_a_failure():
    """The item goes back to PENDING via retry(), not to FAILED. A row
    turning red because the user pressed Pause would be its own bug."""
    import inspect

    src = inspect.getsource(Downloader._download)
    # Split on the *next* handler, not on "except " — the block has an
    # inner try/except around the unlink.
    handler = src.split("except _PausedMidTransfer:")[1].split("except _Cancelled:")[0]

    assert "self.retry(item)" in handler
    assert "FAILED" not in handler


def test_cancel_still_takes_precedence():
    """Both checks run per chunk, cancel first. A cancel while paused
    must remove the item, not re-queue it."""
    import inspect

    src = inspect.getsource(Downloader._download)
    first = src.index("self._check_cancel(item.item_id)")
    assert first < src.index("self._check_paused()")
    assert _Cancelled is not _PausedMidTransfer
