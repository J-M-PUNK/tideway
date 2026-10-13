"""Issue #354 fix A: do not announce a bounded track's URI to the
renderer until the FLAC temp file is demuxed and servable.

``TrackFileSource`` only sets ``ready`` once the whole track is on disk,
and ``_serve_track`` blocks on that gate before answering. Announcing
early makes the renderer's first GET wait on the server; UAPP times out
after ~3s and restarts its decoder from 0, desyncing it from the
desktop clock. On the first track of a session there is no preload to
promote, so the demux starts at connect time and the race is real —
which is why only track 1 was cut.

These pin ``_announce_track`` / ``_notify_when_ready``: ready sources
announce synchronously (the preload case), the connect path waits, and
mid-session changes defer to a watcher thread that skips if the session
ended or a newer track superseded the file.
"""
from __future__ import annotations

import threading
import time
from typing import Optional
from unittest.mock import MagicMock

import pytest

from app.audio.upnp import UpnpManager, _SessionState


# ---------------------------------------------------------------------
# Fixtures: bare manager + fake bounded source / http server
# ---------------------------------------------------------------------


class _FakeTrackSource:
    """Stands in for TrackFileSource: the only surface _announce_track
    and _notify_when_ready touch is ready / failed / path."""

    def __init__(
        self,
        *,
        ready: bool = False,
        failed: bool = False,
        path: Optional[str] = "/tmp/fake.flac",
        track_id: int = 7,
    ) -> None:
        self.ready = threading.Event()
        if ready:
            self.ready.set()
        self.failed = failed
        self.path = path
        self.track_id = track_id


class _FakeHTTPServer:
    def __init__(self, track_source: object) -> None:
        self.track_source = track_source


def _session(av: object, track_source: object) -> _SessionState:
    s = _SessionState(
        device=MagicMock(),
        openhome_device=MagicMock(),
        av=av,
        rc=None,
    )
    s.stream_url = "http://192.168.1.9:9999/dlna/stream"
    s.http_server = _FakeHTTPServer(track_source)
    return s


def _manager(session: _SessionState) -> UpnpManager:
    mgr = UpnpManager.__new__(UpnpManager)
    mgr._session_lock = threading.Lock()
    mgr._session = session
    mgr._metadata_provider = None
    return mgr


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------
# Synchronous cases
# ---------------------------------------------------------------------


def test_ready_source_announces_immediately():
    av = MagicMock()
    ts = _FakeTrackSource(ready=True)
    session = _session(av, ts)
    mgr = _manager(session)

    mgr._announce_track(
        session, ts, {"title": "Fauré"}, 7, wait_for_ready=False
    )

    av.set_av_transport_uri.assert_called_once()
    av.play.assert_called_once()


def test_no_bounded_source_announces_immediately():
    """Ring-buffer / non-DLNA passthrough has no file to wait for."""
    av = MagicMock()
    ts = _FakeTrackSource(ready=True)
    session = _session(av, ts)
    mgr = _manager(session)

    mgr._announce_track(
        session, None, {"title": "Fauré"}, 7, wait_for_ready=False
    )

    av.set_av_transport_uri.assert_called_once()


def test_connect_path_waits_for_demux_then_announces():
    av = MagicMock()
    ts = _FakeTrackSource(ready=False)
    session = _session(av, ts)
    mgr = _manager(session)

    def _become_ready() -> None:
        time.sleep(0.1)
        ts.ready.set()

    threading.Thread(target=_become_ready, daemon=True).start()
    mgr._announce_track(
        session, ts, {"title": "Fauré"}, 7, wait_for_ready=True
    )

    av.set_av_transport_uri.assert_called_once()


def test_connect_path_raises_when_demux_failed():
    av = MagicMock()
    ts = _FakeTrackSource(ready=False)
    session = _session(av, ts)
    mgr = _manager(session)

    def _fail() -> None:
        time.sleep(0.05)
        ts.failed = True
        ts.path = None
        ts.ready.set()

    threading.Thread(target=_fail, daemon=True).start()
    with pytest.raises(RuntimeError):
        mgr._announce_track(
            session, ts, {"title": "Fauré"}, 7, wait_for_ready=True
        )
    av.set_av_transport_uri.assert_not_called()


# ---------------------------------------------------------------------
# Deferred (mid-session) cases
# ---------------------------------------------------------------------


def test_mid_session_does_not_announce_until_ready():
    av = MagicMock()
    ts = _FakeTrackSource(ready=False)
    session = _session(av, ts)
    mgr = _manager(session)

    mgr._announce_track(
        session, ts, {"title": "Fauré"}, 7, wait_for_ready=False
    )
    # Not ready yet: the renderer must not have been told.
    av.set_av_transport_uri.assert_not_called()

    ts.ready.set()
    assert _wait_until(lambda: av.set_av_transport_uri.call_count == 1)


def test_deferred_announce_skips_when_superseded():
    av = MagicMock()
    ts = _FakeTrackSource(ready=False)
    session = _session(av, ts)
    mgr = _manager(session)

    mgr._announce_track(
        session, ts, {"title": "Fauré"}, 7, wait_for_ready=False
    )
    # A newer track replaced the bounded file before this one was ready.
    session.http_server.track_source = _FakeTrackSource(ready=True, track_id=8)
    ts.ready.set()

    time.sleep(0.2)
    av.set_av_transport_uri.assert_not_called()


def test_deferred_announce_skips_when_session_ended():
    av = MagicMock()
    ts = _FakeTrackSource(ready=False)
    session = _session(av, ts)
    mgr = _manager(session)

    mgr._announce_track(
        session, ts, {"title": "Fauré"}, 7, wait_for_ready=False
    )
    mgr._session = None  # disconnect() won the race
    ts.ready.set()

    time.sleep(0.2)
    av.set_av_transport_uri.assert_not_called()
