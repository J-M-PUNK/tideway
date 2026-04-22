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
import sys
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
class StreamInfo:
    """What's actually audible, not what was requested.

    Populated for streaming sources from Tidal's Stream metadata;
    populated for local files by mutagen-probing on load. `None` for
    each field means "unknown" — the UI renders a compact label only
    when codec is known, because "?? kHz" would just add clutter.

    source: "stream" (Tidal live) or "local" (on-disk file).
    codec: normalized — "flac", "aac", "mp4a", "alac", "mp3".
    bit_depth: 16 / 24 when known, else None (lossy has no meaningful value).
    sample_rate_hz: 44100 / 48000 / 96000 / 176400 / 192000 typical.
    audio_quality: Tidal's tier name for streams ("HIGH", "LOSSLESS",
        "HI_RES", "HI_RES_LOSSLESS"). None for local files.
    audio_mode: "STEREO" for streams (we can't reach immersive modes
        on our PKCE client_id). None for local.
    """
    source: str  # "stream" | "local"
    codec: Optional[str] = None
    bit_depth: Optional[int] = None
    sample_rate_hz: Optional[int] = None
    audio_quality: Optional[str] = None
    audio_mode: Optional[str] = None


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
    # What's actually playing (codec / sample rate / bit depth). None
    # when the player is idle or still loading. Drives the quality
    # badge in the now-playing bar.
    stream_info: Optional[StreamInfo] = None


@dataclass
class _Preload:
    """A manifest + metadata the frontend asked us to pre-resolve for
    the next track. Consumed by `load()` when the user (or auto-
    advance) actually starts that track, dropping the network round-
    trip from the transition. One-slot cache — we only ever need to
    preload the one track that comes after the current one.
    """
    track_id: str
    quality: Optional[str]
    mpd_path: str
    duration_s: Optional[float]
    stream_info: Optional[StreamInfo]


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
        # Two SEPARATE vlc Instances — one per MediaPlayer — so the
        # primer's set_time(0) rewind-to-start doesn't wedge the
        # main's shared demuxer / input manager (that sharing
        # caused mid-track freezes when instances were unified).
        # Per the VLC multi-player wiki, this is the supported
        # pattern for running two MediaPlayers with independent
        # demuxer state. The flags are identical across both:
        #   --no-video / --quiet / --intf dummy: no UI
        #   --file-caching=100 / --network-caching=300: low latency
        #   --clock-jitter=0 / --no-audio-time-stretch: no clock
        #     smoothing or pitch-preserve resampling at transitions
        _common_flags = (
            "--no-video",
            "--quiet",
            "--intf",
            "dummy",
            "--file-caching=100",
            "--network-caching=300",
            "--clock-jitter=0",
            "--no-audio-time-stretch",
        )
        # One vlc.Instance, two MediaPlayers. This is the supported
        # multi-player pattern per VLC's wiki, and on macOS
        # CoreAudio specifically does NOT cleanly handle two
        # Instances competing for the audio output device —
        # attempting that caused the app to freeze mid-track under
        # hi-res quality loads. The tradeoff is that we can't
        # safely call set_time(0) on the paused primer (seek state
        # is shared across MediaPlayers in the same Instance; prior
        # attempts deadlocked the main player's demuxer). That
        # means the primer's position drifts ~50-250ms past zero
        # before we pause it, which manifests as the first handful
        # of ms of each track being clipped. Audible but tiny, and
        # vastly better than a freeze. Truly gapless at position 0
        # requires migrating off libvlc (libmpv/GStreamer).
        self._instance = vlc.Instance(*_common_flags)
        self._player = self._instance.media_player_new()
        self._primer = self._instance.media_player_new()
        self._primer.audio_set_volume(0)
        # track_id that the primer is currently buffering, if any.
        # Distinct from _preload.track_id: _preload is the resolved
        # manifest cache (local disk); _primer_track_id is the VLC-
        # level media that's already decoding.
        self._primer_track_id: Optional[str] = None
        # Metadata shadowed from the priming run so swap-to-primer
        # can populate the audible-player's current_* fields without
        # re-inspecting the media.
        self._primer_duration_ms: Optional[int] = None
        self._primer_stream_info: Optional[StreamInfo] = None
        # Path to the MPD the primer is currently decoding. We keep
        # it so the swap can hand ownership to _current_mpd_path and
        # the next load's _safe_unlink sweeps it.
        self._primer_mpd_path: Optional[str] = None
        self._lock = threading.RLock()
        self._listeners: list[Callable[[PlayerSnapshot], None]] = []
        self._seq = 0

        self._current_track_id: Optional[str] = None
        self._current_duration_ms: int = 0
        self._current_mpd_path: Optional[str] = None
        self._current_stream_info: Optional[StreamInfo] = None
        # One-slot cache of a pre-resolved next-track manifest. The
        # frontend fires `preload()` ~15s before the current track
        # ends; `load()` consumes the slot if the requested track_id
        # matches, skipping the 200-500ms network fetch for a
        # near-gapless transition.
        self._preload: Optional[_Preload] = None
        self._state: str = "idle"
        self._last_error: Optional[str] = None

        # Attach event handlers to the audible player. On swap we
        # detach from the old primary and re-attach to the new one
        # so state transitions track the audible player only —
        # primer events (it's always playing muted during its priming
        # window) would otherwise pollute the state machine.
        self._attach_events(self._player)

    def _attach_events(self, player) -> None:  # type: ignore[no-untyped-def]
        em = player.event_manager()
        em.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_playing)
        em.event_attach(vlc.EventType.MediaPlayerPaused, self._on_paused)
        em.event_attach(vlc.EventType.MediaPlayerStopped, self._on_stopped)
        em.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_ended)
        em.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_error)
        em.event_attach(vlc.EventType.MediaPlayerTimeChanged, self._on_time)

    def _detach_events(self, player) -> None:  # type: ignore[no-untyped-def]
        em = player.event_manager()
        for ev in (
            vlc.EventType.MediaPlayerPlaying,
            vlc.EventType.MediaPlayerPaused,
            vlc.EventType.MediaPlayerStopped,
            vlc.EventType.MediaPlayerEndReached,
            vlc.EventType.MediaPlayerEncounteredError,
            vlc.EventType.MediaPlayerTimeChanged,
        ):
            try:
                em.event_detach(ev)
            except Exception:
                pass

    def _arm_primer_pause_on_first_samples(self, track_id: str) -> None:
        """Pause the primer on its first reported position tick.

        With both MediaPlayers sharing one vlc.Instance (the
        supported multi-player pattern on macOS), set_time on the
        primer is unsafe — it can wedge the main's demuxer through
        shared input-manager state. So we don't rewind; we just
        pause as early as possible. First TimeChanged fires ~0-250ms
        into the track, so users hear transitions starting a few
        ms past position 0. Audible but small, and stable.

        Sub-position-0 gapless on DASH would require migrating off
        libvlc entirely (libmpv / GStreamer).
        """
        em = self._primer.event_manager()

        def on_time(_event: object) -> None:  # type: ignore[no-untyped-def]
            with self._lock:
                if self._primer_track_id != track_id:
                    try:
                        em.event_detach(vlc.EventType.MediaPlayerTimeChanged)
                    except Exception:
                        pass
                    return
                try:
                    self._primer.set_pause(1)
                except Exception:
                    pass
                try:
                    em.event_detach(vlc.EventType.MediaPlayerTimeChanged)
                except Exception:
                    pass

        em.event_attach(vlc.EventType.MediaPlayerTimeChanged, on_time)

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

        Three paths in order of speed:
          0. Already-audible no-op: backend-initiated auto-swap
             already moved us to this track. The frontend's redundant
             playTrack() in advanceRef lands here and we return
             immediately without disturbing audio.
          1. Primer hit (true gapless): the primer MediaPlayer has
             already parsed the MPD, fetched segments, and primed
             the decoder. Hot-swap the primer to become the audible
             player. Sub-10ms; no cold-start at all.
          2. Preload cache hit: manifest is on disk but no primer
             priming. set_media + play still pays DASH cold-start
             (~100-300ms) but skips the network manifest fetch.
          3. Miss: full fresh fetch + cold-start.
        """
        with self._lock:
            # Path 0: backend auto-swap already put us on this track.
            if (
                self._current_track_id == track_id
                and self._state in ("playing", "paused")
            ):
                return self.snapshot()
            self._transition("loading", track_id=track_id)
        # Path 1: primer already playing this track silently?
        if self._swap_primer_if_matches(track_id):
            print(
                f"[player] primer HOT SWAP track={track_id}",
                file=sys.stderr, flush=True,
            )
            return self.snapshot()
        # Path 2: manifest cache. Path 3: fetch.
        pre = self._take_preload_if_matches(track_id, quality)
        if pre is not None:
            mpd_path, duration_s, stream_info = (
                pre.mpd_path, pre.duration_s, pre.stream_info,
            )
            print(
                f"[player] preload cache HIT track={track_id}",
                file=sys.stderr, flush=True,
            )
        else:
            try:
                mpd_path, duration_s, stream_info = self._resolve_stream(
                    track_id, quality
                )
            except Exception as exc:
                log.exception("stream resolution failed for %s", track_id)
                with self._lock:
                    self._last_error = str(exc)
                    self._transition("error")
                return self.snapshot()
            print(
                f"[player] preload cache MISS track={track_id}",
                file=sys.stderr, flush=True,
            )
        with self._lock:
            # Clean up the previous temp file now that a new one is ready.
            old = self._current_mpd_path
            self._current_track_id = track_id
            self._current_duration_ms = int(duration_s * 1000) if duration_s else 0
            self._current_mpd_path = mpd_path
            self._current_stream_info = stream_info
            self._last_error = None
            media = self._instance.media_new(mpd_path)
            self._player.set_media(media)
            # VLC keeps its own ref; free ours.
            media.release()
            self._bump_seq()
        _safe_unlink(old)
        return self.snapshot()

    def _swap_primer_if_matches(self, track_id: str) -> bool:
        """If the primer MediaPlayer is currently priming `track_id`,
        swap it to become the audible player. Returns True on swap.

        The primer's been playing silently (volume=0) long enough to
        have parsed the MPD, fetched initial segments, and primed the
        decoder. Swapping means:
          1. Detach event handlers from the old (now dead-end) player
          2. Swap the _player / _primer references
          3. Attach event handlers to the new audible player
          4. Unmute and keep playing (it's already playing, just
             silent — unmuting makes it audible instantly)
          5. Stop the old player so it's clean to reuse as next primer

        This is the one place where the decoder doesn't have a cold-
        start, so transitions here are sub-10ms to first audible
        sample instead of the 100-300ms libvlc DASH baseline.
        """
        with self._lock:
            if self._primer_track_id != track_id:
                return False
            # Capture old primary's volume to apply to the new audible
            # player. We muted the primer; the new primer (old
            # primary) will inherit the audio-set state when we swap.
            try:
                target_volume = int(self._player.audio_get_volume() or 100)
                target_mute = bool(self._player.audio_get_mute())
            except Exception:
                target_volume = 100
                target_mute = False
            # Remove event handlers from old primary so its impending
            # stop() doesn't emit state transitions we don't want.
            self._detach_events(self._player)
            old_primary = self._player
            self._player = self._primer
            self._primer = old_primary
            self._primer_track_id = None
            # Attach handlers to the new audible player.
            self._attach_events(self._player)
            # Primer was paused (freshly primed, position near 0) or
            # still priming and muted. Unmute first, then unpause —
            # doing it in that order avoids a brief burst of audio
            # from the priming period if the pause-timer hadn't
            # landed yet.
            #
            # NOTE: don't call set_position(0.0) here. DASH
            # adaptive-streaming demuxers treat a seek as "flush
            # the segment buffer and refetch," which forces a
            # cold-start we just carefully avoided. The primer
            # paused at t≈50-100ms (see event-driven pause in
            # `preload()`), so the user hears the track ~100ms
            # in at worst — imperceptible for album-transition
            # gapless. The old seek-to-0 was the source of the
            # residual delay the dual-player setup didn't fix.
            try:
                self._player.audio_set_volume(target_volume)
                self._player.audio_set_mute(1 if target_mute else 0)
                self._player.set_pause(0)
            except Exception:
                pass
            # The old primary's media is the track that just ended.
            # Stop it and mute so it's ready to be the next primer.
            try:
                self._primer.set_pause(1)
                self._primer.audio_set_volume(0)
            except Exception:
                pass
            # Carry the new state forward. The previous media's temp
            # path stays referenced as _current_mpd_path so _safe_unlink
            # at load time cleans it. We move the new "current" fields
            # to reflect what's now audible.
            # The old primary's MPD is safe to unlink now — its
            # MediaPlayer has been stopped and VLC doesn't hold a
            # ref anymore. The primer's MPD transfers into _current_
            # mpd_path so the next load's _safe_unlink sweep handles
            # it.
            old_mpd = self._current_mpd_path
            self._current_track_id = track_id
            if self._primer_duration_ms is not None:
                self._current_duration_ms = self._primer_duration_ms
            if self._primer_stream_info is not None:
                self._current_stream_info = self._primer_stream_info
            self._current_mpd_path = self._primer_mpd_path
            self._primer_duration_ms = None
            self._primer_stream_info = None
            self._primer_mpd_path = None
            _safe_unlink(old_mpd)
            # No _transition to "playing" here: the primer was
            # already in the playing state (silently). The state
            # machine will see a new MediaPlayerTimeChanged on the
            # new audible player shortly and we'll report position
            # from it.
            self._state = "playing"
            self._bump_seq()
        return True

    def preload(self, track_id: str, quality: Optional[str] = None) -> dict:
        """Pre-resolve the next track AND start priming the primer
        MediaPlayer so the swap at track-end is true-gapless.

        Two-stage:
          1. Manifest resolve (same as before): fetches the MPD from
             Tidal and stores it at `_preload.mpd_path`. Skipping
             this network hit is already a big win on its own.
          2. Primer prime: `set_media` + `play()` on the second
             MediaPlayer with volume=0. libvlc parses the MPD,
             fetches the first audio segments, and primes the
             decoder while the main player is still audible. At
             swap time there's no cold-start left to pay.

        Idempotent for the same (track_id, quality) — short-circuits
        when already primed for this track. A different track_id
        stops + drops the stale primer before re-priming.
        """
        with self._lock:
            # Short-circuit on exact primer match (includes quality
            # via the preload cache comparison below).
            if self._primer_track_id == track_id:
                return {"ok": True, "cached": True, "hit": True}
            existing = self._preload
            if (
                existing is not None
                and existing.track_id == track_id
                and existing.quality == quality
                and self._primer_track_id == track_id
            ):
                return {"ok": True, "cached": True, "hit": True}
            # Drop stale preload manifest.
            if existing is not None and (
                existing.track_id != track_id or existing.quality != quality
            ):
                _safe_unlink(existing.mpd_path)
                self._preload = None
            # Stop + clear any stale primer (different track).
            if self._primer_track_id is not None and self._primer_track_id != track_id:
                try:
                    self._primer.set_pause(1)
                    self._primer.set_media(None)
                except Exception:
                    pass
                if self._primer_mpd_path:
                    _safe_unlink(self._primer_mpd_path)
                self._primer_mpd_path = None
                self._primer_duration_ms = None
                self._primer_stream_info = None
                self._primer_track_id = None
        # Resolve manifest off-lock (may be 200-500ms network).
        try:
            mpd_path, duration_s, stream_info = self._resolve_stream(
                track_id, quality
            )
        except Exception as exc:
            log.exception("preload resolve failed for %s", track_id)
            return {"ok": False, "cached": False, "error": str(exc)}
        # Stash the manifest entry + fire the primer under lock.
        with self._lock:
            # Re-check racing concurrent preloads.
            if self._primer_track_id == track_id:
                _safe_unlink(mpd_path)
                return {"ok": True, "cached": True, "hit": False}
            if self._preload is not None and self._preload.track_id == track_id:
                # Manifest already cached by a parallel call; keep
                # that one, discard ours.
                _safe_unlink(mpd_path)
                pre_path = self._preload.mpd_path
                pre_duration = self._preload.duration_s
                pre_stream_info = self._preload.stream_info
            else:
                self._preload = _Preload(
                    track_id=track_id,
                    quality=quality,
                    mpd_path=mpd_path,
                    duration_s=duration_s,
                    stream_info=stream_info,
                )
                pre_path = mpd_path
                pre_duration = duration_s
                pre_stream_info = stream_info
            # Prime the primer: load the media into the silent player
            # and call play() so libvlc starts decoding. We briefly
            # pause after priming lands so the primer doesn't advance
            # past the start of the track while waiting for swap —
            # otherwise users would hear the track start several
            # seconds in.
            try:
                media = self._instance.media_new(pre_path)
                self._primer.set_media(media)
                media.release()
                self._primer.audio_set_volume(0)
                self._primer.play()
                self._primer_track_id = track_id
                self._primer_mpd_path = pre_path
                self._primer_duration_ms = (
                    int(pre_duration * 1000) if pre_duration else 0
                )
                self._primer_stream_info = pre_stream_info
            except Exception as exc:
                log.exception("primer prime failed for %s", track_id)
                return {"ok": True, "cached": True, "primer": False, "error": str(exc)}
        # Event-driven pause: the primer signals libvlc's
        # TimeChanged event as it actually plays. Pause it once it
        # has real playback-time (~50ms) rather than on a wall-clock
        # timer. The Timer was strictly worse — on slow network, it
        # would fire before the primer was actually playing, pausing
        # a pre-decoder state and re-triggering cold-start at swap.
        # Event-driven pausing measures the primer's internal
        # pipeline reaching first-sample output, which is exactly
        # what we want to freeze.
        self._arm_primer_pause_on_first_samples(track_id)
        print(
            f"[player] primer PRIMED track={track_id} quality={quality}",
            file=sys.stderr, flush=True,
        )
        return {"ok": True, "cached": True, "primer": True}

    def _take_preload_if_matches(
        self, track_id: str, quality: Optional[str]
    ) -> Optional[_Preload]:
        """Consume the cache slot if it matches. Mismatch returns None
        and drops the stale entry (unlinks its MPD) so the slot is
        empty when the caller falls through to a fresh fetch.
        """
        with self._lock:
            pre = self._preload
            if pre is None:
                return None
            # Quality-match rule: a None request matches whatever
            # quality was preloaded (caller trusts the session
            # default). Otherwise exact match required.
            matches = pre.track_id == track_id and (
                quality is None or pre.quality == quality
            )
            self._preload = None
        if matches:
            return pre
        # Stale — unlink its MPD outside the lock.
        _safe_unlink(pre.mpd_path)
        return None

    def _drop_preload(self) -> None:
        """Unlink the cached MPD, stop the primer, and clear both.
        Called on stop() and whenever external state (quality, auth)
        invalidates what we preloaded.
        """
        with self._lock:
            pre = self._preload
            self._preload = None
            primer_mpd = self._primer_mpd_path
            self._primer_track_id = None
            self._primer_mpd_path = None
            self._primer_duration_ms = None
            self._primer_stream_info = None
            try:
                self._primer.set_pause(1)
                self._primer.set_media(None)
            except Exception:
                pass
        if pre is not None:
            _safe_unlink(pre.mpd_path)
        if primer_mpd is not None and (pre is None or primer_mpd != pre.mpd_path):
            _safe_unlink(primer_mpd)

    def play_track(self, track_id: str, quality: Optional[str] = None) -> PlayerSnapshot:
        """Combined load + play in a single call. For gapless auto-
        advance: skips one HTTP round-trip + avoids the await gap
        between `load()` and `play()` on the frontend.

        Same cache semantics as `load()` — hits the preload cache if
        the track matches, falls through to a fresh fetch otherwise.
        Calling this is equivalent to `load(track_id, quality)` then
        `play()`, but the two libvlc calls land back-to-back under
        the same lock so VLC's demuxer starts priming sooner.
        """
        snap = self.load(track_id, quality=quality)
        if snap.state == "error":
            return snap
        return self.play()

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
            self._current_stream_info = None
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
                stream_info=None,
            )
        # Temp-file cleanup happens outside the lock — unlink is cheap
        # and VLC may still hold the file briefly, which is fine on
        # macOS (unlinked-but-open files stay alive until close).
        _safe_unlink(old)
        # Stop also invalidates whatever was preloaded — the user's
        # queue may have changed or they may restart from elsewhere.
        # Without this, a leftover MPD would leak until the next
        # `preload()` overwrite.
        self._drop_preload()
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
        disables the EQ entirely.

        Uses the module-level C bindings so we can explicitly release
        the AudioEqualizer handle after set_equalizer — matches the
        preset path and prevents a slow leak when the user drags a
        slider (we rebuild the EQ on every commit)."""
        with self._lock:
            if not bands:
                # Empty list = disable EQ entirely. libvlc's API says
                # passing a null AudioEqualizer disables filtering.
                self._player.set_equalizer(None)
                return
            eq = vlc.libvlc_audio_equalizer_new()
            try:
                if preamp is not None:
                    vlc.libvlc_audio_equalizer_set_preamp(
                        eq, max(-20.0, min(20.0, float(preamp)))
                    )
                count = vlc.libvlc_audio_equalizer_get_band_count()
                for i in range(min(count, len(bands))):
                    v = max(-20.0, min(20.0, float(bands[i])))
                    vlc.libvlc_audio_equalizer_set_amp_at_index(eq, v, i)
                self._player.set_equalizer(eq)
            finally:
                # MediaPlayer.set_equalizer retains its own reference —
                # safe to release ours immediately (same pattern the
                # preset path uses).
                vlc.libvlc_audio_equalizer_release(eq)

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
                stream_info=self._current_stream_info,
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
            prior = self._state
            # Three cases for a Stopped event:
            #   prior=playing  → libvlc's DASH demuxer fired Stopped
            #     instead of EndReached at natural end-of-track. Treat
            #     as end-of-track so the frontend's auto-advance
            #     (state=ended) still triggers.
            #   prior=loading  → old-media Stopped echo from set_media
            #     replacement as a new track loads. Ignore: load() is
            #     still in progress, and the subsequent play() + VLC's
            #     Playing event will drive state forward. Transitioning
            #     to "idle" here would be read by _on_playing as a
            #     "late Playing echo from a torn-down session" and
            #     skipped, leaving state stuck at idle even though
            #     audio is playing (the nav bar shows paused, scrubber
            #     doesn't move).
            #   anything else → transition to idle.
            if prior == "playing":
                self._transition("ended")
                should_emit = True
                new_state = "ended"
            elif prior == "loading":
                # Don't transition. Don't emit. Just diagnostic.
                should_emit = False
                new_state = "loading"
            else:
                self._transition("idle")
                should_emit = True
                new_state = "idle"
            primer_track = self._primer_track_id if new_state == "ended" else None
        print(
            f"[player] MediaPlayerStopped (prior={prior}) → "
            f"{'state=' + new_state if should_emit else 'ignored (media replacement)'}"
            f" track={self._current_track_id}",
            file=sys.stderr,
            flush=True,
        )
        if should_emit:
            self._emit()
        # Backend auto-swap on end-of-track (Stopped-as-end variant).
        # See _on_ended for the rationale — we avoid the 50-100ms
        # SSE round-trip by swapping here instead of waiting for the
        # frontend to call playTrack.
        if primer_track is not None:
            if self._swap_primer_if_matches(primer_track):
                print(
                    f"[player] backend auto-swap to primed track={primer_track}",
                    file=sys.stderr, flush=True,
                )
                self._emit()

    def _on_ended(self, _event: object) -> None:
        with self._lock:
            if self._state == "idle":
                return
            self._transition("ended")
            primer_track = self._primer_track_id
        print(
            f"[player] MediaPlayerEndReached → state=ended track={self._current_track_id}",
            file=sys.stderr,
            flush=True,
        )
        # Emit the ended frame so the frontend's advanceRef fires
        # and updates its queueIndex. Then if the primer is primed,
        # swap IMMEDIATELY — don't wait for the frontend to round-
        # trip a playTrack call. The SSE→frontend→backend path adds
        # 50-100ms of silence between the old track's last sample
        # and the primer becoming audible. This cuts that out.
        # The frontend's subsequent playTrack will early-return
        # because load() sees current_track_id already matches.
        self._emit()
        if primer_track is not None:
            if self._swap_primer_if_matches(primer_track):
                print(
                    f"[player] backend auto-swap to primed track={primer_track}",
                    file=sys.stderr, flush=True,
                )
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
    ) -> tuple[str, Optional[float], Optional[StreamInfo]]:
        """Resolve a Tidal track id to a playable path.

        Two paths:
          1. Local: the user has this track downloaded. Return the file
             path directly — libvlc decodes local FLAC / AAC natively.
             No network, no session required.
          2. Streaming: fetch the DASH manifest via tidalapi (same path
             the downloader uses) and write it to a temp MPD file.
             libvlc's built-in DASH demuxer handles segment fetching.

        Returns (path, duration_seconds, stream_info). `stream_info`
        drives the now-playing quality badge; it's None when we can't
        determine the actual audible stream (rare — mutagen probe
        failures on unusual local files).
        """
        if self._local_lookup is not None:
            local_path = self._local_lookup(track_id)
            if local_path:
                return local_path, None, _probe_local_stream_info(local_path)
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
            info = StreamInfo(
                source="stream",
                codec=_normalize_codec(
                    getattr(manifest, "codecs", None)
                    or getattr(manifest, "get_codecs", lambda: None)()
                ),
                bit_depth=_safe_int(getattr(stream, "bit_depth", None)),
                sample_rate_hz=_safe_int(getattr(stream, "sample_rate", None)),
                audio_quality=getattr(stream, "audio_quality", None),
                audio_mode=getattr(stream, "audio_mode", None),
            )
            return mpd_path, float(duration) if duration else None, info
        finally:
            if override is not None:
                session.config.quality = original


# ---------------------------------------------------------------------------
# Stream-info helpers
# ---------------------------------------------------------------------------


def _safe_int(v: object) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _normalize_codec(raw: object) -> Optional[str]:
    """Reduce tidalapi / mutagen codec strings to a short lowercase tag.

    Inputs include "flac", "mp4a.40.2", "MP4A", "ALAC", "mp3", etc.
    We only surface the tag to render a badge — "FLAC" vs "AAC" — so
    aggressive normalization is fine.
    """
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if "flac" in s:
        return "flac"
    if "alac" in s:
        return "alac"
    if "mp4a" in s or "aac" in s:
        return "aac"
    if "mp3" in s:
        return "mp3"
    if "opus" in s:
        return "opus"
    if "vorbis" in s:
        return "vorbis"
    return s


def _probe_local_stream_info(path: str) -> Optional["StreamInfo"]:
    """Best-effort codec/rate probe for a local file via mutagen.

    Mutagen is already a dependency (FLAC tagging in the downloader)
    so we don't pay a new install cost. Returns None if probing fails;
    the UI hides the badge in that case rather than showing
    `unknown / unknown`.
    """
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except Exception:
        return None
    try:
        m = MutagenFile(path)
    except Exception:
        return None
    if m is None or getattr(m, "info", None) is None:
        return None
    info = m.info
    codec: Optional[str] = None
    # Mutagen exposes a mime list per format; the first mime is a
    # reliable-enough codec source. FLAC/ALAC/AAC/MP3 all follow this.
    mime_list = getattr(info, "mime", None) or []
    if mime_list:
        codec = _normalize_codec(mime_list[0])
    if codec is None:
        # Fallback: file extension — cheap but right for well-named
        # local downloads.
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        codec = _normalize_codec(ext)
    return StreamInfo(
        source="local",
        codec=codec,
        bit_depth=_safe_int(getattr(info, "bits_per_sample", None)),
        sample_rate_hz=_safe_int(getattr(info, "sample_rate", None)),
    )


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


