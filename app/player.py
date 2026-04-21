"""Native audio engine backed by libvlc.

The frontend HTML `<audio>` element can't decode Dolby Atmos (E-AC-3 JOC),
MQA, or Sony 360 Reality Audio — so users with HiFi Plus / Max tier
subscriptions silently get stereo downmixes through the old path.

This module owns a single VLCPlayer singleton that:

- Accepts a track (by Tidal id + quality preference) and resolves a
  DASH MPD manifest through tidalapi, writes it to a temp file, and
  loads it into libvlc.
- Exposes play / pause / resume / stop / seek / volume via thin methods.
- Emits state-change and position events that the HTTP layer streams
  over SSE to the frontend.

Queue / shuffle / repeat / sleep-timer all stay on the frontend — the
backend only plays the single track it was told to. When a track ends
naturally, we fire `track_ended` and the frontend picks the next one.
For gapless, the frontend pre-loads the next track's manifest ahead of
time; VLC's media swap on track-end is effectively instant (low-ms)
because the next media has already been resolved.
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import tidalapi

log = logging.getLogger(__name__)

# Lazy import so the rest of the app can import this module even on
# machines where libvlc isn't installed yet (e.g. dev containers).
# `is_available()` tells callers whether to actually use the engine.
try:
    import vlc  # type: ignore

    _VLC_IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover - env-dependent
    vlc = None  # type: ignore
    _VLC_IMPORT_ERROR = str(exc)


def is_available() -> bool:
    return vlc is not None


# ---------------------------------------------------------------------------
# State types
# ---------------------------------------------------------------------------


@dataclass
class PlayerSnapshot:
    """Plain-JSON view of the player, sent to the frontend over SSE."""
    state: str  # "idle" | "loading" | "playing" | "paused" | "ended" | "error"
    track_id: Optional[str]
    position_ms: int
    duration_ms: int
    volume: int  # 0..100
    muted: bool
    error: Optional[str] = None
    # Echoed back for the frontend to reconcile async commands with the
    # version of state they apply to. Bumped on every state-changing
    # method call.
    seq: int = 0


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class VLCPlayer:
    """Thread-safe libvlc wrapper.

    Thread safety: libvlc callbacks fire on its own threads. All state
    mutations go through `_lock`. Public methods are HTTP-handler-safe.
    Listeners (`subscribe`) are called on the mutation thread — the SSE
    layer should queue snapshots rather than do anything heavy in-line.
    """

    def __init__(
        self,
        session_getter: Callable[[], tidalapi.Session],
        local_lookup: Optional[Callable[[str], Optional[str]]] = None,
        quality_clamp: Optional[Callable[[str], Optional[str]]] = None,
    ):
        if vlc is None:
            raise RuntimeError(
                f"libvlc not available: {_VLC_IMPORT_ERROR}"
            )
        self._session_getter = session_getter
        # Optional: returns a filesystem path for a track id if the user
        # already has it downloaded. We skip the Tidal manifest fetch
        # entirely in that case — faster, bandwidth-free, and works in
        # offline mode without a live session.
        self._local_lookup = local_lookup
        # Optional: given a requested quality ("hi_res_lossless" etc),
        # returns the highest tier the user's subscription allows.
        # Saves us from 401s when the user's saved preference exceeds
        # their tier (possible after a subscription downgrade).
        self._quality_clamp = quality_clamp
        # --no-video: we don't have a window. --quiet: libvlc's stderr
        # is chatty. --intf dummy: prevent it opening its own UI.
        self._instance = vlc.Instance("--no-video", "--quiet", "--intf", "dummy")
        self._player = self._instance.media_player_new()
        self._lock = threading.RLock()
        self._listeners: list[Callable[[PlayerSnapshot], None]] = []
        self._seq = 0

        self._current_track_id: Optional[str] = None
        self._current_duration_ms: int = 0
        self._current_mpd_path: Optional[str] = None
        self._state: str = "idle"
        self._last_error: Optional[str] = None

        # Attach event manager. VLC fires these on its worker threads.
        em = self._player.event_manager()
        em.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_playing)
        em.event_attach(vlc.EventType.MediaPlayerPaused, self._on_paused)
        em.event_attach(vlc.EventType.MediaPlayerStopped, self._on_stopped)
        em.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_ended)
        em.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_error)
        em.event_attach(vlc.EventType.MediaPlayerTimeChanged, self._on_time)

    # --- public API --------------------------------------------------------

    def subscribe(self, listener: Callable[[PlayerSnapshot], None]) -> Callable[[], None]:
        """Register a snapshot listener. Returns an unsubscribe fn."""
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return _unsub

    def load(self, track_id: str, quality: Optional[str] = None) -> PlayerSnapshot:
        """Resolve a stream for `track_id` and load it into the player.

        Does NOT start playback; the caller must call `play()` after.
        Splitting load and play lets the frontend pre-resolve the next
        track during the tail of the current one (gapless).
        """
        with self._lock:
            self._transition("loading", track_id=track_id)
        try:
            mpd_path, duration_s = self._resolve_stream(track_id, quality)
        except Exception as exc:
            log.exception("stream resolution failed for %s", track_id)
            with self._lock:
                self._last_error = str(exc)
                self._transition("error")
            return self.snapshot()
        with self._lock:
            # Clean up the previous temp file now that a new one is ready.
            old = self._current_mpd_path
            self._current_track_id = track_id
            self._current_duration_ms = int(duration_s * 1000) if duration_s else 0
            self._current_mpd_path = mpd_path
            self._last_error = None
            media = self._instance.media_new(mpd_path)
            self._player.set_media(media)
            # VLC keeps its own ref; free ours.
            media.release()
            self._bump_seq()
        _safe_unlink(old)
        return self.snapshot()

    def play(self) -> PlayerSnapshot:
        with self._lock:
            self._player.play()
            self._bump_seq()
        return self.snapshot()

    def pause(self) -> PlayerSnapshot:
        with self._lock:
            self._player.set_pause(1)
            self._bump_seq()
        return self.snapshot()

    def resume(self) -> PlayerSnapshot:
        with self._lock:
            self._player.set_pause(0)
            self._bump_seq()
        return self.snapshot()

    def stop(self) -> PlayerSnapshot:
        """Pause + clear current-track state.

        We intentionally do NOT call libvlc's `media_player_stop()`.
        On macOS (libvlc 3.x) stop() blocks for multiple seconds on
        network streams while the demuxer unwinds, and during that
        window any other call into the player (even `get_time()`)
        deadlocks. Pausing instead releases the audio device
        immediately and gets us the user-visible behavior we want —
        silence + "nothing playing." When a new track loads next,
        `set_media()` replaces the old media cleanly.
        """
        with self._lock:
            try:
                self._player.set_pause(1)
            except Exception:
                pass
            vol = 0
            muted = False
            try:
                vol = int(self._player.audio_get_volume() or 0)
                muted = bool(self._player.audio_get_mute())
            except Exception:
                pass
            self._transition("idle")
            self._current_track_id = None
            old = self._current_mpd_path
            self._current_mpd_path = None
            self._current_duration_ms = 0
            self._bump_seq()
            snap = PlayerSnapshot(
                state="idle",
                track_id=None,
                position_ms=0,
                duration_ms=0,
                volume=vol,
                muted=muted,
                error=None,
                seq=self._seq,
            )
        # Temp-file cleanup happens outside the lock — unlink is cheap
        # and VLC may still hold the file briefly, which is fine on
        # macOS (unlinked-but-open files stay alive until close).
        _safe_unlink(old)
        return snap

    def seek(self, fraction: float) -> PlayerSnapshot:
        """Seek to `fraction` (0..1) of the track."""
        fraction = max(0.0, min(1.0, float(fraction)))
        with self._lock:
            self._player.set_position(fraction)
            self._bump_seq()
        return self.snapshot()

    def set_volume(self, volume: int) -> PlayerSnapshot:
        volume = max(0, min(100, int(volume)))
        with self._lock:
            self._player.audio_set_volume(volume)
            self._bump_seq()
        return self.snapshot()

    def set_muted(self, muted: bool) -> PlayerSnapshot:
        with self._lock:
            self._player.audio_set_mute(1 if muted else 0)
            self._bump_seq()
        return self.snapshot()

    # -- equalizer ----------------------------------------------------------

    @staticmethod
    def eq_presets() -> list[dict]:
        """Static list of libvlc's built-in presets. Not per-session so
        we expose it as a class-level helper. Each entry carries the
        index (used by libvlc) + the human-readable name."""
        if vlc is None:
            return []
        out: list[dict] = []
        count = vlc.libvlc_audio_equalizer_get_preset_count()
        for i in range(count):
            name = vlc.libvlc_audio_equalizer_get_preset_name(i)
            out.append({
                "index": i,
                "name": name.decode() if name else f"Preset {i}",
            })
        return out

    @staticmethod
    def eq_bands_count() -> int:
        if vlc is None:
            return 0
        return int(vlc.libvlc_audio_equalizer_get_band_count())

    @staticmethod
    def eq_band_frequencies() -> list[float]:
        """Center frequency (Hz) for each band — for slider labels."""
        if vlc is None:
            return []
        count = VLCPlayer.eq_bands_count()
        return [
            float(vlc.libvlc_audio_equalizer_get_band_frequency(i))
            for i in range(count)
        ]

    def apply_equalizer(
        self, bands: list[float], preamp: Optional[float] = None
    ) -> None:
        """Apply a manual EQ. `bands` must be length `eq_bands_count()`;
        values are amplitudes in dB, clamped to [-20, 20]. Empty list
        disables the EQ entirely."""
        with self._lock:
            if not bands:
                # Empty list = disable EQ entirely. libvlc's API says
                # passing a null AudioEqualizer disables filtering.
                self._player.set_equalizer(None)
                return
            eq = vlc.AudioEqualizer()
            if preamp is not None:
                eq.set_preamp(max(-20.0, min(20.0, float(preamp))))
            count = vlc.libvlc_audio_equalizer_get_band_count()
            for i in range(min(count, len(bands))):
                v = max(-20.0, min(20.0, float(bands[i])))
                eq.set_amp_at_index(v, i)
            self._player.set_equalizer(eq)

    def apply_equalizer_preset(self, preset_index: int) -> list[float]:
        """Apply one of libvlc's built-in presets. Returns the band
        amplitudes that ended up active so the frontend's sliders can
        render the preset curve.

        python-vlc's class-level helpers (`AudioEqualizer.new_from_preset`)
        don't exist in every version; use the module-level C bindings
        which are stable across libvlc 3.x."""
        with self._lock:
            eq = vlc.libvlc_audio_equalizer_new_from_preset(int(preset_index))
            self._player.set_equalizer(eq)
            count = vlc.libvlc_audio_equalizer_get_band_count()
            bands = [
                float(vlc.libvlc_audio_equalizer_get_amp_at_index(eq, i))
                for i in range(count)
            ]
            # libvlc's equalizer object belongs to us until we release
            # it. MediaPlayer.set_equalizer internally retains a copy,
            # so dropping our handle here is safe.
            vlc.libvlc_audio_equalizer_release(eq)
            return bands

    # -- output device ------------------------------------------------------

    def list_output_devices(self) -> list[dict]:
        """Enumerate libvlc's audio output devices (USB DACs,
        Bluetooth, built-in speakers, etc.). Returns
        [{"id": "<opaque>", "name": "<human>"}...]. An empty id means
        "system default" — always the first entry."""
        with self._lock:
            head = self._player.audio_output_device_enum()
            out: list[dict] = [{"id": "", "name": "System default"}]
            node = head
            seen: set[str] = set()
            try:
                while node:
                    ref = node.contents
                    did_raw = ref.device
                    name_raw = ref.description
                    did = did_raw.decode() if did_raw else ""
                    name = name_raw.decode() if name_raw else ""
                    # libvlc's "0" / empty id is the system default —
                    # we always include it as the first entry; skip
                    # here to avoid duplicating it.
                    if did and did != "0" and did not in seen:
                        seen.add(did)
                        out.append({"id": did, "name": name or did})
                    node = ref.next
            finally:
                if head:
                    vlc.libvlc_audio_output_device_list_release(head)
            return out

    def set_output_device(self, device_id: str) -> None:
        """Switch output device. Empty string routes to the system
        default. libvlc handles the handoff while playback continues
        (a small gap is expected)."""
        with self._lock:
            # Passing None for module + device_id uses the currently-
            # active module. On macOS this is "auhal"; on Linux it'd
            # be "pulse" / "alsa". Passing None lets libvlc keep
            # whatever it's already using.
            self._player.audio_output_device_set(None, device_id or None)

    def snapshot(self) -> PlayerSnapshot:
        with self._lock:
            # In idle state there's no current media; asking libvlc for
            # position / duration returns -1 (or sometimes faults) —
            # return zeros instead of poking the player.
            if self._state == "idle":
                pos_ms = 0
            else:
                try:
                    t = self._player.get_time()
                    pos_ms = int(t) if t is not None and t >= 0 else 0
                except Exception:
                    pos_ms = 0
            try:
                vol = int(self._player.audio_get_volume() or 0)
            except Exception:
                vol = 0
            try:
                muted = bool(self._player.audio_get_mute())
            except Exception:
                muted = False
            return PlayerSnapshot(
                state=self._state,
                track_id=self._current_track_id,
                position_ms=pos_ms,
                duration_ms=self._current_duration_ms,
                volume=vol,
                muted=muted,
                error=self._last_error,
                seq=self._seq,
            )

    # --- VLC callbacks (fire on VLC worker threads) ------------------------

    def _on_playing(self, _event: object) -> None:
        with self._lock:
            # "idle" means our stop() already tore things down — ignore
            # late Playing echoes from the previous media. Every other
            # state (including "loading") is a legitimate predecessor
            # to "playing".
            if self._state == "idle":
                return
            self._transition("playing")
        self._emit()

    def _on_paused(self, _event: object) -> None:
        with self._lock:
            if self._state == "idle":
                return
            self._transition("paused")
        self._emit()

    def _on_stopped(self, _event: object) -> None:
        with self._lock:
            # Only our own stop() moves us to idle; VLC's own stopped
            # echoes are noise once we've already acted.
            if self._state == "idle":
                return
            self._transition("idle")
        self._emit()

    def _on_ended(self, _event: object) -> None:
        with self._lock:
            if self._state == "idle":
                return
            self._transition("ended")
        self._emit()

    def _on_error(self, _event: object) -> None:
        with self._lock:
            if self._state == "idle":
                return
            self._last_error = self._last_error or "playback error"
            self._transition("error")
        self._emit()

    def _on_time(self, _event: object) -> None:
        # Position changed. We don't actually emit on every tick —
        # the HTTP SSE layer polls snapshot() at 4Hz and compares seq.
        # But we bump seq so the SSE layer notices movement.
        with self._lock:
            self._seq += 1

    # --- internals ---------------------------------------------------------

    def _transition(self, state: str, track_id: Optional[str] = None) -> None:
        self._state = state
        if track_id is not None:
            self._current_track_id = track_id
        self._seq += 1

    def _bump_seq(self) -> None:
        self._seq += 1

    def _emit(self) -> None:
        snap = self.snapshot()
        # Copy the list so a mid-iteration unsubscribe is safe.
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(snap)
            except Exception:
                log.exception("player listener raised")

    def _resolve_stream(
        self, track_id: str, quality: Optional[str]
    ) -> tuple[str, Optional[float]]:
        """Resolve a Tidal track id to a playable path.

        Two paths:
          1. Local: the user has this track downloaded. Return the file
             path directly — libvlc decodes local FLAC / AAC natively.
             No network, no session required.
          2. Streaming: fetch the DASH manifest via tidalapi (same path
             the downloader uses) and write it to a temp MPD file.
             libvlc's built-in DASH demuxer handles segment fetching.

        Returns (path, duration_seconds).
        """
        if self._local_lookup is not None:
            local_path = self._local_lookup(track_id)
            if local_path:
                return local_path, None
        session = self._session_getter()
        # Clamp the requested quality to whatever the user's
        # subscription allows — otherwise a user who's saved "Max"
        # from the settings picker but only has the HiFi tier would
        # 401 on every stream. The downloader path does the same
        # clamping upstream; the player path used to skip it.
        if quality and self._quality_clamp is not None:
            try:
                clamped = self._quality_clamp(quality)
                if clamped and clamped != quality:
                    log.info(
                        "clamping streaming quality %r -> %r (subscription ceiling)",
                        quality,
                        clamped,
                    )
                    quality = clamped
            except Exception:
                pass
        override = None
        if quality:
            try:
                override = tidalapi.Quality[quality]
            except KeyError:
                log.warning("unknown quality %r, using session default", quality)
        original = session.config.quality
        if override is not None:
            session.config.quality = override
        try:
            track = session.track(int(track_id))
            stream = track.get_stream()
            manifest = stream.get_stream_manifest()
            if getattr(manifest, "is_encrypted", False):
                raise RuntimeError("encrypted stream — can't decode")
            raw = getattr(manifest, "manifest", None)
            if not raw:
                raise RuntimeError("empty manifest from Tidal")
            mpd_bytes = (
                base64.b64decode(raw) if isinstance(raw, str) else raw
            )
            fd, mpd_path = tempfile.mkstemp(suffix=".mpd", prefix="tdl-vlc-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(mpd_bytes)
            except Exception:
                _safe_unlink(mpd_path)
                raise
            duration = getattr(track, "duration", None)
            return mpd_path, float(duration) if duration else None
        finally:
            if override is not None:
                session.config.quality = original


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


_singleton: Optional[VLCPlayer] = None
_singleton_lock = threading.Lock()


def get_player(
    session_getter: Callable[[], tidalapi.Session],
    local_lookup: Optional[Callable[[str], Optional[str]]] = None,
    quality_clamp: Optional[Callable[[str], Optional[str]]] = None,
) -> VLCPlayer:
    """Lazy singleton. Constructed on first call so importing the module
    is free even when VLC isn't in use."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = VLCPlayer(
                session_getter,
                local_lookup=local_lookup,
                quality_clamp=quality_clamp,
            )
        return _singleton


def shutdown() -> None:
    """Stop playback and release the singleton. Safe to call multiple
    times. Used at process shutdown."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            try:
                _singleton.stop()
            except Exception:
                pass
            _singleton = None


def _safe_unlink(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except Exception:
        pass


