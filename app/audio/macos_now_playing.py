"""macOS Now Playing integration.

Without this module, pressing the play / pause / next / previous media
keys when Tideway isn't in the foreground opens Apple Music — even
though Tideway is the app actually playing audio. macOS routes media
keys to whichever app currently holds the "Now Playing" role via
MPNowPlayingInfoCenter and MPRemoteCommandCenter. By default Apple
Music claims that role, so unless we explicitly take it, the keys
hit Music.app instead of us.

This bridge:

  1. Registers Tideway as the active media player by setting
     `MPNowPlayingInfoCenter.defaultCenter().nowPlayingInfo` and
     `playbackState` whenever the local player transitions states.
     Once registered, macOS routes media-key events to our app's
     remote-command handlers instead of Apple Music's.

  2. Wires play / pause / toggle / next / previous remote commands
     to local HTTP endpoints (`/api/player/play`, `.../pause`,
     `.../hotkey/next`, etc.) — same pattern as `app/global_keys.py`'s
     pynput listener uses, so the audio engine doesn't have to know
     anything about Cocoa.

  3. Mirrors the player's track / state / position into
     `nowPlayingInfo` so the metadata shows up in Control Center,
     the menu-bar Now Playing widget, and the lock screen.

Cleanly degrades:
  - Non-macOS: `start()` is a no-op.
  - macOS but `MediaPlayer` framework not importable (older PyObjC,
    stripped pyinstaller bundle, etc.): logs a single warning and
    no-ops. The pynput global-key listener still picks up media keys
    when Tideway IS in the foreground; this bridge just adds
    background coverage on macOS.

The pynput listener and this bridge coexist: pynput catches the raw
key events at the OS level when Tideway is focused, this bridge
catches the routed Now Playing remote commands when it isn't. Both
end up firing the same HTTP endpoints.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Optional
from urllib import request as urlrequest

log = logging.getLogger(__name__)


def _say(msg: str) -> None:
    """Visible log line. Mirrors the convention in tidal_realtime —
    Python's `logging` isn't routed to a visible sink in this app, so
    high-signal lines that the user might need to see use bare print."""
    print(f"[macos-np] {msg}", flush=True)


class MacOSNowPlayingBridge:
    """Owns the MPNowPlayingInfoCenter + MPRemoteCommandCenter
    integration. Constructed lazily by server.py at startup; safe to
    instantiate on non-macOS systems (start() will return None).
    """

    def __init__(self, base_url: str = ""):
        self._base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._enabled = False
        self._info_center: Any = None
        self._command_center: Any = None
        # Cached metadata. The PlayerSnapshot has track_id but not
        # title / artist / album, so callers (server.py, frontend
        # via /api/now-playing) push richer metadata in here when
        # the track changes. Until the first push lands we render
        # whatever we know — usually nothing — and the menu-bar
        # widget just shows the app name.
        self._title = ""
        self._artist = ""
        self._album = ""
        self._duration_ms = 0
        self._artwork_url = ""
        # Latest state from the player. Cached so a metadata push
        # can re-render without waiting for the next state event.
        self._state = "idle"
        self._position_ms = 0
        # Strong refs to the Cocoa block handlers so they don't get
        # garbage-collected and silently stop firing remote commands.
        self._handler_refs: list[Any] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_base_url(self, base_url: str) -> None:
        """Set the local HTTP base URL after construction. Used by
        server.py's lifespan, which knows the port only after argparse
        has run, while the bridge is constructed at module-import
        time."""
        self._base_url = base_url.rstrip("/")

    def start(self) -> None:
        """Register the remote command handlers and seed the now-
        playing center. Idempotent and safe on non-macOS / stripped
        builds — those paths log once and no-op the rest of the API.
        """
        if sys.platform != "darwin":
            return
        with self._lock:
            if self._enabled:
                return
            try:
                import MediaPlayer  # type: ignore[import-not-found]
            except ImportError as exc:
                _say(f"MediaPlayer framework unavailable: {exc!r}")
                return
            try:
                self._info_center = (
                    MediaPlayer.MPNowPlayingInfoCenter.defaultCenter()
                )
                self._command_center = (
                    MediaPlayer.MPRemoteCommandCenter.sharedCommandCenter()
                )
                self._wire_command_handlers()
                self._enabled = True
            except Exception as exc:
                _say(f"MediaPlayer init failed: {exc!r}")
                return
        _say("registered with macOS Now Playing")
        self._push()

    def _wire_command_handlers(self) -> None:
        """Bind each MPRemoteCommand to a local handler that POSTs
        the corresponding action to our HTTP API. Using HTTP rather
        than a direct player reference so this module stays
        independent of the audio engine's threading model — same
        decoupling pattern `app/global_keys.py` uses for pynput.
        """
        # Imported here so non-macOS doesn't pay the import cost.
        import MediaPlayer  # type: ignore[import-not-found]

        # Handlers receive an MPRemoteCommandEvent and must return an
        # MPRemoteCommandHandlerStatusSuccess. The block API expects
        # an objc.callable that returns NSInteger; PyObjC bridges the
        # Python callable shape automatically.
        success = MediaPlayer.MPRemoteCommandHandlerStatusSuccess

        def _on(action: str):
            def handler(_event: Any) -> int:
                threading.Thread(
                    target=_safe_post,
                    args=(self._base_url, action),
                    daemon=True,
                ).start()
                return success
            return handler

        bindings = [
            (self._command_center.playCommand(), _on("/api/player/play")),
            (self._command_center.pauseCommand(), _on("/api/player/pause")),
            (
                self._command_center.togglePlayPauseCommand(),
                _on("/api/hotkey/play_pause"),
            ),
            (self._command_center.nextTrackCommand(), _on("/api/hotkey/next")),
            (
                self._command_center.previousTrackCommand(),
                _on("/api/hotkey/previous"),
            ),
        ]
        for cmd, handler in bindings:
            cmd.setEnabled_(True)
            ref = cmd.addTargetWithHandler_(handler)
            # Pin the returned target ref so ARC doesn't drop it.
            self._handler_refs.append(ref)
            self._handler_refs.append(handler)

    # ------------------------------------------------------------------
    # State + metadata updates
    # ------------------------------------------------------------------

    def update_state(self, snap: Any) -> None:
        """Called from a PCMPlayer subscribe() listener on every state
        change. Mirrors state + position into MPNowPlayingInfoCenter
        so the menu-bar Now Playing widget stays in sync.

        The PlayerSnapshot doesn't carry track title / artist
        (those live on the frontend Track model), so we just update
        state + position here. Track metadata flows in via
        update_metadata(), called from the /api/now-playing endpoint
        the frontend hits on track change.
        """
        with self._lock:
            if not self._enabled:
                return
            self._state = getattr(snap, "state", "idle")
            self._position_ms = int(getattr(snap, "position_ms", 0) or 0)
            duration = int(getattr(snap, "duration_ms", 0) or 0)
            if duration > 0:
                self._duration_ms = duration
        self._push()

    def update_metadata(
        self,
        *,
        title: str,
        artist: str,
        album: str = "",
        duration_ms: int = 0,
        artwork_url: str = "",
    ) -> None:
        """Push richer track metadata. Called by the
        /api/now-playing endpoint, which the frontend hits whenever
        the playing track changes (or on initial load when the
        backend already has something playing)."""
        with self._lock:
            if not self._enabled:
                return
            self._title = title or ""
            self._artist = artist or ""
            self._album = album or ""
            if duration_ms > 0:
                self._duration_ms = int(duration_ms)
            self._artwork_url = artwork_url or ""
        self._push()

    def clear(self) -> None:
        """Drop the now-playing entry. Called when playback ends or
        the user explicitly stops. Without this, the menu-bar widget
        keeps showing the last track forever."""
        with self._lock:
            if not self._enabled:
                return
            self._title = ""
            self._artist = ""
            self._album = ""
            self._duration_ms = 0
            self._position_ms = 0
            self._state = "idle"
            self._artwork_url = ""
            try:
                import MediaPlayer  # type: ignore[import-not-found]

                self._info_center.setNowPlayingInfo_(None)
                self._info_center.setPlaybackState_(
                    MediaPlayer.MPNowPlayingPlaybackStateStopped
                )
            except Exception as exc:
                _say(f"clear failed: {exc!r}")

    # ------------------------------------------------------------------
    # Push to MPNowPlayingInfoCenter
    # ------------------------------------------------------------------

    def _push(self) -> None:
        """Snapshot the cached state + metadata and send it to
        MPNowPlayingInfoCenter. Called after every state or metadata
        update. Cheap — one Cocoa call, no network — so we don't
        bother coalescing."""
        with self._lock:
            if not self._enabled:
                return
            title = self._title or "Tideway"
            artist = self._artist
            album = self._album
            duration_s = self._duration_ms / 1000.0
            position_s = self._position_ms / 1000.0
            state = self._state
        try:
            import MediaPlayer  # type: ignore[import-not-found]

            info: dict[str, Any] = {
                MediaPlayer.MPMediaItemPropertyTitle: title,
                MediaPlayer.MPMediaItemPropertyArtist: artist,
                MediaPlayer.MPMediaItemPropertyAlbumTitle: album,
                MediaPlayer.MPMediaItemPropertyPlaybackDuration: duration_s,
                MediaPlayer.MPNowPlayingInfoPropertyElapsedPlaybackTime: (
                    position_s
                ),
                # Playback rate is 1.0 when playing, 0.0 when paused.
                # macOS uses this to decide whether the Control Center
                # widget animates the progress bar.
                MediaPlayer.MPNowPlayingInfoPropertyPlaybackRate: (
                    1.0 if state == "playing" else 0.0
                ),
            }
            self._info_center.setNowPlayingInfo_(info)
            if state == "playing":
                self._info_center.setPlaybackState_(
                    MediaPlayer.MPNowPlayingPlaybackStatePlaying
                )
            elif state == "paused":
                self._info_center.setPlaybackState_(
                    MediaPlayer.MPNowPlayingPlaybackStatePaused
                )
            else:
                self._info_center.setPlaybackState_(
                    MediaPlayer.MPNowPlayingPlaybackStateStopped
                )
        except Exception as exc:
            _say(f"push failed: {exc!r}")


def _safe_post(base_url: str, path: str) -> None:
    url = f"{base_url}{path}"
    try:
        req = urlrequest.Request(url, method="POST")
        urlrequest.urlopen(req, timeout=2).close()  # noqa: S310
    except Exception as exc:
        log.debug("macos-np POST %s failed: %s", url, exc)
