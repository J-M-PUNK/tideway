"""Phase 2 of issue #354: drive DLNA track advance from the renderer's
own playback clock via GetPositionInfo / GetCurrentURI polling.

The renderer buffers ahead and re-inits its decoder on SetAVTransportURI,
so the desktop decoder's EOF fires early and cannot be the gapless
trigger. ``UpnpManager._renderer_watch`` polls the renderer until it
reports the current bounded track ended (position reached the duration,
or a pre-staged SetNextAVTransportURI was consumed) and then fires the
player's ended callback. Renderers that don't answer position probes
leave the local decode clock in charge.

These pin the parser, the URI matcher, the watchdog's end detection,
its give-up behaviour, and the clock-active gate.
"""
from __future__ import annotations

import threading
import time
from typing import Optional
from unittest.mock import MagicMock

import pytest

from app.audio import upnp as upnp_mod
from app.audio.avtransport import parse_rel_time
from app.audio.upnp import UpnpManager, _SessionState, _same_track_uri


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(upnp_mod, "_RENDERER_POLL_INTERVAL_S", 0.01)


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------


class _FakeAV:
    """Stands in for AVTransportController: only the probes the
    watchdog uses. `positions` is consumed one entry per
    get_position_seconds() call; an entry may be an Exception to
    simulate a SOAP failure, or None for "not supported"."""

    def __init__(self, positions=None, current_uri=None,
                 transport_state=None) -> None:
        self.positions = list(positions) if positions else []
        self.current_uri = current_uri
        self.transport_state = transport_state

    def get_position_seconds(self):
        if self.positions:
            value = self.positions.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return None

    def get_current_uri(self):
        return self.current_uri

    def get_transport_info(self):
        return {"CurrentTransportState": self.transport_state or ""}


def _session(av) -> _SessionState:
    return _SessionState(
        device=MagicMock(),
        openhome_device=MagicMock(),
        av=av,
        rc=None,
    )


def _manager(session) -> UpnpManager:
    mgr = UpnpManager.__new__(UpnpManager)
    mgr._session_lock = threading.Lock()
    mgr._session = session
    mgr._renderer_ended_callback = None
    mgr._renderer_clock_active = False
    mgr._rebind_lock = threading.Lock()
    return mgr


def _run_watch(mgr, session, gen=0, fallback: Optional[float] = None):
    # Treat the track as having started long ago so the wall-time guard
    # (a track can't end before its duration has elapsed) doesn't block
    # the position-based end-of-stream under test.
    session.renderer_started_at = time.monotonic() - 10_000.0
    threading.Thread(
        target=mgr._renderer_watch,
        args=(session, MagicMock(), gen, fallback),
        daemon=True,
    ).start()


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------------
# parse_rel_time
# ---------------------------------------------------------------------


def test_parse_rel_time_hms_and_minutes():
    assert parse_rel_time("00:00:00") == 0.0
    assert parse_rel_time("01:02:03") == 3723.0
    assert parse_rel_time("00:01:02.5") == pytest.approx(62.5)
    assert parse_rel_time("10:00") == 600.0
    assert parse_rel_time("42") == 42.0


def test_parse_rel_time_unknown_values():
    assert parse_rel_time(None) is None
    assert parse_rel_time("") is None
    assert parse_rel_time("   ") is None
    assert parse_rel_time("NOT_IMPLEMENTED") is None
    assert parse_rel_time("not_implemented") is None
    assert parse_rel_time("garbage") is None
    assert parse_rel_time("1:2:3:4") is None


# ---------------------------------------------------------------------
# _same_track_uri
# ---------------------------------------------------------------------


def test_same_track_uri_exact_and_ts_suffix():
    staged = "http://192.168.1.9:9/dlna/stream?ts=42"
    assert _same_track_uri(staged, staged) is True
    assert _same_track_uri("http://other/dlna/stream?ts=42", staged) is True
    assert _same_track_uri("http://x/dlna/stream?ts=41", staged) is False
    assert _same_track_uri("http://x/dlna/stream", staged) is False


# ---------------------------------------------------------------------
# Watchdog: end detection
# ---------------------------------------------------------------------


def test_position_at_duration_fires_once_and_gates_clock():
    av = _FakeAV(positions=[(99.8, 100.0)])
    session = _session(av)
    mgr = _manager(session)
    fired = threading.Event()
    calls = []
    mgr._renderer_ended_callback = lambda: (calls.append(1), fired.set())

    _run_watch(mgr, session)

    assert fired.wait(timeout=2.0)
    assert len(calls) == 1
    assert session.renderer_ended_fired is True
    assert mgr.renderer_clock_active() is True


def test_position_below_duration_waits_then_fires():
    av = _FakeAV(positions=[(10.0, 100.0), (10.0, 100.0), (100.0, 100.0)])
    session = _session(av)
    mgr = _manager(session)
    fired = threading.Event()
    mgr._renderer_ended_callback = lambda: fired.set()

    _run_watch(mgr, session)

    assert fired.wait(timeout=2.0)
    assert session.renderer_ended_fired is True


def test_metadata_duration_used_when_renderer_omits_it():
    # TrackDuration parses to a usable value only via the fallback.
    av = _FakeAV(positions=[(29.5, 0.0), (30.0, 0.0)])
    session = _session(av)
    mgr = _manager(session)
    fired = threading.Event()
    mgr._renderer_ended_callback = lambda: fired.set()

    _run_watch(mgr, session, fallback=30.0)

    assert fired.wait(timeout=2.0)


def test_auto_advanced_uri_fires_without_waiting_for_duration():
    staged = "http://192.168.1.9:9/dlna/stream?ts=7"
    av = _FakeAV(positions=[(1.0, 200.0)], current_uri=staged)
    session = _session(av)
    session.renderer_next_uri = staged
    mgr = _manager(session)
    fired = threading.Event()
    mgr._renderer_ended_callback = lambda: fired.set()

    _run_watch(mgr, session)

    assert fired.wait(timeout=2.0)
    assert mgr.renderer_clock_active() is True


def test_unsupported_position_does_not_fire_and_leaves_local_clock():
    av = _FakeAV(positions=[])  # always None
    session = _session(av)
    mgr = _manager(session)
    calls = []
    mgr._renderer_ended_callback = lambda: calls.append(1)

    _run_watch(mgr, session)
    time.sleep(0.2)

    assert calls == []
    assert mgr.renderer_clock_active() is False
    assert session.renderer_ended_fired is False


def test_gated_then_probes_fail_does_not_advance_early():
    # One good position (engages the clock), then None forever. Probe
    # failure must NOT fire the ended callback — that desyncs the session
    # during a renderer's decoder re-init. The deadline is the backstop.
    av = _FakeAV(positions=[(10.0, 100.0)])  # one good, then None forever
    session = _session(av)
    mgr = _manager(session)
    calls = []
    mgr._renderer_ended_callback = lambda: calls.append(1)

    _run_watch(mgr, session)
    time.sleep(0.3)

    assert calls == []
    assert mgr.renderer_clock_active() is True


def test_error_exception_then_good_position_recovers():
    av = _FakeAV(positions=[RuntimeError("soap"), (50.0, 100.0),
                            (100.0, 100.0)])
    session = _session(av)
    mgr = _manager(session)
    fired = threading.Event()
    mgr._renderer_ended_callback = lambda: fired.set()

    _run_watch(mgr, session)

    assert fired.wait(timeout=2.0)


def test_stops_when_generation_changes():
    av = _FakeAV(positions=[(10.0, 100.0)] * 100)
    session = _session(av)
    mgr = _manager(session)
    calls = []
    mgr._renderer_ended_callback = lambda: calls.append(1)

    _run_watch(mgr, session, gen=5)
    # A new track started: generation moved on. The watcher for gen 5
    # must exit without firing.
    with session.passthrough_lock:
        session.renderer_watch_gen = 6
    time.sleep(0.2)
    assert calls == []


def test_stops_when_session_ends():
    av = _FakeAV(positions=[(10.0, 100.0)] * 100)
    session = _session(av)
    mgr = _manager(session)
    calls = []
    mgr._renderer_ended_callback = lambda: calls.append(1)

    _run_watch(mgr, session)
    mgr._session = None  # disconnect() won
    time.sleep(0.2)
    assert calls == []


def test_already_fired_generation_exits_immediately():
    av = _FakeAV(positions=[(10.0, 100.0)] * 100)
    session = _session(av)
    session.renderer_ended_fired = True
    mgr = _manager(session)
    calls = []
    mgr._renderer_ended_callback = lambda: calls.append(1)

    _run_watch(mgr, session)
    time.sleep(0.2)
    assert calls == []


# ---------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------


def test_arm_bumps_generation_and_resets_gate():
    av = _FakeAV(positions=[(10.0, 100.0)] * 100)
    session = _session(av)
    session.renderer_ended_fired = True
    session.renderer_clock_usable = True
    mgr = _manager(session)
    mgr._set_renderer_clock_active(True)

    before = session.renderer_watch_gen
    mgr._arm_renderer_watch(session, MagicMock(), {"duration_s": 30})

    assert session.renderer_watch_gen == before + 1
    assert session.renderer_ended_fired is False
    assert session.renderer_clock_usable is False
    assert mgr.renderer_clock_active() is False


def test_metadata_duration_preferred_over_stale_renderer_duration():
    """A renderer that just promoted a pre-staged track can report the
    previous track's TrackDuration (UAPP does). The metadata duration
    must win, or end-of-stream fires far too early."""
    av = _FakeAV(positions=[(1.0, 56.0), (2.0, 56.0)])
    session = _session(av)
    mgr = _manager(session)
    fired = threading.Event()
    mgr._renderer_ended_callback = lambda: fired.set()

    _run_watch(mgr, session, fallback=216.0)

    assert _wait(lambda: session.renderer_duration_s is not None)
    assert session.renderer_duration_s == 216.0
    assert not fired.is_set()


def test_set_renderer_ended_callback_stored():
    mgr = UpnpManager.__new__(UpnpManager)
    cb = lambda: None
    mgr.set_renderer_ended_callback(cb)
    assert mgr._renderer_ended_callback is cb


def test_watchdog_records_renderer_position():
    """The reported position must be published to the session so the
    player can surface it (the frontend's preload trigger is
    position-gated and the local clock doesn't advance in DLNA)."""
    av = _FakeAV(positions=[(12.0, 200.0)])
    session = _session(av)
    mgr = _manager(session)

    _run_watch(mgr, session, fallback=200.0)
    deadline = time.time() + 1.0
    while session.renderer_position_s is None and time.time() < deadline:
        time.sleep(0.01)

    assert session.renderer_position_s == 12.0


def test_renderer_position_ms_none_when_clock_inactive():
    session = _session(_FakeAV())
    session.renderer_position_s = 12.0
    session.renderer_duration_s = 200.0
    session.renderer_position_at = time.monotonic()
    mgr = _manager(session)
    mgr._renderer_clock_active = False
    assert mgr.renderer_position_ms() is None


def test_renderer_position_ms_reports_fresh_reading():
    session = _session(_FakeAV())
    session.renderer_position_s = 12.5
    session.renderer_duration_s = 200.0
    session.renderer_position_at = time.monotonic()
    mgr = _manager(session)
    mgr._renderer_clock_active = True
    assert mgr.renderer_position_ms() == 12500


def test_renderer_position_ms_none_when_stale():
    session = _session(_FakeAV())
    session.renderer_position_s = 12.5
    session.renderer_duration_s = 200.0
    session.renderer_position_at = time.monotonic() - 30.0
    mgr = _manager(session)
    mgr._renderer_clock_active = True
    assert mgr.renderer_position_ms() is None


# ---------------------------------------------------------------------
# Sticky consumed-track evidence
# ---------------------------------------------------------------------


def test_fire_renderer_ended_records_sticky_consumed_track_id():
    """When the watchdog fires with a next URI pre-staged, it records
    WHICH TIDAL track the renderer advanced into. That evidence must
    outlive the watchdog generation reset so the player can match a cast
    preload after the frontend's follow-up play arrives."""
    session = _session(_FakeAV())
    session.renderer_watch_gen = 3
    session.renderer_next_uri = "http://x/dlna/stream?ts=9"
    session.renderer_next_track_id = "tid-n1"
    mgr = _manager(session)
    fired = threading.Event()
    mgr._renderer_ended_callback = lambda: fired.set()

    mgr._fire_renderer_ended(session, 3, "test", consumed=True)

    assert fired.is_set()
    assert session.renderer_consumed_next is True
    assert session.renderer_consumed_track_id == "tid-n1"


def test_deadline_fires_with_staged_uri_but_records_no_evidence(monkeypatch):
    """A renderer that ignores the staged NextURI must not stall forever:
    the deadline fires even while a URI is staged, and records no
    consumption evidence so the player falls back to announcing instead
    of falsely adopting a track the renderer never switched to."""
    monkeypatch.setattr(upnp_mod, "_RENDERER_EOS_MARGIN_S", 0.0)
    staged = "http://x/dlna/stream?ts=9"
    # Position pinned at 0 (never reaches duration) and CurrentURI is not
    # the staged URI, so neither signal 1 nor signal 2 can fire.
    av = _FakeAV(positions=[(0.0, 1.0)] * 2000, current_uri=None)
    session = _session(av)
    session.renderer_next_uri = staged
    session.renderer_next_track_id = "tid-n1"
    mgr = _manager(session)
    fired = threading.Event()
    mgr._renderer_ended_callback = lambda: fired.set()

    _run_watch(mgr, session, fallback=1.0)

    assert fired.wait(timeout=3.0)
    assert session.renderer_consumed_next is False
    assert session.renderer_consumed_track_id is None


def test_arm_renderer_watch_preserves_sticky_consumed_track_id():
    session = _session(_FakeAV())
    session.renderer_consumed_track_id = "tid-n1"
    session.renderer_next_track_id = "tid-n1"
    mgr = _manager(session)

    mgr._arm_renderer_watch(session, MagicMock(), {"duration_s": 30})

    assert session.renderer_consumed_track_id == "tid-n1"
    assert session.renderer_next_track_id is None

