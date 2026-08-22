"""Tests for the persisted download queue that survives a restart.

Two field reports drove this: a user with unfinished downloads reboots
or force-quits, reopens Tideway, and the queue is empty. Both causes
lived in the same file.

  * `_worker_loop` cleared an item's snapshot record the instant a
    worker picked it up, on the reasoning that an in-flight transfer
    isn't resumable. True for the bytes, wrong for the item: a hard
    kill leaves no UI holding the row, so clearing at start meant
    whatever was downloading vanished for good.

  * `restore()` deleted the snapshot file up front and relied on the
    resubmitted items to write it back. Anything that went wrong in
    that window — a second crash, an expand thread that never
    finished — took the whole queue with it.

The fix keeps a record alive from enqueue until the item is genuinely
terminal, and makes restore() rewrite the snapshot in place under the
original item ids instead of truncating it.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app import downloader as dl_mod
from app.downloader import DownloadItem, DownloadStatus, Downloader


class _Settings:
    concurrent_downloads = 1
    skip_existing = True

    def __init__(self, out: Path) -> None:
        self.output_dir = str(out)


class _Tidal:
    """The downloader only calls fetch_url on the expand path, which
    these tests stub out or never reach."""

    def fetch_url(self, url):  # pragma: no cover - not exercised here
        raise AssertionError(f"unexpected fetch_url({url!r})")


class _Track:
    def __init__(self, tid: int) -> None:
        self.id = tid


@pytest.fixture
def make_dl(tmp_path, monkeypatch):
    """Build a Downloader with its queue file redirected into tmp_path.

    A factory rather than a plain fixture because the constructor
    reads the queue file — tests about restoring have to write the
    snapshot first and then construct, the same order a real launch
    uses.

    The constructor spawns its worker pool; the threads park on an
    empty queue, so tests that never enqueue anything pay nothing for
    them. Daemon threads, so a test that leaves one parked doesn't
    hold up the run.
    """
    monkeypatch.setattr(dl_mod, "QUEUE_STATE_FILE", tmp_path / "download_queue.json")
    out = tmp_path / "out"
    out.mkdir()

    def _build() -> Downloader:
        return Downloader(
            _Tidal(),
            _Settings(out),
            on_add=lambda _i: None,
            on_update=lambda _i: None,
        )

    return _build


@pytest.fixture
def dl(make_dl):
    return make_dl()


def _snapshot(tmp_path: Path) -> list[dict]:
    f = tmp_path / "download_queue.json"
    if not f.exists():
        return []
    return json.loads(f.read_text())


def _item(title: str = "Song") -> DownloadItem:
    import uuid

    return DownloadItem(item_id=str(uuid.uuid4()), url="", title=title)


# ---------------------------------------------------------------------------
# Record / clear lifecycle
# ---------------------------------------------------------------------------


def test_record_writes_item_id_into_the_snapshot(dl, tmp_path):
    """restore() keys off item_id, so it has to round-trip to disk."""
    item = _item()
    dl._record_pending_track(item, _Track(101), "LOSSLESS")

    entries = _snapshot(tmp_path)
    assert len(entries) == 1
    assert entries[0]["item_id"] == item.item_id
    assert entries[0]["id"] == "101"
    assert entries[0]["quality"] == "LOSSLESS"


def test_track_without_an_id_is_not_recorded(dl, tmp_path):
    """A track we can't name in a URL can't be restored, so don't
    pretend it's in the queue."""
    track = _Track(1)
    track.id = None
    dl._record_pending_track(_item(), track, None)

    assert _snapshot(tmp_path) == []


def test_pending_record_survives_the_start_of_a_download(dl, tmp_path):
    """The regression this branch exists for.

    Drive a real item through the real worker loop, freeze it inside
    _download, and check the snapshot still lists it. Before the fix
    the worker cleared the record before calling _download at all, so
    a kill at this exact moment lost the item.
    """
    item = _item("In flight")
    dl._record_pending_track(item, _Track(202), None)

    entered = threading.Event()
    release = threading.Event()

    def _fake_download(_it: DownloadItem) -> None:
        entered.set()
        release.wait(timeout=5)

    dl._download = _fake_download  # type: ignore[method-assign]
    dl._work_queue.put(item)

    assert entered.wait(timeout=5), "worker never picked the item up"
    try:
        assert [e["item_id"] for e in _snapshot(tmp_path)] == [item.item_id]
    finally:
        release.set()


def test_cancel_drops_the_record(dl, tmp_path):
    item = _item()
    dl._record_pending_track(item, _Track(303), None)
    dl.cancel(item.item_id)

    assert _snapshot(tmp_path) == []


def test_worker_clears_the_record_when_a_download_explodes(dl, tmp_path):
    """A crash that escapes _download is terminal. Leaving the record
    behind would re-queue the same exploding item every launch."""
    item = _item()
    dl._record_pending_track(item, _Track(404), None)

    # Watch the clear itself rather than polling the file — the worker
    # publishes FAILED and clears after _download returns, so waiting
    # on _download alone would race the assertion.
    cleared = threading.Event()
    real_clear = dl._clear_pending

    def _spy(item_id: str) -> None:
        real_clear(item_id)
        cleared.set()

    dl._clear_pending = _spy  # type: ignore[method-assign]

    def _fake_download(_it: DownloadItem) -> None:
        raise RuntimeError("kaboom")

    dl._download = _fake_download  # type: ignore[method-assign]
    dl._work_queue.put(item)

    assert cleared.wait(timeout=5), "worker never cleared the record"
    assert _snapshot(tmp_path) == []
    assert item.status is DownloadStatus.FAILED


def test_retry_puts_the_record_back(dl, tmp_path):
    """A failed item's record is gone. Retrying re-arms it so the
    second attempt is as restart-proof as the first."""
    item = _item()
    track = _Track(505)
    dl._track_map_put(item.item_id, (track, None))
    dl._record_pending_track(item, track, None)
    dl._clear_pending(item.item_id)
    assert _snapshot(tmp_path) == []

    # retry() queues the item; park the worker that picks it up so the
    # real download path stays out of this test.
    parked = threading.Event()
    dl._download = lambda _it: parked.wait(timeout=5)  # type: ignore[method-assign]
    try:
        dl.retry(item)
        assert [e["item_id"] for e in _snapshot(tmp_path)] == [item.item_id]
    finally:
        parked.set()


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------


def _seed_queue_file(tmp_path: Path, n: int) -> list[dict]:
    entries = [
        {
            "item_id": f"item-{i}",
            "kind": "track",
            "id": str(1000 + i),
            "quality": None,
            "title": f"Track {i}",
            "artist": "Someone",
            "album": "Something",
            "album_folder_artist": "",
        }
        for i in range(n)
    ]
    (tmp_path / "download_queue.json").write_text(json.dumps(entries))
    return entries


def test_restore_does_not_truncate_the_snapshot(make_dl, tmp_path):
    """Before the fix, restore() unlinked the file and hoped the
    resubmits would write it back. A crash in that window emptied the
    queue permanently."""
    _seed_queue_file(tmp_path, 3)
    dl = make_dl()
    dl.submit = lambda *a, **k: None  # type: ignore[method-assign]

    assert dl.restore() == 3
    assert sorted(e["item_id"] for e in _snapshot(tmp_path)) == [
        "item-0",
        "item-1",
        "item-2",
    ]


def test_restore_resubmits_under_the_original_item_id(make_dl, tmp_path):
    _seed_queue_file(tmp_path, 2)
    dl = make_dl()
    calls: list[dict] = []
    dl.submit = lambda url, quality=None, item_id=None: calls.append(  # type: ignore[method-assign]
        {"url": url, "item_id": item_id}
    )

    dl.restore()

    assert [c["item_id"] for c in calls] == ["item-0", "item-1"]
    assert calls[0]["url"].endswith("/track/1000")


def test_a_partial_restore_leaves_the_rest_of_the_queue_on_disk(make_dl, tmp_path):
    """The item ids are reused precisely so that a resubmit rewriting
    the snapshot mid-restore doesn't erase the entries it hasn't
    reached yet."""
    _seed_queue_file(tmp_path, 3)
    dl = make_dl()
    dl.submit = lambda *a, **k: None  # type: ignore[method-assign]
    dl.restore()

    # One restored item completes its expand and re-records itself.
    revived = _item("Track 1")
    revived.item_id = "item-1"
    dl._record_pending_track(revived, _Track(1001), None)

    assert sorted(e["item_id"] for e in _snapshot(tmp_path)) == [
        "item-0",
        "item-1",
        "item-2",
    ]


def test_restore_consumes_the_snapshot_once_per_process(make_dl, tmp_path):
    """Both the boot thread and the deferred-session callback call
    restore(), and the callback can fire more than once. A second pass
    over a queue that is still pending would download everything
    twice."""
    _seed_queue_file(tmp_path, 2)
    dl = make_dl()
    calls: list[str] = []
    dl.submit = lambda url, quality=None, item_id=None: calls.append(url)  # type: ignore[method-assign]

    assert dl.restore() == 2
    assert dl.restore() == 0
    assert len(calls) == 2


def test_restore_tolerates_a_snapshot_without_item_ids(make_dl, tmp_path):
    """Queue files written by an older build have no item_id key.
    They still restore, just under freshly minted ids."""
    (tmp_path / "download_queue.json").write_text(
        json.dumps([{"kind": "track", "id": "77", "quality": None}])
    )
    dl = make_dl()
    seen: list[str | None] = []
    dl.submit = lambda url, quality=None, item_id=None: seen.append(item_id)  # type: ignore[method-assign]

    assert dl.restore() == 1
    assert seen and seen[0]


def test_restore_skips_malformed_entries(make_dl, tmp_path):
    (tmp_path / "download_queue.json").write_text(
        json.dumps([{"kind": "track"}, "junk", {"id": "5"}, {"kind": "track", "id": "9"}])
    )
    dl = make_dl()
    seen: list[str] = []
    dl.submit = lambda url, quality=None, item_id=None: seen.append(url)  # type: ignore[method-assign]

    assert dl.restore() == 1
    assert seen[0].endswith("/track/9")


def test_restore_with_no_file_is_a_no_op(dl, tmp_path):
    dl.submit = lambda *a, **k: pytest.fail("should not submit")  # type: ignore[method-assign]
    assert dl.restore() == 0


# ---------------------------------------------------------------------------
# The snapshot has to survive not being restored
# ---------------------------------------------------------------------------


def test_a_new_download_does_not_erase_an_unrestored_queue(make_dl, tmp_path):
    """The gap the restore machinery left open.

    _persist_pending writes the whole in-memory map, so if the
    previous session's queue is still sitting unread on disk, the
    first download queued in this process would write a snapshot
    without it. That ordering is reachable: booting signed out makes
    the boot thread skip the restore, and an interactive sign-in does
    not fire the deferred-session callback that would catch it, so the
    user can queue something new with the old queue still unread.
    """
    _seed_queue_file(tmp_path, 3)
    dl = make_dl()

    # A brand-new download, queued before restore() ever runs.
    fresh = _item("Queued today")
    dl._record_pending_track(fresh, _Track(999), None)

    ids = {e["item_id"] for e in _snapshot(tmp_path)}
    assert ids == {"item-0", "item-1", "item-2", fresh.item_id}


def test_an_unrestored_queue_survives_to_the_next_launch(make_dl, tmp_path):
    """Same scenario, one restart further on. The session that never
    restored still shut down with three things pending — the two it
    inherited and the one it queued itself — and the next launch owes
    the user all three."""
    _seed_queue_file(tmp_path, 2)
    first = make_dl()
    first._record_pending_track(_item(), _Track(999), None)

    second = make_dl()
    second.submit = lambda *a, **k: None  # type: ignore[method-assign]
    assert second.restore() == 3


def test_items_queued_in_this_process_are_not_resubmitted(make_dl, tmp_path):
    """Restoring what the running process already queued would
    download it twice."""
    dl = make_dl()
    dl._record_pending_track(_item(), _Track(123), None)
    dl.submit = lambda *a, **k: pytest.fail("should not resubmit")  # type: ignore[method-assign]

    assert dl.restore() == 0


# ---------------------------------------------------------------------------
# Permanent vs transient expand failures
# ---------------------------------------------------------------------------


class _Boom:
    def __init__(self, exc) -> None:
        self.exc = exc

    def fetch_url(self, url):
        raise self.exc


def _http_error(status: int) -> Exception:
    """Shaped like the transport error our HTTP layer raises: the
    status is on an attached response, not in the message."""
    exc = Exception(f"HTTP Error {status}: ")
    exc.response = type("_R", (), {"status_code": status})()  # type: ignore[attr-defined]
    return exc


def _expand_failing_with(dl: Downloader, exc: Exception, item_id: str) -> None:
    dl.tidal = _Boom(exc)
    dl._expand_and_enqueue("https://tidal.com/browse/track/5", None, item_id)


def test_a_retired_track_is_dropped_from_the_queue(make_dl, tmp_path):
    """A 404 means the id is gone. Keeping it would retry on every
    launch forever, with no way for the user to see or stop it."""
    _seed_queue_file(tmp_path, 1)
    dl = make_dl()

    _expand_failing_with(dl, _http_error(404), "item-0")

    assert _snapshot(tmp_path) == []


def test_a_rate_limited_restore_keeps_the_item(make_dl, tmp_path):
    """Restoring resubmits a whole queue at once, which is exactly
    when a 429 is likeliest. Dropping the item for that would be the
    data loss the snapshot exists to prevent."""
    _seed_queue_file(tmp_path, 1)
    dl = make_dl()

    _expand_failing_with(dl, _http_error(429), "item-0")

    assert [e["item_id"] for e in _snapshot(tmp_path)] == ["item-0"]


def test_a_network_failure_keeps_the_item(make_dl, tmp_path):
    _seed_queue_file(tmp_path, 1)
    dl = make_dl()

    _expand_failing_with(dl, ConnectionError("connection reset"), "item-0")

    assert [e["item_id"] for e in _snapshot(tmp_path)] == ["item-0"]


def test_a_server_error_keeps_the_item(make_dl, tmp_path):
    _seed_queue_file(tmp_path, 1)
    dl = make_dl()

    _expand_failing_with(dl, _http_error(502), "item-0")

    assert [e["item_id"] for e in _snapshot(tmp_path)] == ["item-0"]
