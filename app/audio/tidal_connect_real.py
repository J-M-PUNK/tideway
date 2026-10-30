"""Real Tidal Connect controller. JSON-over-WSS.

This is the actual Tidal Connect protocol controller, decoded from
the official Tidal desktop client's bundled source. Sister module
to `app/audio/tidal_connect.py`, which mimics Tidal Connect via the
public OpenHome SOAP surface; that module remains as a fallback for
OpenHome-only devices that aren't in the Tidal Connect ecosystem.

## Protocol summary

  Discovery:  mDNS service type `_tidalconnect._tcp.local`
  Transport:  WebSocket Secure (wss://<addr>:<port>)
  TLS trust:  embedded TIDAL Root CA + TIDAL TS CA bundle (below)
  Wire:       JSON envelopes with `command` and `requestId`

The full spec, captured from the desktop client's TypeScript
sourcemaps, is in `private/features/tidal-connect-real-spec.md`.

## Playback handoff

Track playback creates a Tidal cloud queue and sends loadCloudQueue with
server-assigned queue/item IDs and receiver authentication. The receiver
fetches and decodes the audio. Validated against a WiiM Pro; device and
firmware coverage beyond that remains experimental.

## Threading model

Discovery and the WSS connection both run inside an asyncio event
loop spawned in a daemon thread (mirrors `app/audio/cast.py`'s
posture). The public `TidalConnectRealManager` exposes a sync API
that posts coroutines to the loop with `asyncio.run_coroutine_
threadsafe(...)`. PCMPlayer never sees this; the manager calls
into the player's `set_external_output_active(True)` while a
session is open so local audio mutes, same as Cast and DLNA.

The pause callback into the player fires from the loop's thread.
PCMPlayer's methods are thread-safe (lock-protected internally),
so no thread hop is needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 4.0
DISCONNECT_TIMEOUT = 6.0


class TidalConnectProtocolError(RuntimeError):
    """A receiver rejection, containing only safe protocol identifiers."""

    def __init__(self, command: str, code: Any = None) -> None:
        self.command = command
        self.code = code
        suffix = f" (code {code})" if isinstance(code, (int, float)) else ""
        super().__init__(f"Tidal Connect {command} failed{suffix}")


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

# mDNS service type Tidal Connect receivers register under. Captured
# from the desktop client's mDNS browser config in
# `app/main/tidalConnect/TidalConnectController.js:519`:
#
#   serviceName: '_tidalconnect._tcp.local'
TIDAL_CONNECT_SERVICE = "_tidalconnect._tcp.local."

# Default app identity sent on `startSession`. The desktop client
# uses Tidal's own appId; using ours flags the session as Tideway
# in the device's session log without changing protocol behaviour.
# Both fields are honoured by the device but not validated against
# any registry, so we can choose freely. Pick a stable identifier
# so log lines on the device's side stay correlated across sessions.
# The official Tidal desktop client identifies as "tidal" / "tidal"
# on startSession. Verified by capturing a real session against a
# faked-receiver rig (see private/tools/tidal-connect-capture/).
# Receivers may match against a whitelist; using the same string the
# desktop client uses is the safest bet.
DEFAULT_APP_ID = "tidal"
DEFAULT_APP_NAME = "tidal"

# Reconnect schedule. Doubles each consecutive failure, capped so
# we don't drift into multi-minute holes during outages.
_BACKOFF_INITIAL_SEC = 0.5
_BACKOFF_MAX_SEC = 60.0


# ---------------------------------------------------------------------------
# CA bundle
# ---------------------------------------------------------------------------
#
# The desktop client validates device certs against a custom CA
# bundle (TIDAL Root CA + TIDAL TS CA) and ignores the system trust
# store. Both certs sit verbatim in the desktop client's
# websocket.js source. They're long-lived (Root CA valid until
# 2040, TS CA until 2040), so embedding directly is fine for the
# foreseeable future. If Tidal rotates either, devices stop being
# reachable until we ship an update.
#
# Tideway extracts these from /Applications/TIDAL.app at build
# time? No, we just inline them. They're not secret; the desktop
# client ships them in the clear, and any device firmware that
# implements Tidal Connect carries the same chain.
_TIDAL_CA_BUNDLE_PEM = """\
-----BEGIN CERTIFICATE-----
MIIFrzCCA5egAwIBAgIQNBdQrsnhXAku8aG8fUI9xTANBgkqhkiG9w0BAQsFADA1
MQswCQYDVQQGEwJubzEOMAwGA1UECgwFVElEQUwxFjAUBgNVBAMMDVRJREFMIFJv
b3QgQ0EwHhcNMjAwMzE3MDgwMzQ2WhcNNDAwMjAxMDkwMzQ2WjAzMQswCQYDVQQG
EwJubzEOMAwGA1UECgwFVElEQUwxFDASBgNVBAMMC1RJREFMIFRTIENBMIICIjAN
BgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAvuBuw3NjdQA2ofGvvOpJnJp89q+f
AWLPoaqGrWaaJuw/4cUFP+kz/SiVuexsBHnbl1D621lq+grT0vfrgL0X5eZXBtKn
rIlX66yXr/RcLCapI4OjDLiQ/kAd0qJKx6QNbie7VOyJTLd9hXDhngtCwTBP91j5
e5hVK33SYQ3wLNF1jubG561Ct/aKNBTO1NrVtk5hOgnTlIMnfuP+kqbhKWRv/oU+
Nq+qfbY39aRnbTgoZ9xOz7vygum+Vkq0R9Bx8KZqbXj8n8OsdxA+/r82DOSY3lma
D2CMbKqqfXctKYE68YYfQpVFDs1laXcZgXlGLKwqBR9bTL+hY8waC9v6TzEBmU+a
PUqdH+14NU5MRarGA4pbCgBpUPW8uAG998hBEmXydlm/d8WUOS+1odywNwAiw3qc
zf9CbkBDtMhSjSIvcFmstX6/nFWWeloXKOAX37ojcIfpR/IDsNVdxIUqTHlwj2xr
mLu8ZbKdkZAl7sAjC1klfA7lBls2ZoIxVmIHU9o8e99YMVcYHnqX0OqLngzL4rra
+ufmBqeolJQHjJuuznXYKz7l3Xz7irVvfzZzJd1d6UgVrCFVbWVFxNMwQsShELip
Xr4ViMxPk4fU5zMACC3pLzJ4mThcImD8+Ym+EIJrDTAL0RN82mG37f6wqXL/noHp
oesYEKpC6FQA8v0CAwEAAaOBvDCBuTASBgNVHRMBAf8ECDAGAQH/AgEAMB8GA1Ud
IwQYMBaAFIqbsL3OGur0wNK2jlhgVKKizg8rMB0GA1UdDgQWBBS3GeQDSguernws
iK7RjGss42v36TAOBgNVHQ8BAf8EBAMCAYYwUwYDVR0fBEwwSjBIoEagRIZCaHR0
cDovL2NybC50aWRhbGhpLmZpL2NybC9kOWI5YjVjMi1hNDJjLTQ2MDQtOGRiNi03
ZTU0Zjk1OGY2Y2IuY3JsMA0GCSqGSIb3DQEBCwUAA4ICAQBZBPGz1OJsHr1NFAPK
swL/NjJK2xK80vwZyiPeog/wJh3HAQEBlj6DUGRKGMZrvg7rj/oUVTa3RQ5hx/tx
jzeHamDDHXSylA2z78wfpzktibiH+4ryHhijpobxvj38tjjVveWYXgH3ge5QIFih
DD+KMRZ14BFJZ77FyJdqStaLxQE+pjRrztuvxbkrxOT1f5/yViumdokNzlT1IbfO
CZFC8M3EOvIjGXWVRpwty+N1RnKW3BCRGN8UNWTVh/+XfHH7dM7a67oGw1/p+k6v
+d+dBzF6Up59xwdKv0mvwtNCf6wiIZgMsniE7QXeDpktCup52/hpD7xNNgp6DZjd
2gaQIJQV+Nq9PCZcdpj/2WNSCxKlAvu9qWkt6dXu9pCgh1EZQK9YERZVR4OqlFUB
wf/3ssiNscV0Y5Ut4dyO2wysMbuGK2cYE46CheoOmh80+Ey9Dkf9QjSeaJZfxab2
x++xZY43/+OcDPKKGJo7INvtAE+m4eHBUQbUrGWoh0hTc5tXrJIS+1CMX66vuuKA
emXuhRzHqiUjwy+558qDTbmDsj1B3fAb4RCXS/zBl9/GxrNDfCVBHYu84j7d2Qtf
7kqX2jY7Sg92tQv4yQQfT395UZCLku4MoaVMZPGb4sFMyg8o7gu0+MBfx/G/dgnP
55wtdVtL3jxezmTavYgqx+qMLA==
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIFNjCCAx6gAwIBAgIQWO2pp96w7ja06H8Vk0VEiDANBgkqhkiG9w0BAQ0FADA1
MQswCQYDVQQGEwJubzEOMAwGA1UECgwFVElEQUwxFjAUBgNVBAMMDVRJREFMIFJv
b3QgQ0EwHhcNMjAwMjA1MTA1NTI1WhcNNDAwMjA1MTE1NTI1WjA1MQswCQYDVQQG
EwJubzEOMAwGA1UECgwFVElEQUwxFjAUBgNVBAMMDVRJREFMIFJvb3QgQ0EwggIi
MA0GCSqGSIb3DQEBAQUAA4ICDwAwggIKAoICAQCfWPE+WVPtda9FP6uIS4l0A8vh
+VsmeUbq7hk9YZmA6HqNm8Lp2YjKBrW/rzDyK7zYF+Qe3/R9+cS0dCXQB1huVTG7
LklY8dY1VwLSdUwJqe1+GGWm9C1UsUU+BZlfFQuFYECb/aQpCMOv9+ivybreb5Tj
PSLM5Ba+O9CEC4+23CTfvCadi0h7miGdxMBqtSd5SaiBHBQyOnYTKGUekrkms09k
5BuY5DBk1IWHbeLz13T65ZrLrMAuFjVD/q4FHw5IJG1iN1QLZKZfVqO89MB1hxFc
aBVIPD5qCi1/vUwsrHOIYWvJGSqvg7h3vz5OR9T3AZbUcMu/uoPs+5cd8a2kza/0
UVXmgy3AwJRBJfTathTwvczgbAzGgZbbht8Jtjl11eMwJ8XuNmRCpyxVkE/ehI+M
euFn8QCG3YawFryrrAd89XBeiEJ3u+Orjc9V8VO26rHkxjmURgrcC+7w6cV/GlB3
8Mb/18nSiyvxYeg5fUkJjwsD6/yOnD7X0nqspoC554HFiz1zZNgt2iKlX9yuzTr2
336+rIr/ijQR9KvTXTEoSH9XKIoJzGp44AK90GVKsiLQYfEFj1RiA22eqx0T/vKE
y32gYv7bfDuY1q+41oTDGAVC4YNSYvfr/HnxZ3FzuGylQYAvWy28aNk64TJJHZrE
JXfer2/t4M9AwMAAmwIDAQABo0IwQDAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQW
BBSKm7C9zhrq9MDSto5YYFSios4PKzAOBgNVHQ8BAf8EBAMCAYYwDQYJKoZIhvcN
AQENBQADggIBAB5mlBJ2MxK77ryz05gp83v2VTV4OfB7DVxfzUZW7eEXnNlhLfFP
X5u+zp3LiAvyt4SpnvTTPHlUzUSYdz5Wiijsd+OO6VQQhma4iDdb7DnIznRBdrZ7
7ahxtebGR2UtTVfRq15KvrWVYTsDOr95nQX88n9gUiCzloahDxYI37FYf7hg0ctC
o56NelbC7zvFcYqPybpabwqaaBINKsV//d0KKAnPY3TWkWOp5lvmYB3wiHKCL16/
rD+uHqbjA8igaAtgrnpiWmxRiajzei9UxIRGGar6JwOWNqbCmPnbBO6TdJqEz4Xs
ylU15u/JMwfeMrCQMsBECdq/R6yxRZ6JRqymndT8c/pwPZ1xdFaaM3CUrE0gMITe
kLU69W2p4Lm1Ul5ST+CoTGfLKwIfeUFSz9k3AG1s8aB3WS5Kjt13cmktuEO2WGRP
iTy0ysjuCVqIoa137/1HepR6NYx7nPQARuC2v2b50rkmXC3wqtYeNuWY8vD0AVyM
VGmriYp/v5UDS0JQCqzStDVzZoJvihssF/Cb9ExUzlYR7nrL09s90UinnGSwZG+l
wt1erm0HKlygg2us7q8xBRTXLJGXcTZ8969C/6uPkKN5s9KHTZrswjXFz+0zLz2y
42V7gw1nqPWH+f7wXqwxhoifvcAHIhNM02lLu6fJat0gQBtivBKEHUaG
-----END CERTIFICATE-----
"""


def _build_ssl_context() -> ssl.SSLContext:
    """Trust only the Tidal CA bundle, skip hostname checks (devices
    use IP-based certs, not DNS). Mirrors the desktop client's
    `ca / checkServerIdentity / rejectUnauthorized=true` posture."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Don't load the system trust store; the device's cert is signed
    # by Tidal's CAs, not a public CA, so the system store is
    # actively wrong for this purpose.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    # `load_verify_locations` accepts cadata if we pass the PEM in
    # memory, avoiding a temp file at runtime.
    ctx.load_verify_locations(cadata=_TIDAL_CA_BUNDLE_PEM)
    return ctx


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiscoveredDevice:
    """A Tidal Connect device that mDNS discovered. id is the mDNS
    service instance name; address/port are the resolved A record +
    SRV port we connect to."""

    id: str
    name: str
    address: str
    port: int


# Token provider: returns the user's current Tidal OAuth access
# token, or None if no session. Called every time we (re)connect to
# a device. Tokens rotate on refresh, so caching once would race
# against tidalapi's refresh path.
TokenProvider = Callable[[], Optional[str]]

# Silencer callback. Called with True when a TC session opens, False
# when it closes. Tideway's PCMPlayer wires this to its
# external-output flag so the local sounddevice output goes silent
# while audio is being directed at the TC device. Mirrors the same
# pattern Cast / OpenHome / DLNA use.
LocalSilencer = Callable[[bool], None]

# Track resolver. Returns a loadCloudQueue payload without command or
# requestId: autoplay, position, queueInfo, currentMediaInfo,
# contentServerInfo and queueServerInfo. It creates the cloud queue
# using the app's Tidal session; the receiver fetches the audio itself.
# The payload contains credentials and must never be logged.
# Retain the existing alias/setter name for callers registering it.
TrackUrlResolver = Callable[[int, Optional[str]], dict]

# Notification callback. Fires for every server-side notification
# (`notifyPlayerStatusChanged`, `notifyMediaInfoChanged`, etc).
# Receiving frame is the parsed JSON dict; the manager forwards
# verbatim and lets the caller route by `command` field.
NotificationCallback = Callable[[dict], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# Connection (one device, one WSS link)
# ---------------------------------------------------------------------------


class TidalConnectConnection:
    """A live WSS connection to one device.

    Constructed by the manager when the user picks a device; torn
    down on disconnect or device drop. Owns the request-id counter
    for this session, the response future map, and the heartbeat
    task. Not intended for direct construction by the rest of the
    app.
    """

    def __init__(
        self,
        device: DiscoveredDevice,
        token_provider: TokenProvider,
        on_notification: NotificationCallback,
    ) -> None:
        self.device = device
        self._token_provider = token_provider
        self._on_notification = on_notification
        self._websocket: Any = None  # `websockets.WebSocketClientProtocol`
        self._next_request_id = 0
        self._pending: dict[int | str, asyncio.Future] = {}
        self._receiver_task: Optional[asyncio.Task] = None
        self._session_id: Optional[str] = None
        self._closed = False
        self._closing = False

    def is_active(self) -> bool:
        return bool(
            not self._closed and not self._closing and self._session_id and self._receiver_task
            and not self._receiver_task.done()
        )

    async def open(self) -> None:
        """Connect, validate TLS, complete the session handshake.
        Raises on any failure; the manager handles retry/backoff."""
        # Lazy import so the rest of the app boots without the
        # `websockets` dep installed (degraded mode).
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "websockets library not installed; cannot open Tidal "
                "Connect WSS"
            ) from exc

        url = f"wss://{self.device.address}:{self.device.port}"
        ssl_ctx = _build_ssl_context()
        self._websocket = await websockets.connect(
            url,
            ssl=ssl_ctx,
            compression=None,  # match desktop client (perMessageDeflate: false)
            # Official controller sends WebSocket control pings at 15s;
            # a missing pong must eventually close the transport.
            ping_interval=15,
            ping_timeout=16,
            open_timeout=10,
            close_timeout=2,
        )
        # Spawn the receiver task before sending startSession so its
        # response is captured.
        loop = asyncio.get_running_loop()
        self._receiver_task = loop.create_task(
            self._receive_loop(), name=f"tc-recv:{self.device.id}"
        )
        try:
            await self._start_session()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Tear down: end the session, cancel receiver, close WSS."""
        self._closing = True
        try:
            if self._session_id and not self._closed:
                # Some receivers never acknowledge endSession. Flush the
                # command before closing WSS without waiting for that reply.
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    await self._websocket.send(json.dumps({
                        "command": "endSession", "sessionId": self._session_id,
                        "stopCasting": False,
                    }))
        except Exception:
            pass
        finally:
            # Cancellation can interrupt endSession when an output switch
            # supersedes a command. The transport must still be released.
            try:
                if self._receiver_task is not None:
                    self._receiver_task.cancel()
                    await asyncio.gather(self._receiver_task, return_exceptions=True)
                if self._websocket is not None:
                    try:
                        async with asyncio.timeout(2.0):
                            await self._websocket.close()
                    except (TimeoutError, OSError):
                        pass
            finally:
                self._closed = True
                self._session_id = None
                self._fail_pending(ConnectionError("Tidal Connect connection closed"))

    # -- request/response ---------------------------------------------------

    def _alloc_request_id(self) -> int:
        rid = self._next_request_id
        self._next_request_id += 1
        return rid

    async def _send(self, frame: dict) -> dict:
        """Serialize and send a frame; await its matching response.
        Frames that don't carry a requestId (`startSession`) need
        to be sent with `_send_unmatched` instead."""
        rid = self._alloc_request_id()
        frame = {**frame, "requestId": rid}
        response = await self._request(frame, rid)
        code = response.get("resultCode", 0)
        if code != 0:
            raise TidalConnectProtocolError(frame["command"], code)
        return response

    async def _request(self, frame: dict, key: int | str) -> dict:
        if self._websocket is None or self._closed:
            raise ConnectionError("Tidal Connect connection closed")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await self._websocket.send(json.dumps(frame))
                return await future
        except TimeoutError:
            raise TimeoutError(f"Tidal Connect {frame['command']} timed out") from None
        finally:
            if self._pending.get(key) is future:
                self._pending.pop(key, None)
            if not future.done():
                future.cancel()

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _send_unmatched(
        self, frame: dict, expected_response_command: str
    ) -> dict:
        """Send a frame that doesn't echo requestId. Match the next
        incoming frame whose command equals expected_response_command.

        Used for the session handshake: `startSession` doesn't carry
        a requestId; the device's `notifySessionStarted` arrives
        unsolicited but is the awaited response.
        """
        # Sentinel id outside the regular space; receiver routes
        # by command name when it sees this id.
        return await self._request(frame, expected_response_command)

    async def _receive_loop(self) -> None:
        """Read frames until the WSS closes; route each to its
        matching pending future or to the notification callback."""
        try:
            async for raw in self._websocket:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    log.warning("tidal_connect_real: dropping non-JSON frame")
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("requestId")
                command = msg.get("command")
                if command == "notifySessionError":
                    self._fail_pending(TidalConnectProtocolError(
                        "session", msg.get("errorCode")))
                    self._closed = True
                    self._session_id = None
                elif command == "notifySessionEnded":
                    self._session_id = None
                # 1) Match by requestId for normal command responses.
                if rid is not None and rid in self._pending:
                    future = self._pending.pop(rid)
                    if not future.done():
                        future.set_result(msg)
                    continue
                # 2) Match by command name for the unmatched-response
                # cases (`notifySessionStarted` after `startSession`).
                if command and command in self._pending:
                    future = self._pending.pop(command)
                    if not future.done():
                        future.set_result(msg)
                    # Notifications still go to the callback so the
                    # rest of the app can react to e.g. the session
                    # starting.
                # 3) Anything else is an unsolicited notification.
                try:
                    result = self._on_notification(msg)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception(
                        "tidal_connect_real: notification callback raised"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "tidal_connect_real: receive loop stopped (%s)", type(exc).__name__
            )
        finally:
            self._closed = True
            self._session_id = None
            self._fail_pending(ConnectionError("Tidal Connect connection closed"))
            try:
                if not self._closing:
                    result = self._on_notification({"command": "connectionLost"})
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                log.warning("tidal_connect_real: connection-close callback failed")

    # -- session ------------------------------------------------------------

    async def _start_session(self) -> None:
        """Open a fresh session on the device. Captures session id
        from the notifySessionStarted response.

        sessionCredential is the user's Tidal user id as a decimal
        string (not a JWT, not a bearer token, not a JSON struct).
        Verified empirically by capturing a real desktop-client
        session against the fake-receiver rig in
        private/tools/tidal-connect-capture/. The device uses this
        id along with its own embedded partner cert and the OAuth
        `grant_type: 'switch_client'` exchange to fetch the user's
        actual session from Tidal's servers. Queue loading supplies
        OAuth authentication separately. The token_provider returns the user
        id; despite the name it is not a token."""
        user_id = self._token_provider() or ""
        session_credential = str(user_id)

        frame = {
            "appId": DEFAULT_APP_ID,
            "appName": DEFAULT_APP_NAME,
            "command": "startSession",
            "sessionCredential": session_credential,
        }
        response = await self._send_unmatched(frame, "notifySessionStarted")
        self._session_id = response.get("sessionId")
        if not self._session_id:
            raise TidalConnectProtocolError("startSession")
        log.info(
            "tidal_connect_real: session started; device=%s",
            self.device.name,
        )

    # -- public command surface --------------------------------------------

    async def play(self) -> dict:
        return await self._send({"command": "play"})

    async def pause(self) -> dict:
        return await self._send({"command": "pause"})

    async def stop(self) -> dict:
        return await self._send({"command": "stop"})

    async def next_track(self) -> dict:
        return await self._send({"command": "next"})

    async def previous_track(self) -> dict:
        return await self._send({"command": "previous"})

    async def seek(self, position_ms: int) -> dict:
        return await self._send({"command": "seek", "position": position_ms})

    async def set_volume(self, level: int) -> dict:
        # Public API uses percent; the receiver protocol uses 0..1.
        return await self._send({"command": "setVolume", "level": max(0, min(100, int(level))) / 100.0})

    async def set_mute(self, mute: bool) -> dict:
        return await self._send({"command": "setMute", "mute": bool(mute)})

    async def set_repeat_mode(self, mode: str) -> dict:
        # mode is "OFF" / "ALL" / "SINGLE" per the desktop client's
        # RepeatMode enum.
        return await self._send({"command": "setRepeatMode", "repeatMode": mode})

    async def set_shuffle(self, shuffle: bool) -> dict:
        return await self._send({"command": "setShuffle", "shuffle": bool(shuffle)})

    async def load_media(self, media_info: dict) -> dict:
        """Load a single track. media_info has the shape
        `tidalConnect/mediaInfo.js` documents: itemId, mediaId,
        srcUrl, streamType, and a metadata dict with title,
        albumTitle, artists, duration (seconds), images. server.py's
        `_tcr_url_resolver` builds it from a tidalapi track."""
        return await self._send(
            {"command": "loadMediaInfo", "mediaInfo": media_info}
        )

    async def load_cloud_queue(self, payload: dict) -> dict:
        """Load an authenticated cloud queue prepared by the track resolver.

        Request identifiers belong to this connection. The caller's
        payload is never mutated or logged because it carries tokens.
        """
        return await self._send({**payload, "command": "loadCloudQueue"})


# ---------------------------------------------------------------------------
# Manager (mDNS browse + lifecycle)
# ---------------------------------------------------------------------------


@dataclass
class TidalConnectRealManager:
    """Discovers Tidal Connect devices and brokers connections.

    Single instance per process; constructed lazily by the
    module-level `manager` accessor below. Exposes a sync API for
    the rest of Tideway to call from the request thread; internally
    drives an asyncio loop on a daemon thread.
    """

    token_provider: TokenProvider
    on_notification: NotificationCallback

    _devices: dict[str, DiscoveredDevice] = field(default_factory=dict, init=False)
    _devices_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _connection: Optional[TidalConnectConnection] = field(default=None, init=False)
    # All connection replacement and commands run on _loop under this
    # lock. HTTP controls may arrive concurrently from different threads.
    _command_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _command_tasks: set[asyncio.Task] = field(default_factory=set, init=False)
    _disconnect_requests: int = field(default=0, init=False)
    _loop: Optional[asyncio.AbstractEventLoop] = field(default=None, init=False)
    _loop_thread: Optional[threading.Thread] = field(default=None, init=False)
    _zeroconf: Any = field(default=None, init=False)
    _browser: Any = field(default=None, init=False)
    _local_silencer: Optional[LocalSilencer] = field(default=None, init=False)
    _track_url_resolver: Optional[TrackUrlResolver] = field(default=None, init=False)
    _track_quality: Optional[str] = field(default=None, init=False)
    # Snapshot of the device's last-reported state. Updated by the
    # notification tap below. Fields mirror the player snapshot the
    # frontend reads, so the status endpoint can serve from here
    # without the UI branching on engine.
    _remote_state: dict = field(default_factory=lambda: {
        "player_state": "IDLE",   # device's notifyPlayerStatusChanged value
        "position_ms": 0,
        "duration_ms": 0,
        "volume": 100,
        "muted": False,
        "media_info": None,        # last notifyMediaInfoChanged payload
        "session_id": None,
        "device_id": None,
        "error": None,
        # Bumps on every notification frame the device sends. The
        # frontend's polling diff uses this to detect "something
        # changed" without inspecting every field; needs to advance
        # whenever the device's state moves, even if our parser
        # doesn't pull anything out of the frame.
        "seq": 0,
    }, init=False)
    _remote_state_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def set_local_silencer(self, fn: LocalSilencer) -> None:
        """Register the PCMPlayer external-output flag setter. Called
        with True when a session opens, False when it closes."""
        self._local_silencer = fn

    def set_track_url_resolver(self, fn: TrackUrlResolver) -> None:
        """Register a callable taking a track id and requested quality.
        It returns a loadCloudQueue payload. Owned by server.py because it needs
        the authenticated Tidal session to create the cloud queue."""
        self._track_url_resolver = fn

    def remote_state(self) -> dict:
        """Snapshot of the device's last-reported state. Safe to call
        from any thread."""
        with self._remote_state_lock:
            return dict(self._remote_state)

    def _record_notification(self, frame: dict) -> None:
        """Tap that updates the manager's remote_state from the
        device's notification frames before forwarding to the user
        callback. Tracks playerState, position, volume, mute, media,
        session id."""
        with self._remote_state_lock:
            self._remote_state["seq"] = self._remote_state.get("seq", 0) + 1
            cmd = frame.get("command")
            if cmd == "notifyPlayerStatusChanged":
                ps = frame.get("playerState")
                if isinstance(ps, str):
                    self._remote_state["player_state"] = ps
                pos = frame.get("progress", frame.get("position"))
                if isinstance(pos, (int, float)):
                    self._remote_state["position_ms"] = int(pos)
                duration = frame.get("duration")
                if isinstance(duration, (int, float)):
                    self._remote_state["duration_ms"] = int(duration)
            elif cmd == "notifyDeviceStatusChanged":
                vol = frame.get("volume")
                level = vol.get("level") if isinstance(vol, dict) else None
                if isinstance(level, (int, float)):
                    self._remote_state["volume"] = round(max(0, min(1, level)) * 100)
                mute = vol.get("mute") if isinstance(vol, dict) else None
                if isinstance(mute, bool):
                    self._remote_state["muted"] = mute
            elif cmd in ("notifyMediaChanged", "notifyMediaInfoChanged"):
                self._remote_state["media_info"] = frame.get("mediaInfo")
                error = self._remote_state.get("error") or {}
                if error.get("command") in ("notifyPlaybackError", "notifyContentServerError"):
                    self._remote_state["error"] = None
                meta = (frame.get("mediaInfo") or {}).get("metadata") or {}
                duration = meta.get("duration")
                if isinstance(duration, (int, float)):
                    self._remote_state["duration_ms"] = int(duration)
            elif cmd == "notifySessionStarted":
                self._remote_state["session_id"] = frame.get("sessionId")
                self._remote_state["error"] = None
            elif cmd in ("notifyPlaybackError", "notifyContentServerError",
                         "notifyQueueServerError", "notifySessionError"):
                # Never retain server descriptions: they can contain URLs,
                # bearer credentials or a raw authentication response.
                code = frame.get("errorCode")
                self._remote_state["error"] = {
                    "command": cmd,
                    "code": code if isinstance(code, (int, float)) else None,
                }
                self._remote_state["player_state"] = "ERROR"
                if cmd == "notifySessionError":
                    self._remote_state["session_id"] = None
            elif cmd in ("notifySessionEnded", "connectionLost"):
                self._remote_state["session_id"] = None
                self._remote_state["player_state"] = "IDLE"
                error = self._remote_state.get("error") or {}
                if cmd == "connectionLost" and error.get("command") != "notifySessionError":
                    self._remote_state["error"] = {"command": cmd, "code": None}

    def _toggle_silencer(self, active: bool) -> None:
        if self._local_silencer is None:
            return
        try:
            self._local_silencer(active)
        except Exception as exc:
            log.warning(
                "tidal_connect_real: silencer raised on active=%s: %s",
                active, exc,
            )

    def start(self) -> None:
        """Begin mDNS discovery in the background. Idempotent."""
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return
        ready = threading.Event()
        loop_holder: dict[str, Any] = {}

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_holder["loop"] = loop
            ready.set()
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run_loop, name="tidal-connect-real", daemon=True
        )
        self._loop_thread.start()
        ready.wait()
        self._loop = loop_holder["loop"]
        self._start_discovery()

    def stop(self) -> None:
        """Tear down discovery + any active connection. Idempotent."""
        if self._connection is not None and self._loop is not None:
            # Await the close round-trip before stopping the loop.
            # Without this, the loop can stop before the WSS close
            # frame is sent, leaving the device's session dangling.
            future = asyncio.run_coroutine_threadsafe(
                self._connection.close(), self._loop
            )
            try:
                future.result(timeout=5.0)
            except Exception as exc:
                log.warning(
                    "tidal_connect_real: stop close raised: %s", exc,
                )
            self._connection = None
        if self._browser is not None:
            try:
                self._browser.cancel()
            except Exception:
                pass
            self._browser = None
        if self._zeroconf is not None:
            try:
                self._zeroconf.close()
            except Exception:
                pass
            self._zeroconf = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None

    def list_devices(self) -> list[DiscoveredDevice]:
        with self._devices_lock:
            return list(self._devices.values())

    def connect(self, device_id: str) -> None:
        """Open a session to the given device. Drops any existing
        connection first."""
        if self._loop is None:
            raise RuntimeError("manager not started")
        with self._devices_lock:
            device = self._devices.get(device_id)
        if device is None:
            selected = self._connection
            if selected is not None and selected.device.id == device_id:
                # Keep a selected receiver reconnectable while mDNS is
                # temporarily absent; it may still be reachable by IP.
                device = selected.device
            else:
                raise KeyError(f"unknown device: {device_id}")

        future = asyncio.run_coroutine_threadsafe(
            self._run_command(lambda: self._connect_device(device)), self._loop
        )
        try:
            future.result(timeout=20.0)
        except TimeoutError:
            future.cancel()
            raise

    async def _connect_device(self, device: DiscoveredDevice) -> None:
        """Retain a previously selected receiver when its transport fails.

        Caller holds _command_lock on the manager loop.
        """
        previous = self._connection
        if previous is not None:
            await previous.close()

        # Notification wrapper: record into remote_state first, then
        # forward to the user-supplied callback. The wrapper is what
        # the connection sees, so internal state stays current even
        # if the user's callback is lazy / no-op.
        def _tap(frame: dict) -> None:
            self._record_notification(frame)
            try:
                self.on_notification(frame)
            except Exception as exc:
                log.warning(
                    "tidal_connect_real: on_notification raised: %s", exc,
                )

        conn = TidalConnectConnection(
            device=device,
            token_provider=self.token_provider,
            on_notification=_tap,
        )
        self._connection = conn
        with self._remote_state_lock:
            if self._remote_state["device_id"] != device.id:
                self._remote_state.update(
                    player_state="IDLE", position_ms=0, duration_ms=0,
                    media_info=None, volume=100, muted=False, error=None,
                )
            self._remote_state["device_id"] = device.id
            self._remote_state["session_id"] = None
            self._remote_state["seq"] += 1
        try:
            await conn.open()
        except BaseException:
            if previous is not None and previous.device.id == device.id:
                self._record_notification({"command": "connectionLost"})
            else:
                self._clear_selection()
            raise
        self._toggle_silencer(True)

    def _clear_selection(self) -> None:
        self._connection = None
        self._track_quality = None
        self._toggle_silencer(False)
        with self._remote_state_lock:
            self._remote_state.update(
                player_state="IDLE", position_ms=0, duration_ms=0,
                media_info=None, session_id=None, device_id=None, error=None,
                seq=self._remote_state["seq"] + 1,
            )

    async def _run_command(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        if self._disconnect_requests:
            raise ConnectionError("Tidal Connect is disconnecting")
        task = asyncio.current_task()
        self._command_tasks.add(task)
        try:
            async with self._command_lock:
                return await operation()
        finally:
            self._command_tasks.discard(task)

    def disconnect(self) -> None:
        if self._loop is None:
            self._clear_selection()
            return

        async def _do_disconnect() -> None:
            self._disconnect_requests += 1
            try:
                # Output switching must interrupt a stalled reconnect/control,
                # rather than wait behind it until the HTTP deadline expires.
                tasks = list(self._command_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                async with self._command_lock:
                    conn = self._connection
                    try:
                        if conn is not None:
                            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                                await conn.close()
                    except (TimeoutError, OSError) as exc:
                        log.warning("tidal_connect_real: disconnect close failed: %s", exc)
                    finally:
                        self._clear_selection()
            finally:
                self._disconnect_requests -= 1

        future = asyncio.run_coroutine_threadsafe(
            _do_disconnect(), self._loop
        )
        try:
            future.result(timeout=25.0)
        except TimeoutError:
            future.cancel()
            raise

    def get_connection(self) -> Optional[TidalConnectConnection]:
        """Returns the current connection so the player can call
        play / pause / etc. directly. None when no device is open."""
        return self._connection

    def is_active(self) -> bool:
        return self._connection is not None and self._connection.is_active()

    # -- sync command wrappers -----------------------------------------------
    # Each dispatches the connection's async method onto the manager's
    # event-loop thread. The outer deadline includes closing an old
    # session (4s + 2s transport close), opening a new one (10s + 4s
    # handshake), and the command's own 4s deadline.

    def _call(self, coro_factory: Callable[[], Any], timeout: float = 25.0) -> dict:
        if self._loop is None:
            raise RuntimeError("manager not started")

        async def _dispatch() -> dict:
            selected = self._connection
            if selected is None:
                raise RuntimeError("no Tidal Connect device selected")
            if not selected.is_active():
                with self._devices_lock:
                    device = self._devices.get(selected.device.id, selected.device)
                await self._connect_device(device)
            return await coro_factory()

        future = asyncio.run_coroutine_threadsafe(
            self._run_command(_dispatch), self._loop
        )
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def play(self) -> dict:
        return self._call(lambda: self._connection.play())  # type: ignore[union-attr]

    def resume(self, fallback_track_id: Optional[int] = None) -> dict:
        """Resume the receiver's track, restoring its queue after session loss."""
        selected = self._connection
        saved = self.remote_state()

        async def _resume() -> dict:
            remote = self.remote_state()
            if self._connection is not selected or remote.get("player_state") in ("IDLE", "ERROR"):
                media = saved.get("media_info") or {}
                try:
                    track_id = int(media.get("mediaId") or fallback_track_id or 0)
                except (TypeError, ValueError):
                    track_id = 0
                if track_id <= 0 or self._track_url_resolver is None:
                    raise RuntimeError("No current Tidal Connect track to resume")
                payload = await asyncio.to_thread(
                    self._track_url_resolver, track_id, self._track_quality
                )
                position = int(saved.get("position_ms") or 0)
                duration = int(saved.get("duration_ms") or 0)
                # The saved clock belongs to the receiver's media, not
                # an unrelated persisted fallback from local playback.
                if str(media.get("mediaId")) == str(track_id) and 0 < position < duration:
                    payload = {**payload, "position": position}
                await self._connection.load_cloud_queue(payload)
            return await self._connection.play()

        return self._call(_resume)

    def pause(self) -> dict:
        return self._call(lambda: self._connection.pause())  # type: ignore[union-attr]

    def stop(self) -> dict:
        return self._call(lambda: self._connection.stop())  # type: ignore[union-attr]

    def next_track(self) -> dict:
        return self._call(lambda: self._connection.next_track())  # type: ignore[union-attr]

    def previous_track(self) -> dict:
        return self._call(lambda: self._connection.previous_track())  # type: ignore[union-attr]

    def seek(self, position_ms: int) -> dict:
        return self._call(lambda: self._connection.seek(int(position_ms)))  # type: ignore[union-attr]

    def set_volume(self, level: int) -> dict:
        return self._call(lambda: self._connection.set_volume(int(level)))  # type: ignore[union-attr]

    def set_mute(self, mute: bool) -> dict:
        return self._call(lambda: self._connection.set_mute(bool(mute)))  # type: ignore[union-attr]

    def load_track(self, track_id: int, quality: Optional[str] = None) -> dict:
        """Create a cloud queue for `track_id` and load it on the receiver."""
        if self._track_url_resolver is None:
            raise RuntimeError(
                "no track URL resolver registered; "
                "call set_track_url_resolver at startup"
            )
        async def _load() -> dict:
            payload = await asyncio.to_thread(
                self._track_url_resolver, int(track_id), quality
            )
            result = await self._connection.load_cloud_queue(payload)
            # A reconnect must rebuild this queue at the requested quality.
            self._track_quality = quality
            return result

        return self._call(_load)

    def load_cloud_queue(self, payload: dict) -> dict:
        """Send a prepared queue payload through the live connection."""
        return self._call(
            lambda: self._connection.load_cloud_queue(payload)  # type: ignore[union-attr]
        )

    # -- discovery ----------------------------------------------------------

    def _start_discovery(self) -> None:
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:  # pragma: no cover
            log.warning(
                "tidal_connect_real: zeroconf not installed; discovery off"
            )
            return
        self._zeroconf = Zeroconf()
        self._browser = ServiceBrowser(
            self._zeroconf, TIDAL_CONNECT_SERVICE, handlers=[self._on_service]
        )

    def _on_service(
        self, zeroconf: Any, service_type: str, name: str, state_change: Any
    ) -> None:
        from zeroconf import ServiceStateChange  # type: ignore
        if state_change is ServiceStateChange.Added or state_change is ServiceStateChange.Updated:
            info = zeroconf.get_service_info(service_type, name)
            if info is None:
                return
            try:
                addr = socket.inet_ntoa(info.addresses[0])
            except Exception:
                return
            device = DiscoveredDevice(
                id=name,
                name=(info.properties.get(b"fn") or info.properties.get(b"name") or b"").decode(errors="replace") or name,
                address=addr,
                port=info.port or 0,
            )
            with self._devices_lock:
                self._devices[name] = device
            log.info(
                "tidal_connect_real: discovered %s @ %s:%d",
                device.name, device.address, device.port,
            )
        elif state_change is ServiceStateChange.Removed:
            with self._devices_lock:
                self._devices.pop(name, None)


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------

_manager: Optional[TidalConnectRealManager] = None


def get_manager() -> Optional[TidalConnectRealManager]:
    return _manager


def start_manager(
    token_provider: TokenProvider,
    on_notification: NotificationCallback,
) -> TidalConnectRealManager:
    """Construct (or reuse) the module-level manager and start
    discovery. Idempotent."""
    global _manager
    if _manager is None:
        _manager = TidalConnectRealManager(
            token_provider=token_provider,
            on_notification=on_notification,
        )
    _manager.start()
    return _manager


def stop_manager() -> None:
    if _manager is not None:
        _manager.stop()
