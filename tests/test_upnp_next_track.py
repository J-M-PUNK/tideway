"""Phase 1 gapless: pre-stage the next track on the renderer via
SetNextAVTransportURI, and invalidate it on any explicit change.

The renderer advances on its own at the natural end of the current
track, so there is no second SetAVTransportURI and no decoder re-init
gap. The bounded file for that track is built once and shared with the
gapped path; these tests pin the staging/invalidation bookkeeping, the
URL the renderer is handed, and that invalidation releases the file
without touching a source that has already been promoted to current.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.audio.upnp import UpnpManager, _SessionState


class _FakeTrackSource:
    def __init__(self, track_id: int = 42, ready: bool = True) -> None:
        self.track_id = track_id
        self.ready = threading.Event()
        if ready:
            self.ready.set()
        self.failed = False
        self.path = "/tmp/fake.flac"
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _session(av: object) -> _SessionState:
    s = _SessionState(
        device=MagicMock(),
        openhome_device=MagicMock(),
        av=av,
        rc=None,
    )
    s.stream_url = "http://192.168.1.9:9999/dlna/stream"
    s.http_server = SimpleNamespace(track_source=None, next_track_source=None,
                                    dlna=True)
    s.passthrough_active = True
    s.current_announced.set()
    # The gapless opt-in gates SetNext pre-staging; these tests
    # exercise that path.
    s.gapless = True
    return s


def _manager(session: _SessionState) -> UpnpManager:
    mgr = UpnpManager.__new__(UpnpManager)
    mgr._session_lock = threading.Lock()
    mgr._session = session
    mgr._metadata_provider = None
    mgr._rebind_lock = threading.Lock()
    mgr._renderer_clock_active = False
    return mgr


def _stage(mgr: UpnpManager, session: _SessionState, ts) -> None:
    def _fake_prepare(source, prefetched=None, track_id=None):
        with session.passthrough_lock:
            session.next_track_source = ts
            session.next_source_urls = tuple(source)
            session.next_track_id = (
                str(track_id) if track_id is not None else None
            )

    mgr.prepare_next_passthrough = _fake_prepare


def test_set_next_track_false_without_session():
    mgr = UpnpManager.__new__(UpnpManager)
    mgr._session_lock = threading.Lock()
    mgr._session = None
    assert mgr.set_next_track(["u"]) is False

def test_set_next_track_false_without_renderer_support():
    av = MagicMock()
    av.supports_next_uri.return_value = False
    session = _session(av)
    mgr = _manager(session)
    ts = _FakeTrackSource()
    _stage(mgr, session, ts)

    assert mgr.set_next_track(["a", "b"], metadata={"title": "x"}) is False
    av.set_next_av_transport_uri.assert_not_called()


def test_set_next_track_stages_uri_and_metadata():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    mgr = _manager(session)
    ts = _FakeTrackSource(track_id=99)
    _stage(mgr, session, ts)

    ok = mgr.set_next_track(
        ["a", "b"], metadata={"title": "Fauré", "artist": "X"}
    )

    assert ok is True
    av.set_next_av_transport_uri.assert_called_once()
    uri, didl = av.set_next_av_transport_uri.call_args.args
    assert uri.endswith("?ts=99")
    assert "Fauré" in didl
    assert session.renderer_next_uri == uri
    assert session.http_server.next_track_source is ts


def test_set_next_track_uses_metadata_provider_fallback():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    mgr = _manager(session)
    mgr._metadata_provider = lambda: {"title": "Provided"}
    ts = _FakeTrackSource()
    _stage(mgr, session, ts)

    assert mgr.set_next_track(["a"]) is True
    _uri, didl = av.set_next_av_transport_uri.call_args.args
    assert "Provided" in didl


def test_set_next_track_false_without_metadata():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    mgr = _manager(session)
    ts = _FakeTrackSource()
    _stage(mgr, session, ts)

    assert mgr.set_next_track(["a"]) is False
    av.set_next_av_transport_uri.assert_not_called()


def test_invalidate_clears_and_closes_staged_source():
    av = MagicMock()
    session = _session(av)
    mgr = _manager(session)
    ts = _FakeTrackSource()
    session.next_track_source = ts
    session.next_source_urls = ("a",)
    session.renderer_next_uri = "http://x/dlna/stream?ts=42"
    session.http_server.next_track_source = ts

    mgr.invalidate_next_track()

    assert session.next_track_source is None
    assert session.next_source_urls is None
    assert session.renderer_next_uri is None
    assert session.http_server.next_track_source is None
    assert ts.closed is True


def test_invalidate_noop_when_nothing_staged():
    session = _session(MagicMock())
    mgr = _manager(session)
    mgr.invalidate_next_track()
    assert session.next_track_source is None
    assert session.renderer_next_uri is None


def test_deferred_send_waits_for_ready():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    mgr = _manager(session)
    ts = _FakeTrackSource(track_id=5, ready=False)
    _stage(mgr, session, ts)

    assert mgr.set_next_track(["a"], metadata={"title": "x"}) is True
    # Not servable yet: the renderer must not be handed the URI.
    av.set_next_av_transport_uri.assert_not_called()

    ts.ready.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if av.set_next_av_transport_uri.call_count == 1:
            break
        time.sleep(0.01)
    av.set_next_av_transport_uri.assert_called_once()
    uri, _didl = av.set_next_av_transport_uri.call_args.args
    assert uri.endswith("?ts=5")
    assert session.renderer_next_uri == uri


def test_invalidate_before_ready_cancels_deferred_send():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    mgr = _manager(session)
    ts = _FakeTrackSource(track_id=5, ready=False)
    _stage(mgr, session, ts)

    assert mgr.set_next_track(["a"], metadata={"title": "x"}) is True
    mgr.invalidate_next_track()
    ts.ready.set()

    time.sleep(0.2)
    av.set_next_av_transport_uri.assert_not_called()


def test_next_uri_waits_for_current_announce():
    """A preload can beat the connect-time current announce; the next
    URI must not go out until the current one has."""
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    session.current_announced.clear()
    mgr = _manager(session)
    ts = _FakeTrackSource(track_id=77)
    _stage(mgr, session, ts)

    assert mgr.set_next_track(["a"], metadata={"title": "x"}) is True
    av.set_next_av_transport_uri.assert_not_called()

    session.current_announced.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if av.set_next_av_transport_uri.call_count == 1:
            break
        time.sleep(0.01)
    av.set_next_av_transport_uri.assert_called_once()
    uri, _didl = av.set_next_av_transport_uri.call_args.args
    assert uri.endswith("?ts=77")


def test_invalidate_leaves_promoted_source_open():
    """After promotion the staged file IS the current source; clearing
    the http server's next pointer must not close it."""
    av = MagicMock()
    session = _session(av)
    mgr = _manager(session)
    ts = _FakeTrackSource()
    session.http_server.track_source = ts  # promoted to current
    session.http_server.next_track_source = None
    session.next_track_source = None

    mgr.invalidate_next_track()

    assert ts.closed is False
    assert session.http_server.track_source is ts


# ---------------------------------------------------------------------
# Auto-advance detection (skip the redundant SetAVTransportURI)
# ---------------------------------------------------------------------


def _advanced(session, staged_uri, track_ts, *, promoted=True):
    return _manager(session)._renderer_already_advanced(
        session, promoted, staged_uri, track_ts
    )


def test_already_advanced_true_when_current_uri_matches():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    staged = "http://192.168.1.9:9999/dlna/stream?ts=42"
    av.get_current_uri.return_value = staged

    assert _advanced(session, staged, 42) is True


def test_already_advanced_true_on_ts_suffix():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    av.get_current_uri.return_value = "http://other-host/dlna/stream?ts=42"

    assert _advanced(session, "http://x/dlna/stream?ts=42", 42) is True


def test_already_advanced_false_on_explicit_early_jump():
    """Renderer still on the old track: the user jumped early, so the
    normal announce is what switches it now."""
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    av.get_current_uri.return_value = "http://x/dlna/stream?ts=41"

    assert _advanced(session, "http://x/dlna/stream?ts=42", 42) is False


def test_already_advanced_false_without_promotion():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    av.get_current_uri.return_value = "http://x/dlna/stream?ts=42"
    session = _session(av)
    assert _advanced(session, "u", 42, promoted=False) is False


def test_already_advanced_true_on_ts_without_staged_uri():
    """The pre-stage send can lose its race with the local EOF, leaving
    renderer_next_uri unset. The renderer's CurrentURI carrying the
    promoted track's ts is still ground truth: don't re-announce."""
    av = MagicMock()
    av.supports_next_uri.return_value = True
    av.get_current_uri.return_value = "http://x/dlna/stream?ts=42"
    session = _session(av)
    assert _advanced(session, None, 42) is True


def test_already_advanced_false_when_uri_elsewhere_without_stage():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    av.get_current_uri.return_value = "http://x/dlna/stream?ts=99"
    session = _session(av)
    assert _advanced(session, None, 42) is False


def test_already_advanced_true_when_watchdog_consumed_next():
    """A pre-staged URI + the watchdog ending the track means the
    renderer advanced into it, even when GetMediaInfo is unavailable."""
    av = MagicMock()
    av.supports_next_uri.return_value = True
    av.get_current_uri.return_value = None
    session = _session(av)
    assert (
        _manager(session)._renderer_already_advanced(
            session, True, "http://x/dlna/stream?ts=42", 42, True
        )
        is True
    )


def test_already_advanced_false_when_uri_unknown():
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)
    av.get_current_uri.return_value = None

    assert _advanced(session, "http://x/dlna/stream?ts=42", 42) is False


def test_already_advanced_true_on_sticky_consumed_track_id():
    """The CurrentURI probe can fail after UAPP moves its control port.
    The sticky consumed-track id is probe-free evidence the renderer
    advanced, so the announce must still be skipped."""
    av = MagicMock()
    av.supports_next_uri.return_value = True
    av.get_current_uri.side_effect = RuntimeError("Connection refused")
    session = _session(av)

    assert (
        _manager(session)._renderer_already_advanced(
            session,
            True,
            None,
            42,
            False,
            consumed_track_id="tid-n1",
            track_id="tid-n1",
        )
        is True
    )


def test_already_advanced_sticky_ignored_without_promotion():
    """Sticky evidence only applies when the file we promoted is the one
    the renderer consumed; a fresh (non-promoted) build must announce."""
    av = MagicMock()
    av.supports_next_uri.return_value = True
    session = _session(av)

    assert (
        _manager(session)._renderer_already_advanced(
            session,
            False,
            None,
            42,
            False,
            consumed_track_id="tid-n1",
            track_id="tid-n1",
        )
        is False
    )


def test_start_passthrough_skips_announce_on_sticky_evidence(monkeypatch):
    """End-to-end of the boundary fix: the watchdog recorded that the
    renderer consumed the pre-staged next (sticky, and the CurrentURI
    probe now fails because UAPP moved its control port). The follow-up
    start_passthrough for that track must promote the file and NOT send a
    second SetAVTransportURI."""
    av = MagicMock()
    av.supports_next_uri.return_value = True
    av.get_current_uri.side_effect = RuntimeError("Connection refused")
    session = _session(av)
    mgr = _manager(session)

    staged = _FakeTrackSource(track_id=1000)
    session.next_track_source = staged
    session.next_source_urls = ("uN",)
    session.next_track_id = "tid-n1"
    # Watchdog evidence recorded before the generation reset.
    session.renderer_consumed_track_id = "tid-n1"
    session.http_server.track_source = _FakeTrackSource(track_id=999)

    announced = []
    monkeypatch.setattr(
        mgr, "_announce_track", lambda *a, **k: announced.append(a)
    )
    monkeypatch.setattr(mgr, "_arm_renderer_watch", lambda *a, **k: None)
    import app.audio.segment_reader as _sr_mod

    monkeypatch.setattr(_sr_mod, "SegmentReader", lambda *a, **k: object())

    mgr.start_passthrough(
        ["uN"], metadata={"title": "N1"}, track_id="tid-n1"
    )

    assert announced == [], "must not re-announce the consumed track"
    assert session.http_server.track_source is staged




def _rebind_manager(session: _SessionState) -> UpnpManager:
    mgr = UpnpManager.__new__(UpnpManager)
    mgr._session_lock = threading.Lock()
    mgr._session = session
    mgr._rebind_lock = threading.Lock()
    return mgr


def test_rebind_repoints_session_when_device_moved(monkeypatch):
    """UAPP re-registers its UPnP device after some track boundaries,
    which moves the control URL. The session must follow it or every
    later SOAP call hits the dead endpoint."""
    import app.audio.upnp as upnp_mod

    device = SimpleNamespace(id="uuid:abc", location="http://h:1/d.xml")
    av_old = MagicMock()
    session = _SessionState(
        device=device, openhome_device=MagicMock(), av=av_old, rc=None
    )
    mgr = _rebind_manager(session)

    moved = SimpleNamespace(id="uuid:abc", location="http://h:2/d.xml")
    mgr.refresh = lambda timeout=4.0: [moved]

    fresh = MagicMock(name="fresh")
    monkeypatch.setattr(upnp_mod, "fetch_device", lambda loc: fresh)
    new_av = MagicMock(name="new_av")
    monkeypatch.setattr(
        upnp_mod.AVTransportController, "from_device", lambda dev: new_av
    )
    new_rc = MagicMock(name="new_rc")
    monkeypatch.setattr(
        upnp_mod.RenderingControlController, "from_device", lambda dev: new_rc
    )

    assert mgr._rebind_session_renderer(session) is True
    assert session.device is moved
    assert session.openhome_device is fresh
    assert session.av is new_av
    assert session.rc is new_rc


def test_rebind_noop_when_location_unchanged():
    device = SimpleNamespace(id="uuid:abc", location="http://h:1/d.xml")
    av = MagicMock()
    session = _SessionState(
        device=device, openhome_device=MagicMock(), av=av, rc=None
    )
    mgr = _rebind_manager(session)
    mgr.refresh = lambda timeout=4.0: [device]

    assert mgr._rebind_session_renderer(session) is False
    assert session.av is av


def test_rebind_false_when_device_not_found():
    device = SimpleNamespace(id="uuid:abc", location="http://h:1/d.xml")
    av = MagicMock()
    session = _SessionState(
        device=device, openhome_device=MagicMock(), av=av, rc=None
    )
    mgr = _rebind_manager(session)
    mgr.refresh = lambda timeout=4.0: []

    assert mgr._rebind_session_renderer(session) is False
    assert session.av is av


def test_send_next_uri_retries_after_transport_error(monkeypatch):
    """A transient control-endpoint outage must not leave the renderer
    with no `next`: the send is retried on a watcher thread."""
    import app.audio.upnp as upnp_mod

    monkeypatch.setattr(upnp_mod, "_NEXT_URI_RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(upnp_mod, "_NEXT_URI_RETRY_MAX_S", 2.0)
    monkeypatch.setattr(upnp_mod, "_RENDERER_REBIND_MIN_INTERVAL_S", 999.0)

    av = MagicMock()
    av.set_next_av_transport_uri.side_effect = [
        RuntimeError("SOAP transport ... Connection refused"),
        None,
    ]
    session = _session(av)
    mgr = _manager(session)
    mgr._next_uri_lead_reached = lambda s: True
    mgr._rebind_session_renderer = lambda s: False
    ts = _FakeTrackSource(track_id=99)
    with session.passthrough_lock:
        session.next_track_source = ts

    assert (
        mgr._send_next_uri(
            session, ts, "http://x/dlna/stream?ts=99", "<d/>", {"title": "T"}
        )
        is False
    )
    deadline = time.time() + 2.0
    while session.renderer_next_uri is None and time.time() < deadline:
        time.sleep(0.02)

    assert session.renderer_next_uri == "http://x/dlna/stream?ts=99"
    assert av.set_next_av_transport_uri.call_count >= 2


def test_next_uri_lead_prefers_renderer_progress():
    """The lead gate must use the renderer's remaining time when its
    clock is active — the local decode clock doesn't advance in DLNA,
    so a local-position gate would never open."""
    session = _session(MagicMock())
    mgr = _manager(session)
    mgr._renderer_clock_active = True

    session.renderer_position_s = 100.0
    session.renderer_duration_s = 120.0
    session.renderer_position_at = time.monotonic()
    # 20s remaining <= 30s lead -> stage now.
    assert mgr._next_uri_lead_reached(session) is True

    session.renderer_position_s = 5.0
    session.renderer_position_at = time.monotonic()
    # 115s remaining -> not yet.
    assert mgr._next_uri_lead_reached(session) is False


def test_send_next_uri_retry_stops_when_superseded(monkeypatch):
    """The retry must abandon the moment the pre-stage is superseded,
    so a stale URI can't be handed to the renderer after the fact."""
    import app.audio.upnp as upnp_mod

    # Interval longer than the window in which we supersede, so the
    # retry's first check is deterministic.
    monkeypatch.setattr(upnp_mod, "_NEXT_URI_RETRY_INTERVAL_S", 0.2)
    monkeypatch.setattr(upnp_mod, "_NEXT_URI_RETRY_MAX_S", 2.0)

    av = MagicMock()
    av.set_next_av_transport_uri.side_effect = RuntimeError("refused")
    session = _session(av)
    mgr = _manager(session)
    mgr._next_uri_lead_reached = lambda s: True
    mgr._rebind_session_renderer = lambda s: False
    ts = _FakeTrackSource(track_id=99)
    with session.passthrough_lock:
        session.next_track_source = ts

    mgr._send_next_uri(
        session, ts, "http://x/dlna/stream?ts=99", "<d/>", {"title": "T"}
    )
    # The next track starts: start_passthrough clears the pre-stage.
    with session.passthrough_lock:
        session.next_track_source = _FakeTrackSource(track_id=100)
    time.sleep(0.5)

    assert session.renderer_next_uri is None
    assert av.set_next_av_transport_uri.call_count == 1

