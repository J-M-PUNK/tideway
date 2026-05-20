"""Tidal realtime listener: pause Tideway when another device on the
same Tidal account starts playing.

Tidal's official clients react to playback events from other devices
via a realtime bus. When another device on the same account begins a
playback session, the bus pushes a state-change frame and the
receiving client pauses locally. Spotify and the official Tidal app
both behave this way out of the box; users coming from those expect
Tideway to behave the same.

The send half of this contract already exists. `app/play_reporter.py`
posts our own `playback_session` events to the event-producer bus
(`ec.tidal.com/api/event-batch`), which is what makes "Tideway plays
show up in Recently Played" and "starting Tideway pauses other
devices that listen to the same bus" work today. This module is the
**receive** half: a long-lived WebSocket subscription to Tidal's
realtime bus ("Pushkin"), with an `on_other_device_started` callback
that the launcher wires to `PCMPlayer.pause()`.

## Protocol

Reverse-engineered from the Tidal web client's `pushkin-*.js` module
(2026-05-20). Two phases:

  1. **Token acquisition.** POST to `https://api.tidal.com/v1/rt/
     connect` with `Authorization: Bearer <access_token>`. Response
     is `{"url": "wss://pushkin-v2.tidal.com/public/token/<uuid>/
     ws"}` — the full WebSocket URL including a per-session token
     baked into the path. Tokens are minted per connection; we
     re-acquire on every (re)connect.

  2. **WebSocket session.** Plain JSON frames. Incoming types we
     handle:
       - `PRIVILEGED_SESSION_NOTIFICATION` — payload has
         `clientDisplayName` (e.g. "iOS") and `sessionId`. Fires
         when another device on the same Tidal account takes
         playback. We pause local playback in response.
       - `RECONNECT` — server-initiated reconnect. Close and
         reopen.
       - Anything else: log and ignore.

     Outgoing frames are optional. The web client only sends
     `USER_ACTION` messages in response to page user-action events
     (clicks, keypresses); they're not heartbeats and Tidal's
     backend doesn't seem to require them for connection liveness,
     so we don't send anything.

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

## Self-event filtering

The web client's frame handler pauses unconditionally on every
`PRIVILEGED_SESSION_NOTIFICATION` — no self-filter by sessionId.
This works in practice because Tidal's backend doesn't echo a
client's own session events back to that same client's connection.
We follow the same posture: pause callback fires unconditionally;
trust the server to not push our own events to us. If we ever see
self-pause from a Tideway-only-playing scenario, the fix is to
compare against the sessionId play_reporter generated for the
active session.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


# HTTP endpoint that mints a per-session WebSocket URL. Returns
# {"url": "wss://pushkin-v2.tidal.com/public/token/<uuid>/ws"}.
# Reverse-engineered from `pushkin-*.js`'s `l()` function.
_RT_CONNECT_URL = "https://api.tidal.com/v1/rt/connect"

# Backoff schedule for reconnects. Doubles each consecutive failure,
# capped so we don't drift into multi-minute holes that delay the
# user's first cross-device-pause event after a network blip. The
# first failure waits half a second; the cap of 60s keeps us
# responsive while still being polite to Tidal's bus during outages.
_BACKOFF_INITIAL_SEC = 0.5
_BACKOFF_MAX_SEC = 60.0

# How long to wait for the token POST + WebSocket handshake before
# giving up and treating it as a connection failure. The token POST
# is a single HTTPS round-trip against api.tidal.com; the handshake
# is a single TLS+WS upgrade. Tens of seconds is generous; longer
# than that means something's wrong (DNS, blocked, captive portal)
# and we should back off, not wait.
_CONNECT_TIMEOUT_SEC = 10.0


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

    # One of: "idle", "connecting", "connected", "reconnecting",
    # "stopped". "idle" means the listener was constructed but
    # start() hasn't been called.
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
        """Reverse-engineered protocol is in place since 2026-05-20;
        kept on the public surface for the `/api/realtime/status`
        diagnostic endpoint and for any callers that want to gate
        on it. Always True now."""
        return True

    def start(self) -> None:
        """Spawn the connection task. Idempotent: a second call while
        already running is a no-op."""
        if self._task is not None and not self._task.done():
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
        """Acquire a token, open the Pushkin WebSocket, and serve
        frames until the connection drops. Raised exceptions trigger
        reconnect via _run().

        Lazy import of aiohttp + websockets so test environments that
        construct the listener without an event loop don't fail at
        import time on a missing optional dep.
        """
        access_token = self.token_provider()
        if not access_token:
            # No Tidal session right now (logged out, refresh in
            # flight). Raise so _run() backs off and tries again
            # once the session is back.
            raise RuntimeError("no Tidal access token available")

        import aiohttp
        import websockets

        # Phase 1: POST to /v1/rt/connect with the Bearer token to
        # mint a per-session WebSocket URL. The response carries the
        # full wss:// URL including the token UUID baked into the
        # path; we don't need to construct it ourselves.
        async with aiohttp.ClientSession() as http:
            async with http.post(
                _RT_CONNECT_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=_CONNECT_TIMEOUT_SEC),
            ) as resp:
                if resp.status == 401:
                    # Token rejected — the access_token from the
                    # provider is stale. Force a refresh next time
                    # by surfacing this as a specific error.
                    raise RuntimeError(
                        "Pushkin token request returned 401; access "
                        "token rejected"
                    )
                resp.raise_for_status()
                payload = await resp.json()
        ws_url = payload.get("url")
        if not ws_url:
            raise RuntimeError(
                f"Pushkin token response missing 'url': {payload!r}"
            )

        # Phase 2: open the WebSocket and serve frames. No subscribe
        # message and no client heartbeats — the Tidal web client
        # sends `USER_ACTION` frames on page user-action events but
        # they're not required for connection liveness, so we skip
        # them.
        async with websockets.connect(
            ws_url,
            open_timeout=_CONNECT_TIMEOUT_SEC,
        ) as ws:
            self._status.phase = "connected"
            self._status.last_error = None
            log.info(
                "tidal_realtime: connected to Pushkin (reconnect_count=%d)",
                self._status.reconnect_count,
            )
            async for raw in ws:
                frame = self._parse_frame(raw)
                if frame is None:
                    continue
                await self._dispatch_frame(frame, ws)

    async def _dispatch_frame(self, frame: dict, ws) -> None:
        """Route a parsed frame to the right action.

        - PRIVILEGED_SESSION_NOTIFICATION → callback (pause local).
        - RECONNECT → close socket; _run()'s outer loop reopens.
        - anything else → log + ignore (forward-compat).
        """
        frame_type = frame.get("type")
        if frame_type == "PRIVILEGED_SESSION_NOTIFICATION":
            self._status.events_received += 1
            payload = frame.get("payload") or {}
            log.info(
                "tidal_realtime: PRIVILEGED_SESSION_NOTIFICATION "
                "from %s; firing pause callback",
                payload.get("clientDisplayName") or "<unknown>",
            )
            try:
                result = self.on_other_device_started(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception(
                    "tidal_realtime: on_other_device_started "
                    "callback raised"
                )
        elif frame_type == "RECONNECT":
            log.info("tidal_realtime: server-initiated reconnect")
            await ws.close()
        else:
            log.warning(
                "tidal_realtime: unhandled frame type %r", frame_type
            )

    @staticmethod
    def _parse_frame(raw: bytes | str) -> Optional[dict]:
        """Decode a single bus frame. Returns the parsed dict, or
        None if the frame isn't a JSON object we can route on.
        Garbage frames are non-fatal: log and skip."""
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            obj = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("tidal_realtime: dropped non-JSON frame: %r", exc)
            return None
        if not isinstance(obj, dict):
            log.warning(
                "tidal_realtime: dropped non-object frame: %r", obj
            )
            return None
        return obj


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
