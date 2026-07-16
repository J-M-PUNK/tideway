"""MPRIS integration (Linux).

Without this module, Tideway is invisible to the Linux desktop's
media layer: GNOME and KDE media widgets, the lock screen, desklets,
hardware media keys routed through the desktop, and `playerctl` all
talk MPRIS (`org.mpris.MediaPlayer2.*` on the session bus), and no
one is answering for us.

This bridge is the Linux counterpart of the macOS Now Playing bridge
in `app/audio/macos_now_playing.py`, structurally the same, different
transport:

  1. Owns `org.mpris.MediaPlayer2.tideway` on the session bus and
     exports the two standard interfaces at /org/mpris/MediaPlayer2.

  2. Routes Play / Pause / PlayPause / Next / Previous / Stop / Seek
     and volume writes to the same local HTTP endpoints the macOS
     remote commands and the pynput hotkey listener use, so the audio
     engine doesn't have to know anything about D-Bus.

  3. Mirrors the player's state, position, volume, and the display
     metadata (pushed by the frontend via /api/now-playing) into the
     Player interface's properties, emitting PropertiesChanged so
     desktop widgets update live and Seeked when playback position
     jumps discontinuously.

Cleanly degrades:
  - Non-Linux: `start()` is a no-op.
  - Linux without dbus-next or without a session bus (headless SSH,
    stripped build): logs one line and no-ops. Playback is unaffected.

The service runs a dedicated thread with its own asyncio loop, since
dbus-next is asyncio-native and the player callbacks arrive on
arbitrary threads. Cross-thread property updates hop onto the loop
via call_soon_threadsafe.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import Any, Optional
from urllib import request as urlrequest

log = logging.getLogger(__name__)

BUS_NAME = "org.mpris.MediaPlayer2.tideway"
OBJECT_PATH = "/org/mpris/MediaPlayer2"

# mpris:trackid for "nothing loaded". The spec reserves this exact
# path as the sentinel; clients like playerctl special-case it.
_NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

# A position delta bigger than this between the extrapolated and the
# reported position is a seek, not clock drift. Snapshots arrive on
# state transitions rather than a fixed tick, so the tolerance is
# generous.
_SEEK_JUMP_MS = 1500


def _say(msg: str) -> None:
    """Visible log line — same convention as the macOS bridge:
    Python's `logging` isn't routed to a visible sink in this app, so
    high-signal lines use bare print."""
    print(f"[mpris] {msg}", flush=True)


def playback_status(state: str) -> str:
    """Map the PCMPlayer state string onto the MPRIS enum. Everything
    that isn't actively playing or paused (idle, error, loading) is
    Stopped as far as the desktop is concerned."""
    return {"playing": "Playing", "paused": "Paused"}.get(state, "Stopped")


def track_object_path(track_id: Any) -> str:
    """Render a track id as a valid D-Bus object path. Tidal ids are
    numeric, but local files could feed anything through here, so
    every non-alphanumeric byte is folded to `_`."""
    if track_id is None:
        return _NO_TRACK
    safe = "".join(c if c.isalnum() else "_" for c in str(track_id))
    return f"/org/tideway/track/{safe or '_'}"


def build_metadata(
    track_id: Any,
    title: str,
    artist: str,
    album: str,
    duration_ms: int,
    artwork_url: str,
) -> dict[str, Any]:
    """Assemble the xesam/mpris metadata dict with plain Python
    values. Empty fields are omitted rather than sent as empty
    strings — clients render "" literally. The caller wraps values in
    D-Bus Variants; keeping this pure makes it testable without a
    bus."""
    md: dict[str, Any] = {"mpris:trackid": track_object_path(track_id)}
    if duration_ms > 0:
        # MPRIS lengths and positions are microseconds.
        md["mpris:length"] = int(duration_ms) * 1000
    if title:
        md["xesam:title"] = title
    if artist:
        md["xesam:artist"] = [artist]
    if album:
        md["xesam:album"] = album
    if artwork_url:
        md["mpris:artUrl"] = artwork_url
    return md


class MprisBridge:
    """Owns the session-bus service. Constructed at module import by
    server.py (like the macOS bridge); safe to instantiate anywhere —
    start() is where the platform check happens."""

    def __init__(self, base_url: str = ""):
        self._base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._enabled = False
        self._loop: Any = None
        self._bus: Any = None
        self._player_iface: Any = None
        self._thread: Optional[threading.Thread] = None
        # Display metadata, pushed by /api/now-playing on track
        # change. The PlayerSnapshot carries track_id but not
        # title/artist/album — same split as the macOS bridge.
        self._title = ""
        self._artist = ""
        self._album = ""
        self._meta_duration_ms = 0
        self._artwork_url = ""
        # Latest snapshot state. position is cached with the
        # monotonic instant it was true, so the Position property can
        # extrapolate between snapshots instead of serving stale
        # values to `playerctl position` pollers.
        self._state = "idle"
        self._track_id: Any = None
        self._duration_ms = 0
        self._position_ms = 0
        self._position_at = time.monotonic()
        self._volume = 100
        self._muted = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_base_url(self, base_url: str) -> None:
        """Set the local HTTP base URL after construction — server.py
        knows the port only at lifespan time."""
        self._base_url = base_url.rstrip("/")

    def start(self) -> None:
        """Spin up the D-Bus service thread. Idempotent; no-ops off
        Linux or when dbus-next isn't importable."""
        if not sys.platform.startswith("linux"):
            return
        with self._lock:
            if self._enabled:
                return
            try:
                import dbus_next  # noqa: F401
            except ImportError as exc:
                _say(f"dbus-next unavailable, MPRIS disabled: {exc!r}")
                return
            self._thread = threading.Thread(
                target=self._run, name="mpris", daemon=True
            )
            self._thread.start()
            self._enabled = True

    def stop(self) -> None:
        """Disconnect from the bus and stop the loop thread. Called
        from desktop.py's shutdown path; safe when never started."""
        with self._lock:
            if not self._enabled:
                return
            self._enabled = False
            loop, bus = self._loop, self._bus
            self._loop = None
            self._bus = None
            self._player_iface = None
        if loop is not None:
            def _shutdown() -> None:
                if bus is not None:
                    try:
                        bus.disconnect()
                    except Exception:
                        pass
                loop.stop()

            try:
                loop.call_soon_threadsafe(_shutdown)
            except RuntimeError:
                # Loop already closed on its own (bus error path).
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
            self._loop = loop
            loop.run_forever()
        except Exception as exc:
            # No session bus (headless), name taken, bus died — one
            # line, then the app carries on without MPRIS.
            _say(f"service unavailable: {exc!r}")
            with self._lock:
                self._enabled = False
                self._loop = None
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _serve(self) -> None:
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType

        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        root, player = _build_interfaces(self)
        bus.export(OBJECT_PATH, root)
        bus.export(OBJECT_PATH, player)
        await bus.request_name(BUS_NAME)
        self._bus = bus
        self._player_iface = player
        _say(f"registered on session bus as {BUS_NAME}")

    # ------------------------------------------------------------------
    # State + metadata updates (called from player / HTTP threads)
    # ------------------------------------------------------------------

    def update_state(self, snap: Any) -> None:
        """PCMPlayer subscribe() listener. Mirrors state, position,
        and volume into the Player properties and detects seeks."""
        now = time.monotonic()
        with self._lock:
            expected_ms = self._extrapolated_position_ms(now)
            new_pos = int(getattr(snap, "position_ms", 0) or 0)
            was_track = self._track_id
            seeked = (
                self._state in ("playing", "paused")
                and getattr(snap, "state", "") in ("playing", "paused")
                and was_track == getattr(snap, "track_id", None)
                and abs(new_pos - expected_ms) > _SEEK_JUMP_MS
            )
            self._state = getattr(snap, "state", "idle") or "idle"
            self._track_id = getattr(snap, "track_id", None)
            self._duration_ms = int(getattr(snap, "duration_ms", 0) or 0)
            self._position_ms = new_pos
            self._position_at = now
            self._volume = int(getattr(snap, "volume", self._volume))
            self._muted = bool(getattr(snap, "muted", False))
            changed = {
                "PlaybackStatus": playback_status(self._state),
                "Metadata": self._metadata_variants(),
                "Volume": self._volume_double(),
            }
        self._emit(changed, seeked=seeked)

    def update_metadata(
        self,
        *,
        title: str = "",
        artist: str = "",
        album: str = "",
        duration_ms: int = 0,
        artwork_url: str = "",
    ) -> None:
        """Push display metadata — called from /api/now-playing on
        every track change, alongside the macOS and Cast mirrors."""
        with self._lock:
            self._title = title or ""
            self._artist = artist or ""
            self._album = album or ""
            self._meta_duration_ms = int(duration_ms or 0)
            self._artwork_url = artwork_url or ""
            changed = {"Metadata": self._metadata_variants()}
        self._emit(changed, seeked=False)

    # ------------------------------------------------------------------
    # Property material (bridge-side so interfaces stay thin)
    # ------------------------------------------------------------------

    def _extrapolated_position_ms(self, now: Optional[float] = None) -> int:
        """Position right now, advanced from the last snapshot when
        playing. Snapshots only arrive on state transitions, so
        without this every `playerctl position` poll would read the
        position frozen at the last event."""
        if now is None:
            now = time.monotonic()
        pos = self._position_ms
        if self._state == "playing":
            pos += int((now - self._position_at) * 1000)
        if self._duration_ms > 0:
            pos = min(pos, self._duration_ms)
        return max(0, pos)

    def current_position_us(self) -> int:
        with self._lock:
            return self._extrapolated_position_ms() * 1000

    def volume_double(self) -> float:
        with self._lock:
            return self._volume_double()

    def _volume_double(self) -> float:
        return 0.0 if self._muted else max(0.0, min(1.0, self._volume / 100.0))

    def metadata_variants(self) -> dict:
        with self._lock:
            return self._metadata_variants()

    def _metadata_variants(self) -> dict:
        try:
            from dbus_next import Variant
        except ImportError:
            # Off Linux (or stripped build) there is no bus to emit
            # to; update_state still runs unconditionally from the
            # player's listener list, so degrade instead of raising
            # into the player's emit loop.
            return {}

        md = build_metadata(
            self._track_id,
            self._title,
            self._artist,
            self._album,
            self._meta_duration_ms or self._duration_ms,
            self._artwork_url,
        )
        out: dict[str, Any] = {}
        for key, value in md.items():
            if key == "mpris:trackid":
                out[key] = Variant("o", value)
            elif key == "mpris:length":
                out[key] = Variant("x", value)
            elif key == "xesam:artist":
                out[key] = Variant("as", value)
            else:
                out[key] = Variant("s", value)
        return out

    def playback_status_str(self) -> str:
        with self._lock:
            return playback_status(self._state)

    # ------------------------------------------------------------------
    # Command routing (bus thread -> local HTTP, same as macOS bridge)
    # ------------------------------------------------------------------

    def post(self, path: str, body: Optional[dict] = None) -> None:
        """Fire-and-forget POST to the local API on a worker thread —
        D-Bus method handlers must not block on HTTP."""
        threading.Thread(
            target=_safe_post,
            args=(self._base_url, path, body),
            daemon=True,
        ).start()

    def seek_relative_us(self, offset_us: int) -> None:
        with self._lock:
            duration_ms = self._duration_ms
            target_ms = self._extrapolated_position_ms() + offset_us // 1000
        self._post_seek_ms(target_ms, duration_ms)

    def seek_absolute_us(self, position_us: int) -> None:
        with self._lock:
            duration_ms = self._duration_ms
        self._post_seek_ms(position_us // 1000, duration_ms)

    def _post_seek_ms(self, target_ms: int, duration_ms: int) -> None:
        if duration_ms <= 0:
            return
        fraction = max(0.0, min(1.0, target_ms / duration_ms))
        self.post("/api/player/seek", {"fraction": fraction})

    def set_volume_double(self, value: float) -> None:
        volume = int(round(max(0.0, min(1.0, float(value))) * 100))
        self.post("/api/player/volume", {"volume": volume})

    # ------------------------------------------------------------------
    # Cross-thread emission
    # ------------------------------------------------------------------

    def _emit(self, changed: dict, *, seeked: bool) -> None:
        loop = self._loop
        iface = self._player_iface
        if loop is None or iface is None:
            return

        def _do() -> None:
            try:
                iface.emit_properties_changed(changed)
                if seeked:
                    iface.Seeked(self.current_position_us())
            except Exception as exc:
                log.debug("mpris emit failed: %r", exc)

        try:
            loop.call_soon_threadsafe(_do)
        except RuntimeError:
            # Loop shut down between the check and the call.
            pass


def _build_interfaces(bridge: MprisBridge) -> tuple:
    """Construct the two MPRIS interfaces bound to `bridge`. Kept in a
    factory so dbus_next only imports on the Linux start() path."""
    from dbus_next.service import (
        ServiceInterface,
        dbus_property,
        method,
        signal,
    )
    from dbus_next.constants import PropertyAccess

    class _Root(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.mpris.MediaPlayer2")

        @method()
        def Raise(self):
            bridge.post("/api/_internal/focus")

        @method()
        def Quit(self):
            # CanQuit is False; some clients call it anyway. Ignore —
            # quitting the whole app from a media applet is hostile.
            return None

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":  # noqa: F821
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":  # noqa: F821
            return True

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":  # noqa: F821
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":  # noqa: F821
            return "Tideway"

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":  # noqa: F821
            # Matches the .desktop file the Flatpak installs; harmless
            # when running from source without one.
            return "com.tidaldownloader.Tideway"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":  # noqa: F821
            return []

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":  # noqa: F821
            return []

    class _Player(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.mpris.MediaPlayer2.Player")

        @method()
        def Next(self):
            bridge.post("/api/hotkey/next")

        @method()
        def Previous(self):
            bridge.post("/api/hotkey/previous")

        @method()
        def Pause(self):
            bridge.post("/api/player/pause")

        @method()
        def PlayPause(self):
            bridge.post("/api/hotkey/play_pause")

        @method()
        def Stop(self):
            bridge.post("/api/player/stop")

        @method()
        def Play(self):
            bridge.post("/api/player/play")

        @method()
        def Seek(self, offset: "x"):  # noqa: F821
            bridge.seek_relative_us(offset)

        @method()
        def SetPosition(self, track_id: "o", position: "x"):  # noqa: F821
            bridge.seek_absolute_us(position)

        @method()
        def OpenUri(self, uri: "s"):  # noqa: F821
            # No URI ingestion — Tideway plays from its own catalog.
            return None

        @signal()
        def Seeked(self, position: "x") -> "x":  # noqa: F821
            return position

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":  # noqa: F821
            return bridge.playback_status_str()

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":  # noqa: F821
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":  # noqa: F821
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":  # noqa: F821
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":  # noqa: F821
            return bridge.metadata_variants()

        @dbus_property(access=PropertyAccess.READWRITE)
        def Volume(self) -> "d":  # noqa: F821
            return bridge.volume_double()

        @Volume.setter
        def Volume(self, value: "d"):  # noqa: F821
            bridge.set_volume_double(value)

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":  # noqa: F821
            return bridge.current_position_us()

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":  # noqa: F821
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":  # noqa: F821
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":  # noqa: F821
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":  # noqa: F821
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":  # noqa: F821
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":  # noqa: F821
            return True

    return _Root(), _Player()


def _safe_post(base_url: str, path: str, body: Optional[dict] = None) -> None:
    """POST to the local API, swallowing failures — a dead endpoint
    during startup/shutdown shouldn't take the bus thread with it."""
    if not base_url:
        return
    url = f"{base_url}{path}"
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urlrequest.Request(url, data=data, method="POST")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urlrequest.urlopen(req, timeout=5.0):
            pass
    except Exception as exc:
        log.debug("mpris post %s failed: %r", path, exc)
