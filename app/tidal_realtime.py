"""Tidal realtime listener: pause Tideway when another device on the
same Tidal account starts playing.

Tidal's official clients react to playback events from other devices
via a realtime bus. When another device on the same account begins a
playback_session, the bus pushes a state-change frame and the
receiving client pauses locally. Spotify and the official Tidal app
both behave this way out of the box; users coming from those expect
Tideway to behave the same.

The send half of this contract already exists. `app/play_reporter.py`
posts our own `playback_session` events to the event-producer bus
(`ec.tidal.com/api/event-batch`), which is what makes "Tideway plays
show up in Recently Played" and "starting Tideway pauses other
devices that listen to the same bus" work today. This module is the
**receive** half: a long-lived WebSocket subscription to whatever
Tidal's realtime bus is, with an `on_other_device_started` callback
that the desktop launcher wires to PCMPlayer.pause().

## Status: scaffolded, protocol capture pending

The scaffold is here so the wiring (settings field, lifespan hook,
diagnostic endpoint, callback into the player) can be reviewed and
shipped without the protocol-specific details. Connection setup,
the subscribe message, and the frame parser are stubs marked
**TODO(phase-1)** that need a packet capture from Tidal's web
client to fill in. See
`private/features/cross-device-pause-listener.md` for the capture
plan and the open questions about auth shape, frame format, etc.

Until the capture lands, the listener:
  - Constructs cleanly,
  - Reports `phase=disabled` from `status()` because the protocol
    constants are placeholders,
  - Refuses to start when called: returns immediately with a logged
    "TODO" message rather than spinning a real WebSocket connection
    against a guessed URL that can't actually work.

This shape lets the rest of the app integrate (settings toggle,
status endpoint, lifespan registration) without producing connection
churn against a wrong endpoint.

## Threading model

The listener runs inside FastAPI's asyncio event loop as a background
task spawned from lifespan startup. The task lifecycle is:

  start() → asyncio.create_task → _run() loop:
      while not shutdown:
          try: await _connect_and_serve()
          except: log + backoff
          await sleep(backoff)

Cancellation on shutdown is cooperative: lifespan teardown calls
stop(), which sets a shutdown flag and cancels the task; _run() sees
the cancellation, closes the WebSocket if open, and exits.

The pause callback fires from inside the asyncio loop. PCMPlayer's
methods are thread-safe (they take `_lock` internally) so calling
into them from the loop is fine; we don't need a thread hop.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol constants — TODO(phase-1): replace placeholders after capture.
# ---------------------------------------------------------------------------
#
# `cross-device-pause-listener.md` records the open questions:
#   - Is the realtime endpoint really WebSocket, or long-polling / SSE?
#   - Auth shape: bearer header, query param, post-connect AUTH frame?
#   - Do we need a "register this session" handshake first?
#   - Frame format: JSON, binary, protobuf?
#   - Are there events we'd want to ignore (our own play_reporter
#     events bouncing back)?
#
# Until those are answered the listener stays in `disabled` phase and
# never opens a connection. Replacing these constants and the
# `_parse_frame` helper below is the entire surface the Phase 1
# capture changes.

_REALTIME_URL: Optional[str] = None  # e.g. "wss://realtime.tidal.com/v1/listen"
_SUBSCRIBE_MESSAGE: Optional[str] = None  # JSON sent on connect, if needed
_HEARTBEAT_INTERVAL_SEC: Optional[float] = None  # client-side ping cadence


# Backoff schedule for reconnects. Doubles each consecutive failure,
# capped so we don't drift into multi-minute holes that delay the
# user's first cross-device-pause event after a network blip. The
# first failure waits half a second; the cap of 60s keeps us
# responsive while still being polite to Tidal's bus during outages.
_BACKOFF_INITIAL_SEC = 0.5
_BACKOFF_MAX_SEC = 60.0


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

# Function the listener calls to obtain the current Tidal access token.
# Returning None means "no session right now" and the listener will
# back off. Tokens rotate on refresh, so the listener calls this every
# time it (re)connects rather than caching once.
TokenProvider = Callable[[], Optional[str]]

# Callback fired when another device on the same account starts
# playing. The desktop launcher binds this to PCMPlayer.pause(). The
# callback runs on the asyncio loop and may be sync or async.
OtherDeviceStartedCallback = Callable[[dict], Awaitable[None] | None]


@dataclass
class ListenerStatus:
    """What `/api/realtime/status` returns. Diagnostic surface only."""

    # One of: "disabled", "idle", "connecting", "connected",
    # "reconnecting", "stopped". "disabled" specifically means the
    # protocol constants haven't been filled in yet (Phase 1 not
    # done). "idle" means the listener was constructed but start()
    # hasn't been called.
    phase: str = "idle"
    # Last error message, if any. Cleared on successful connect.
    last_error: Optional[str] = None
    # How many times the listener has reconnected since start(). 0
    # is a fresh listener that hasn't reconnected yet.
    reconnect_count: int = 0
    # How many "other device started" events we've delivered to the
    # callback since start(). Useful sanity check that the wire-up
    # is working when capturing fresh fixtures.
    events_received: int = 0


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


@dataclass
class RealtimeListener:
    """Long-lived connection to Tidal's realtime bus.

    Construct with a token provider and an event callback. Call
    start() to spawn the connection task; call stop() to cancel it.
    Status is observable via status() at any time.
    """

    token_provider: TokenProvider
    on_other_device_started: OtherDeviceStartedCallback

    # Internal state. Not user-tunable.
    _task: Optional[asyncio.Task] = field(default=None, init=False)
    _shutdown: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _status: ListenerStatus = field(default_factory=ListenerStatus, init=False)

    @property
    def is_protocol_known(self) -> bool:
        """True once the Phase 1 capture has populated the protocol
        constants. Until then start() is a no-op and status reports
        `disabled`."""
        return _REALTIME_URL is not None

    def start(self) -> None:
        """Spawn the connection task. Idempotent: a second call while
        already running is a no-op."""
        if self._task is not None and not self._task.done():
            return
        if not self.is_protocol_known:
            self._status.phase = "disabled"
            self._status.last_error = (
                "Tidal realtime protocol not captured yet; listener "
                "is a no-op until Phase 1 of the cross-device-pause "
                "feature lands. See "
                "private/features/cross-device-pause-listener.md."
            )
            log.info(
                "tidal_realtime: start() called but protocol unknown; "
                "no connection will be opened"
            )
            return
        self._shutdown.clear()
        self._status = ListenerStatus(phase="connecting")
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(
            self._run(), name="tidal-realtime-listener"
        )

    def stop(self) -> None:
        """Cancel the connection task. Safe to call multiple times."""
        self._shutdown.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._status.phase = "stopped"

    def status(self) -> ListenerStatus:
        """Snapshot of the listener's current state. The dataclass is
        frozen at the moment of read; callers don't need to lock."""
        return ListenerStatus(
            phase=self._status.phase,
            last_error=self._status.last_error,
            reconnect_count=self._status.reconnect_count,
            events_received=self._status.events_received,
        )

    # -- internals ----------------------------------------------------------

    async def _run(self) -> None:
        """Main connect-and-serve loop. Reconnects on drop with
        exponential backoff. Exits when _shutdown is set."""
        backoff = _BACKOFF_INITIAL_SEC
        first_pass = True
        while not self._shutdown.is_set():
            if not first_pass:
                self._status.phase = "reconnecting"
                self._status.reconnect_count += 1
            first_pass = False
            try:
                await self._connect_and_serve()
                # Clean exit means the server closed without an error;
                # treat as a brief blip and reconnect promptly.
                backoff = _BACKOFF_INITIAL_SEC
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._status.last_error = repr(exc)
                log.warning(
                    "tidal_realtime: connection error; backing off %.1fs: %r",
                    backoff, exc,
                )
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=backoff
                )
                # Shutdown signalled mid-backoff; exit cleanly.
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, _BACKOFF_MAX_SEC)

    async def _connect_and_serve(self) -> None:
        """Open one WebSocket connection, subscribe, and serve frames
        until the connection drops. Re-raised exceptions trigger
        reconnect via _run().

        TODO(phase-1): this method is a placeholder. The real
        implementation will:
          1. Read the access token via self.token_provider().
          2. Open a WebSocket to _REALTIME_URL with the right auth
             shape (header / query param / post-connect frame, TBD).
          3. Send _SUBSCRIBE_MESSAGE if the protocol requires one.
          4. Loop reading frames; for each, call _parse_frame() and
             dispatch other-device-started events to the callback.
          5. Heartbeat at _HEARTBEAT_INTERVAL_SEC if the bus needs
             keep-alives.
        """
        # Sanity belt: the public start() method already checked
        # is_protocol_known and won't have spawned us if the protocol
        # is unknown, but we double-check here in case start() is
        # ever called before constants land.
        if not self.is_protocol_known:
            raise RuntimeError("realtime protocol constants not set")
        # Real implementation lands here in Phase 1. For now, raise
        # so _run()'s reconnect loop logs and backs off; once
        # constants are populated this becomes the actual connect
        # and serve loop.
        raise NotImplementedError(
            "tidal_realtime._connect_and_serve: protocol not captured"
        )

    @staticmethod
    def _parse_frame(raw: bytes | str) -> Optional[dict]:
        """Decode a single bus frame into a structured dict.

        TODO(phase-1): implement once we know whether frames are
        JSON, binary, or protobuf. Stubbed to return None so the
        dispatcher in _connect_and_serve treats unknown frames as
        unhandled (which is the correct behavior for events we
        don't recognize even after the capture).
        """
        return None


# ---------------------------------------------------------------------------
# Module-level singleton + start hook
# ---------------------------------------------------------------------------

_listener: Optional[RealtimeListener] = None


def get_listener() -> Optional[RealtimeListener]:
    """Return the module-level listener if one was started, else None."""
    return _listener


def start_listener(
    token_provider: TokenProvider,
    on_other_device_started: OtherDeviceStartedCallback,
) -> RealtimeListener:
    """Construct (or reuse) the module-level listener and start it.

    Idempotent. Subsequent calls return the existing listener; the
    token_provider and callback from the first call win. Lifespan
    startup is the only intended caller.
    """
    global _listener
    if _listener is None:
        _listener = RealtimeListener(
            token_provider=token_provider,
            on_other_device_started=on_other_device_started,
        )
    _listener.start()
    return _listener


def stop_listener() -> None:
    """Shut down the module-level listener if it was started.
    Idempotent."""
    if _listener is not None:
        _listener.stop()
