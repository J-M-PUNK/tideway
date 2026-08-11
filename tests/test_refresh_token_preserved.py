"""Saving a session must never throw away the refresh token (#309).

The refresh token is the only credential that can renew a session, and
an absent value on the session object means "unchanged", not "revoked":
Tidal omits the field unless it rotates, and tidalapi drops it when it
does. `_token_refresh_capturing` already applies that rule at the HTTP
layer. `save_session` did not, so one save could undo the other and
write a null over a perfectly good token.

The result is a session that cannot be renewed. The watchdog skips a
session with no refresh token, so the access token simply expires and
the user is signed out with nothing in the log. This was found on a real
install: a session file written with a null refresh token and a four
hour access token, signed out on the dot when it expired, and no
`[tidal]` line anywhere near the event.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import tidal_client


@pytest.fixture
def session_file(tmp_path, monkeypatch):
    f = tmp_path / "tidal_session.json"
    monkeypatch.setattr(tidal_client, "SESSION_FILE", f)
    return f


@pytest.fixture
def tidal(monkeypatch):
    import server

    return server.tidal


def _write(f, refresh_token):
    f.write_text(
        json.dumps(
            {
                "token_type": "Bearer",
                "access_token": "A1",
                "refresh_token": refresh_token,
                "expiry_time": datetime.now(timezone.utc)
                .replace(tzinfo=None)
                .isoformat(),
                "is_pkce": False,
            }
        )
    )


def _set_session(monkeypatch, tidal, **attrs):
    for name, value in attrs.items():
        monkeypatch.setattr(tidal.session, name, value, raising=False)


# ----------------------------------------------------------------------
# save_session
# ----------------------------------------------------------------------


def test_a_session_without_a_refresh_token_keeps_the_stored_one(
    session_file, tidal, monkeypatch
):
    """The bug. Saving a session whose refresh token went missing used to
    persist the None and permanently disable renewal."""
    _write(session_file, "GOOD")
    _set_session(
        monkeypatch,
        tidal,
        token_type="Bearer",
        access_token="A2",
        refresh_token=None,
        expiry_time=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    tidal.save_session()

    assert json.loads(session_file.read_text())["refresh_token"] == "GOOD"


def test_an_empty_string_is_treated_as_missing(session_file, tidal, monkeypatch):
    _write(session_file, "GOOD")
    _set_session(
        monkeypatch,
        tidal,
        token_type="Bearer",
        access_token="A2",
        refresh_token="",
        expiry_time=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    tidal.save_session()

    assert json.loads(session_file.read_text())["refresh_token"] == "GOOD"


def test_a_rotated_refresh_token_still_replaces_the_stored_one(
    session_file, tidal, monkeypatch
):
    """The guard must not pin the old token in place — rotation has to
    keep working, or Tidal invalidates us a few days later."""
    _write(session_file, "OLD")
    _set_session(
        monkeypatch,
        tidal,
        token_type="Bearer",
        access_token="A2",
        refresh_token="NEW",
        expiry_time=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    tidal.save_session()

    assert json.loads(session_file.read_text())["refresh_token"] == "NEW"


def test_first_save_with_no_file_writes_what_it_has(
    session_file, tidal, monkeypatch
):
    """A fresh login has nothing on disk to preserve."""
    assert not session_file.exists()
    _set_session(
        monkeypatch,
        tidal,
        token_type="Bearer",
        access_token="A1",
        refresh_token="R1",
        expiry_time=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    tidal.save_session()

    assert json.loads(session_file.read_text())["refresh_token"] == "R1"


def test_a_malformed_file_does_not_break_saving(session_file, tidal, monkeypatch):
    session_file.write_text("{ not json")
    _set_session(
        monkeypatch,
        tidal,
        token_type="Bearer",
        access_token="A1",
        refresh_token=None,
        expiry_time=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    tidal.save_session()

    assert json.loads(session_file.read_text())["refresh_token"] is None


# ----------------------------------------------------------------------
# The watchdog's silence
# ----------------------------------------------------------------------


class _FakeSession:
    def __init__(self, expiry, refresh_token):
        self.expiry_time = expiry
        self.refresh_token = refresh_token


def _run_watchdog_ticks(client, ticks=3):
    seen = {"n": 0}
    real_wait = client._refresh_stop.wait

    def _counting_wait(timeout=None):
        seen["n"] += 1
        if seen["n"] > ticks:
            return True
        return real_wait(0)

    client._refresh_stop.wait = _counting_wait
    client._refresh_watchdog()


def _unrenewable_client():
    import threading

    from app.tidal_client import TidalClient

    c = object.__new__(TidalClient)
    c.session = _FakeSession(
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=4),
        None,
    )
    c._refresh_stop = threading.Event()
    c._session_load_deferred = False
    c._warned_no_refresh_token = False
    c._REFRESH_CHECK_INTERVAL_SEC = 0
    return c


def test_the_watchdog_says_a_session_can_never_be_renewed(audio_lines):
    """Without this the loop skips such a session every tick in silence,
    which is why the real logout left no trace at all."""
    _run_watchdog_ticks(_unrenewable_client())

    assert any("no refresh token" in line for line in audio_lines)


def test_the_warning_is_not_repeated_every_tick(audio_lines):
    """The loop runs once a minute; repeating this would bury the log."""
    _run_watchdog_ticks(_unrenewable_client(), ticks=5)

    assert sum("no refresh token" in line for line in audio_lines) == 1


@pytest.fixture
def audio_lines():
    """What reaches the logger behind audio.log.

    Attached to that logger directly: app.audio.player sets
    propagate=False on it, so caplog's root handler never sees these.
    """
    import logging

    lines = []

    class _Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("tideway.audio")
    handler = _Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
