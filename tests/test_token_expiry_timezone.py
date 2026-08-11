"""Token expiry must be compared in the frame it was written in (#309).

`expiry_time` is stored as *naive UTC* — tidalapi builds it with
`datetime.utcnow()` and `_token_refresh_capturing` matches that on
purpose. The readers used to compare it against `datetime.now()`, which
is naive *local* time, so the remaining lifetime was off by the machine's
UTC offset:

  - West of UTC it was overstated. The watchdog only acts inside a five
    minute window, so an offset of hours meant it never fired: the token
    died while the countdown still read hours, and load_session's
    `expiry <= now` test missed for the same reason, skipping the launch
    refresh. The user was signed out with nothing in the log.
  - East of UTC it was understated, so refreshes fired an offset early
    and every launch took the refresh path, rotating the refresh token
    far more often than needed.

Only a machine sitting exactly on UTC behaved correctly, which is how
this survived: the fixtures built their expiry with `datetime.now()`
too, agreeing with the reader and never exercising the writer's frame.

These tests force a fixed local timezone so they assert the same thing
no matter where they run. Both zones are DST-free (Phoenix is always
UTC-7, Tokyo always UTC+9) so the arithmetic can't drift with the season.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import tidal_client
from app.tidal_client import TidalClient, _now_matching

pytestmark = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="TZ override requires time.tzset (POSIX)"
)

WEST = "America/Phoenix"  # UTC-7 year round
EAST = "Asia/Tokyo"  # UTC+9 year round


@pytest.fixture
def local_tz():
    """Force the process's local timezone, restoring it afterwards.

    TZ is restored here rather than through monkeypatch because the
    matching `time.tzset()` has to run *after* the env var goes back,
    and monkeypatch's undo happens after this fixture's teardown.
    """
    saved = os.environ.get("TZ")

    def _set(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield _set

    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def _utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------
# The frame helper
# ----------------------------------------------------------------------


@pytest.mark.parametrize("zone", [WEST, EAST])
def test_now_matching_tracks_utc_not_the_local_clock(local_tz, zone):
    local_tz(zone)
    naive_expiry = _utc_naive()

    now = _now_matching(naive_expiry)

    assert abs((now - _utc_naive()).total_seconds()) < 5
    # And is emphatically not the local wall clock, which is what the
    # bug compared against.
    assert abs((now - datetime.now()).total_seconds()) > 3600


def test_now_matching_honours_an_aware_expiry(local_tz):
    local_tz(WEST)
    aware = datetime.now(timezone.utc)

    now = _now_matching(aware)

    assert now.tzinfo is not None
    assert abs((now - aware).total_seconds()) < 5


# ----------------------------------------------------------------------
# The refresh watchdog
# ----------------------------------------------------------------------


class _FakeSession:
    def __init__(self, expiry, refresh_token="R1"):
        self.expiry_time = expiry
        self.refresh_token = refresh_token


def _watchdog_client(expiry):
    """A client with just enough wired up to run one watchdog tick.

    Built without __init__ on purpose: the real constructor opens a
    tidalapi session and starts the watchdog thread, neither of which
    this test wants. The loop body under test only touches the
    attributes set here.
    """
    import threading

    c = object.__new__(TidalClient)
    c.session = _FakeSession(expiry)
    c._refresh_stop = threading.Event()
    c._session_load_deferred = False
    c._REFRESH_CHECK_INTERVAL_SEC = 0.01
    return c


def _run_one_tick(client):
    """Run the watchdog until it either refreshes once or ticks twice."""
    refreshes = []
    ticks = {"n": 0}

    def _force_refresh():
        refreshes.append(True)
        client._refresh_stop.set()

    real_wait = client._refresh_stop.wait

    def _counting_wait(timeout=None):
        ticks["n"] += 1
        if ticks["n"] > 2:
            # No refresh is coming; end the loop so the test can assert.
            return True
        return real_wait(timeout)

    client.force_refresh = _force_refresh
    client._refresh_stop.wait = _counting_wait
    client._refresh_watchdog()
    return bool(refreshes)


def test_watchdog_refreshes_a_nearly_expired_token_west_of_utc(local_tz):
    """The silent-logout case. With a UTC-7 local clock the old code read
    a token expiring in 60s as having ~7 hours left and never refreshed
    it, so it died and the next call bounced the user to login."""
    local_tz(WEST)
    client = _watchdog_client(_utc_naive() + timedelta(seconds=60))

    assert _run_one_tick(client) is True


def test_watchdog_leaves_a_healthy_token_alone_east_of_utc(local_tz):
    """The churn case. With a UTC+9 local clock the old code read a token
    with three hours left as long expired and refreshed it immediately,
    rotating the refresh token on every tick."""
    local_tz(EAST)
    client = _watchdog_client(_utc_naive() + timedelta(hours=3))

    assert _run_one_tick(client) is False


# ----------------------------------------------------------------------
# Relaunch
# ----------------------------------------------------------------------


def test_launch_refresh_fires_for_an_expired_token_west_of_utc(
    local_tz, tmp_path, monkeypatch
):
    """A token that really expired two hours ago looked five hours in the
    *future* to a UTC-7 local clock, so load_session skipped the launch
    refresh and handed Tidal a dead token instead."""
    local_tz(WEST)
    import server

    f = tmp_path / "tidal_session.json"
    f.write_text(
        json.dumps(
            {
                "token_type": "Bearer",
                "access_token": "stale",
                "refresh_token": "R1",
                "expiry_time": (_utc_naive() - timedelta(hours=2)).isoformat(),
                "is_pkce": False,
            }
        )
    )
    monkeypatch.setattr(tidal_client, "SESSION_FILE", f)

    t = server.tidal
    monkeypatch.setattr(t, "_session_load_deferred", False)
    monkeypatch.setattr(t, "save_session", lambda: None)
    refreshed = []
    monkeypatch.setattr(
        t, "_token_refresh_capturing", lambda rt: refreshed.append(rt) or True
    )
    monkeypatch.setattr(t.session, "load_oauth_session", lambda *a, **k: None)
    monkeypatch.setattr(t.session, "check_login", lambda: True)

    assert t.load_session() is True
    assert refreshed == ["R1"], "launch refresh never ran for an expired token"
