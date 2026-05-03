"""PCMPlayer — PyAV + sounddevice audio engine.

Decodes DASH / local audio with PyAV and drives a sounddevice
OutputStream at the track's native sample rate + native format.
No software resampling or format conversion anywhere in the
playback path; samples hit CoreAudio / WASAPI / ALSA bit-identical
to what's in the source.

Gapless transitions: preloaded next-track PCM is spliced into the
live OutputStream at the sample boundary (same-rate) or triggers a
~50ms stream reopen (cross-rate). Both paths sit behind `preload()`
and fire automatically on natural end-of-track.

Threading model:
  - Main thread (HTTP handlers): calls load/play/pause/resume/seek/
    stop/set_volume/set_muted. Mutates state through a lock; never
    blocks on the audio pipeline.
  - Decoder thread: pulls PCM chunks from `Decoder`, pushes into
    `_pcm_queue`. Exits at EOF or when `_stop_flag` is set. Seek
    is applied in-flight: `seek()` calls `Decoder.request_seek()`
    + drains the queue, and the decoder picks up the seek on its
    next iteration.
  - Audio callback (sounddevice realtime thread): drains the queue,
    fills output buffers. Honours the `_paused` flag (outputs
    silence, doesn't drain) and applies volume/mute.
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np
import requests
import sounddevice as sd  # type: ignore
import tidalapi

from app.audio.decoder import Decoder
from app.audio.eq import (
    BAND_FREQUENCIES_HZ as EQ_BAND_FREQUENCIES,
    Equalizer,
    PRESETS as EQ_PRESETS,
    preset_bands as eq_preset_bands,
)
from app.audio.manifest_cache import ManifestCache
from app.audio.output_devices import list_output_devices
from app.audio.segment_reader import SegmentReader

log = logging.getLogger(__name__)


# Back-pressure between the decoder and the audio callback. Each
# item is one AudioFrame's worth of PCM (~1024 samples typical).
# Sized to hold ~2s of audio at 44.1k, ~1s at 96k, ~0.5s at 192k —
# enough headroom that a single slow segment fetch (TLS handshake,
# CDN edge swap, brief network blip) drains the queue without
# starving the audio callback. Earlier sizing of 30 was tight for
# CD-rate (0.7s) and dangerously tight for hi-res (0.16s at 192k),
# which produced audible stutter on the slower segment fetches
# hi-res implies (3-7 MB per segment vs. 0.5-1 MB for lossless).
# On older driver stacks (Windows 10 LTSB) sustained underruns
# can escalate to a fatal native abort; the bigger queue is the
# real fix and the diagnostic logging in _audio_callback is the
# safety net.
# Memory cost is negligible (~800 KB at int32 stereo).
_PCM_QUEUE_MAX = 100


@dataclass
class StreamInfo:
    """What's actually audible — drives the now-playing quality badge."""
    source: str  # "stream" | "local"
    codec: Optional[str] = None
    bit_depth: Optional[int] = None
    sample_rate_hz: Optional[int] = None
    audio_quality: Optional[str] = None
    audio_mode: Optional[str] = None


@dataclass
class _Preload:
    """A pre-decoded next track. Created by `preload()` while the
    current track still has time to play. At end-of-current-track
    the audio callback tries to splice this in without pausing the
    OutputStream — if `sample_rate` and `sd_dtype` match the
    current stream, the swap is sample-accurate and the user hears
    no gap between tracks.
    """
    track_id: str
    quality: Optional[str]
    duration_ms: int
    stream_info: StreamInfo
    source_urls: Optional[list[str]]
    source_path: Optional[str]
    decoder: Decoder
    queue: "queue.Queue[Optional[np.ndarray]]"
    thread: threading.Thread
    stop_flag: threading.Event
    done: threading.Event
    sample_rate: int
    channels: int
    sd_dtype: str


@dataclass
class PlayerSnapshot:
    state: str  # idle | loading | playing | paused | ended | error
    track_id: Optional[str]
    position_ms: int
    duration_ms: int
    volume: int  # 0..100
    muted: bool
    error: Optional[str] = None
    seq: int = 0
    stream_info: Optional[StreamInfo] = None
    # Force Volume: when true the volume slider in the UI should
    # render disabled (value pinned at 100) because the backend
    # rejects changes while it's on.
    force_volume: bool = False


class PCMPlayer:
    def __init__(
        self,
        session_getter: Callable[[], tidalapi.Session],
        local_lookup: Optional[Callable[[str], Optional[str]]] = None,
        quality_clamp: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self._session_getter = session_getter
        self._local_lookup = local_lookup
        self._quality_clamp = quality_clamp

        self._lock = threading.RLock()
        # Serializes load/stop/seek/preload so concurrent HTTP calls
        # can't race each other into opening overlapping streams or
        # sharing a single decoder across threads. Held for the full
        # duration of a state-changing operation; the audio callback
        # and decoder threads never touch it. RLock so play_track()
        # can take the lock around its load() + play() pair without
        # needing a separate locked helper for play.
        self._pipeline_lock = threading.RLock()
        self._state = "idle"
        self._current_track_id: Optional[str] = None
        self._current_duration_ms: int = 0
        self._current_stream_info: Optional[StreamInfo] = None
        self._last_error: Optional[str] = None
        self._seq = 0
        self._listeners: list[Callable[[PlayerSnapshot], None]] = []

        self._decoder: Optional[Decoder] = None
        self._stream: Optional[sd.OutputStream] = None
        self._stream_sample_rate: Optional[int] = None
        self._pcm_queue: queue.Queue[Optional[np.ndarray]] = queue.Queue(
            maxsize=_PCM_QUEUE_MAX
        )
        self._decoder_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._decoder_done = threading.Event()
        self._callback_carry: Optional[np.ndarray] = None
        self._samples_emitted = 0

        self._paused = False
        self._seeking = False
        self._volume = 100  # 0..100
        self._muted = False
        # External output active: when something else is rendering
        # the audio (Cast device, AirPlay receiver, Tidal Connect
        # target), the local sounddevice OutputStream still runs but
        # writes silence so the user doesn't hear two copies of
        # the music coming from their Mac speakers and the remote
        # device. The PCM tap to those external sinks happens BEFORE
        # this silencing (see audio_callback) so the receivers still
        # get full-amplitude audio at their own volume control.
        # Flag is owned by the various external-output managers
        # (cast.py + tidal_connect.py both flip it on connect /
        # disconnect via the registered silencer hook).
        self._external_output_active: bool = False
        # sounddevice device-index string ("" = system default).
        # Applied on the next _open_output_stream(). Device-switch
        # live is a Phase 6 refinement.
        self._selected_device_id: str = ""
        # Exclusive Mode: request bit-perfect output from the OS audio
        # layer. Applied on the next _open_output_stream() so toggling
        # mid-track waits for the next stream open.
        self._exclusive_mode: bool = False
        # Force Volume: pin software volume at 100; set_volume becomes
        # a no-op while this is on. User attenuates via DAC/OS instead
        # so bit-depth isn't scaled away before the stream leaves us.
        self._force_volume: bool = False
        # Set True around any code path that intentionally stops the
        # current OutputStream because it's about to open a fresh
        # one in its place (set_output_device, exclusive-mode flip,
        # cross-rate gapless bridge). The finished_callback that
        # sounddevice fires from `stream.stop()` checks this flag and
        # skips its end-of-track / device-loss logic so a transient
        # stream replacement doesn't show up to the frontend as
        # `state="ended"` or trigger device-loss recovery against
        # a perfectly healthy device.
        self._replacing_stream: bool = False

        # Remembered source for restart-based seek. For DASH streams
        # we store the full URL list (index 0 = init segment, 1+ =
        # media segments) so seek can build a new SegmentReader
        # starting at the nearest segment. For local files we store
        # the filesystem path. `None` when no track is loaded.
        self._source_urls: Optional[list[str]] = None
        self._source_path: Optional[str] = None

        # Sounddevice dtype the current OutputStream opened with.
        # Stored so the gapless-swap check can verify the preloaded
        # track matches.
        self._stream_sd_dtype: Optional[str] = None
        self._stream_channels: Optional[int] = None

        # Preloaded next track. Populated by `preload()` ~15s before
        # the current track ends; consumed in-place by the audio
        # callback at end-of-track.
        self._preload: Optional[_Preload] = None

        # Stream-manifest cache. Keyed by (track_id, quality_or_None).
        # Frontend hover / album-mount prefetch writes here so the
        # first real click is free of the playbackinfo round-trip
        # and, if bytes are warmed, of the init + first-media
        # segment fetches too. See app/audio/manifest_cache.py.
        self._manifest_cache = ManifestCache()

        # 10-band biquad EQ applied in the audio callback. One
        # instance owned for the lifetime of the player; when the
        # output stream rate changes (cross-rate bridge) we rebuild
        # it so the filter coefficients are recomputed against the
        # new sample rate. Remembered user settings (bands / preamp)
        # survive the rebuild.
        self._eq: Optional[Equalizer] = None
        self._eq_bands: list[float] = []
        self._eq_preamp: Optional[float] = None
        # AutoEQ headphone-profile mode (see
        # docs/autoeq-headphone-profiles-scope.md). Mutually
        # exclusive with `_eq_bands` — switching modes clears
        # whichever isn't active. Held here (rather than just an
        # SOS) so the stream-reopen path can recompile coefficients
        # at the new sample rate.
        self._eq_profile = None  # type: ignore[var-annotated]
        # Phase 4 A/B bypass — momentary disable that preserves
        # the active SOS / bands so flipping back is instant. The
        # audio callback reads this each frame; toggling has the
        # same ~immediate effect as a coefficient-clear without
        # the cost of rebuilding when the user toggles back on.
        self._eq_bypass: bool = False

        # Audio-callback diagnostics. Each pair is (count, last-print
        # time) for a different rate-limited stderr message:
        #   _cb_status: PortAudio over/underrun flags from the
        #     callback's `status` arg (driver-level glitch).
        #   _cb_starve: our decoder didn't push a chunk in time
        #     (our-side fault — slow disk / network / CPU).
        # Initialised here rather than lazy-init via getattr in the
        # callback so the attribute names live in one place and a
        # typo at the use site is a fail-fast AttributeError, not a
        # silently-zeroed counter.
        self._cb_status_count = 0
        self._cb_status_last_print = 0.0
        self._cb_starve_count = 0
        self._cb_starve_last_print = 0.0
        # Heartbeat tick counter, only incremented when _DEBUG is on.
        # 1-per-callback log every 100 ticks confirms the callback
        # is actually running + advancing.
        self._callback_counter = 0
        # Seq-bump counter for the audio callback's position-tick
        # nudges. Bumped every callback; emits a fresh seq every
        # 20 callbacks (~4-5 Hz at typical 86 Hz callback rate) so
        # the SSE position-update path lets the snapshot through.
        self._seq_bump_counter = 0

        # macOS audio-route listener. Subscribes to TWO CoreAudio
        # events:
        #   1. Default output device changed (headphones unplug
        #      auto-reroutes to speakers; user picks a different
        #      device in Sound prefs).
        #   2. Device list changed (a device plugged in or out,
        #      independent of whether it became the default).
        # Both feed the same handler — recovery is idempotent and
        # the player's state checks naturally dedupe.
        #
        # The dual subscription is important for "headphones
        # plugged in but macOS didn't auto-route to them" (a
        # supported Sound-pref configuration). Without the
        # device-list listener, PortAudio's enumeration stays
        # stale after that plug-in event and the picker never
        # shows the new headphones until the user restarts.
        self._audio_route_listener_unregister: Optional[
            Callable[[], None]
        ] = None
        if sys.platform == "darwin":
            try:
                from app.audio.macos_audio_devices import (
                    register_audio_route_listener,
                )
                self._audio_route_listener_unregister = (
                    register_audio_route_listener(
                        self._on_audio_route_changed
                    )
                )
                if self._audio_route_listener_unregister is not None:
                    print(
                        "[audio] subscribed to CoreAudio default-output "
                        "+ device-list changes",
                        flush=True,
                    )
            except Exception:
                log.exception(
                    "audio-route listener registration crashed"
                )

    def _on_audio_route_changed(self) -> None:
        """Fired (on a CoreAudio thread) whenever the audio routing
        topology changes — default output device flipped OR a
        device plugged in / unplugged.

        We respond with `_recover_from_device_loss` regardless of
        which event fired. The recovery is dual-purpose: when the
        selected device disappeared it falls back to default, when
        the selected device is still around it reopens on the
        original (preserving the user's manual selection AND
        refreshing PortAudio's enumeration so a newly-plugged-in
        device shows up in the picker).

        Idempotent: if the player is idle / error / ended, recovery
        short-circuits and we just need PortAudio to re-enumerate
        on the next picker open.

        Spawned on a fresh thread because we're on a CoreAudio
        internal thread here and shouldn't re-enter stream
        machinery (PortAudio reinit specifically would deadlock).
        """
        with self._lock:
            state = self._state
        if state not in ("playing", "paused", "loading"):
            # No active stream. The picker will refresh PortAudio
            # itself the next time the user opens it, so there's
            # nothing to do here.
            return
        print(
            f"[audio] audio route changed (state={state!r}); "
            "kicking recovery",
            flush=True,
        )
        threading.Thread(
            target=self._recover_from_device_loss,
            name="pcm-audio-route-changed",
            daemon=True,
        ).start()

    # --- public API -------------------------------------------------

    def subscribe(
        self, listener: Callable[[PlayerSnapshot], None]
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return _unsub

    def load(
        self, track_id: str, quality: Optional[str] = None
    ) -> PlayerSnapshot:
        """Resolve stream/local source, open decoder + output stream.
        Doesn't start playback; caller must `play()` next.

        Serialized on `_pipeline_lock`: concurrent HTTP calls (rapid
        skip, backend auto-swap colliding with frontend play_track,
        etc.) resolve in order rather than racing into overlapping
        sounddevice streams.
        """
        with self._pipeline_lock:
            return self._load_locked(track_id, quality)

    def _load_locked(
        self, track_id: str, quality: Optional[str]
    ) -> PlayerSnapshot:
        self._dbg(
            f"load track={track_id} quality={quality} "
            f"current_track={self._current_track_id} "
            f"current_state={self._state} "
            f"preload={self._preload.track_id if self._preload else 'None'}"
        )
        with self._lock:
            # Path 0: already audible on this track. Happens when
            # the callback's gapless swap already transitioned us
            # to the next track and the frontend's redundant
            # play_track() lands here. Don't tear anything down —
            # we're already playing what the caller asked for.
            if (
                self._current_track_id == track_id
                and self._state in ("playing", "paused")
            ):
                self._dbg(f"load Path 0: already playing track={track_id}")
                return self.snapshot()
            # Path 0b: a preload for this track is already buffering.
            # Adopt it without tearing down the sounddevice stream.
            # When rates + dtype match, the active OutputStream just
            # starts being fed from the preload's queue instead —
            # which is the whole point of the preload. This is the
            # path that keeps auto-advance gapless when the frontend
            # races the callback's swap.
            pre = self._preload
            if (
                pre is not None
                and pre.track_id == track_id
                and (quality is None or pre.quality == quality)
                and self._state in ("playing", "paused", "loading")
            ):
                self._dbg(
                    f"load Path 0b: adopting preload track={track_id} "
                    f"(rate {pre.sample_rate}=={self._stream_sample_rate}? "
                    f"dtype {pre.sd_dtype}=={self._stream_sd_dtype}?)"
                )
                return self._adopt_preload_locked(pre)
        # Fall through: no matching preload. Full teardown + fresh
        # resolve. This is the slow path (~300-800ms).
        self._dbg(f"load SLOW PATH: full teardown + fresh resolve for {track_id}")
        load_t0 = time.monotonic()
        self._teardown()
        t_teardown = time.monotonic()
        with self._lock:
            self._transition("loading", track_id=track_id)

        try:
            source_spec, duration_s, stream_info, prefetched_bytes = self._resolve_source(
                track_id, quality
            )
            t_resolved = time.monotonic()
            initial_source = _build_source(source_spec, prefetched=prefetched_bytes)
            decoder = Decoder(initial_source)
            t_decoder = time.monotonic()
        except Exception as exc:
            log.exception("failed to resolve/open source for %s", track_id)
            with self._lock:
                self._last_error = str(exc)
                self._transition("error")
            self._emit()
            return self.snapshot()

        with self._lock:
            self._decoder = decoder
            self._current_duration_ms = (
                int(duration_s * 1000) if duration_s else 0
            )
            self._current_stream_info = stream_info
            self._stream_sample_rate = decoder.sample_rate
            self._stream_sd_dtype = decoder.sounddevice_dtype
            self._stream_channels = decoder.channels
            if isinstance(source_spec, str):
                self._source_path = source_spec
                self._source_urls = None
            else:
                self._source_urls = list(source_spec)
                self._source_path = None
            t_before_stream = time.monotonic()
            self._open_output_stream(
                decoder.sample_rate, decoder.channels, decoder.sounddevice_dtype
            )
            t_stream_open = time.monotonic()
            self._samples_emitted = 0
            self._callback_carry = None
            self._paused = False
            self._seeking = False
            # Fresh events + queue for the new run. An old decoder
            # thread whose join() timed out might still be alive
            # holding the PREVIOUS objects as locals. If we reuse
            # those, the zombie thread would keep pushing pre-load
            # samples into the live queue and see its stop_flag
            # toggle back to "not set," so we hand the new run a
            # clean set of objects.
            self._stop_flag = threading.Event()
            self._decoder_done = threading.Event()
            self._pcm_queue = queue.Queue(maxsize=_PCM_QUEUE_MAX)
            self._last_error = None
            # Transition out of "loading" now that the decoder is up
            # and the output stream is open. We land in "paused"
            # because nothing has called .play() yet — this is the
            # "loaded but not playing" state. play_track() will move
            # us to "playing" via its play() call right after this
            # returns; load()-without-play callers (the restore-on-
            # quit path, the album-end pause-on-track-1 flow) want
            # the snapshot to report "paused" here, not a perpetual
            # "loading" the frontend can't recover from.
            self._transition("paused")

        self._start_decoder_thread()
        t_thread_started = time.monotonic()
        # Emit the full breakdown at the bottom so all phases are on
        # one line. Each segment in milliseconds, total for the
        # click-to-loaded path. The two phases users care about most
        # are `resolve` (Tidal API roundtrip) and `decoder_init`
        # (manifest fetch + first segment fetch + libav probe).
        # `stream_open` is the sounddevice OutputStream open and
        # `thread_start` is the producer-thread spawn cost; both
        # stable across calls so they're floor values.
        print(
            f"[perf] load track={track_id} "
            f"total={(t_thread_started - load_t0) * 1000.0:.0f}ms "
            f"teardown={(t_teardown - load_t0) * 1000.0:.0f}ms "
            f"resolve={(t_resolved - t_teardown) * 1000.0:.0f}ms "
            f"decoder_init={(t_decoder - t_resolved) * 1000.0:.0f}ms "
            f"stream_open={(t_stream_open - t_before_stream) * 1000.0:.0f}ms "
            f"thread_start="
            f"{(t_thread_started - t_stream_open) * 1000.0:.0f}ms",
            file=sys.stderr,
            flush=True,
        )
        self._emit()
        return self.snapshot()

    def play_track(
        self, track_id: str, quality: Optional[str] = None
    ) -> PlayerSnapshot:
        """Combined load + play. The pipeline lock keeps load +
        play atomic relative to other state changes, so a
        concurrent stop / seek can't land in between the two
        phases."""
        with self._pipeline_lock:
            snap = self.load(track_id, quality=quality)
            if snap.state == "error":
                return snap
            return self.play()

    def play(self) -> PlayerSnapshot:
        with self._lock:
            stream = self._stream
            if stream is None:
                return self.snapshot()
            self._paused = False
            try:
                if not stream.active:
                    stream.start()
            except Exception as exc:
                log.exception("stream.start failed")
                self._last_error = str(exc)
                self._transition("error")
                self._emit()
                return self.snapshot()
            self._transition("playing")
        self._emit()
        return self.snapshot()

    def pause(self) -> PlayerSnapshot:
        with self._lock:
            if self._state not in ("playing", "loading"):
                return self.snapshot()
            self._paused = True
            self._transition("paused")
        self._emit()
        return self.snapshot()

    def resume(self) -> PlayerSnapshot:
        return self.play()

    def stop(self) -> PlayerSnapshot:
        with self._pipeline_lock:
            self._teardown()
            with self._lock:
                self._last_error = None
            self._emit()
            return self.snapshot()

    def seek(self, fraction: float) -> PlayerSnapshot:
        """Seek to `fraction` (0..1) of the track duration.

        libav's DASH/fragmented-MP4 seek doesn't work without an
        MPD-derived index, so we can't use `container.seek()` for
        Tidal streams. Instead we rebuild the decoder with a new
        `SegmentReader` that starts at the nearest media segment
        to `target_s`. Seek accuracy is segment-granular (~3-4s
        per segment at hi-res); close enough for scrub UX.

        Local files get a precise seek via `container.seek()` on
        the newly-opened container.
        """
        with self._pipeline_lock:
            fraction = max(0.0, min(1.0, float(fraction)))
            with self._lock:
                duration_ms = self._current_duration_ms
                sample_rate = self._stream_sample_rate or 44100
                if duration_ms <= 0:
                    return self.snapshot()
                target_s = (duration_ms / 1000.0) * fraction
            self._dbg(
                f"seek ENTER fraction={fraction} target_s={target_s:.2f} "
                f"duration_ms={duration_ms} sample_rate={sample_rate} "
                f"samples_emitted_before={self._samples_emitted}"
            )

            # Suppress CallbackStop during the decoder swap. Without
            # this, the brief window where the queue is empty AND the
            # old decoder has exited would let the callback raise
            # CallbackStop and end the stream mid-seek.
            with self._lock:
                self._seeking = True
            effective_s = target_s
            try:
                effective_s = self._restart_decoder_at(target_s)
            except Exception:
                log.exception("seek to %s failed", target_s)
            finally:
                with self._lock:
                    # Use the DECODER'S effective start, not the
                    # user's requested target, so position reporting
                    # matches the audio you actually hear. On DASH
                    # this may be up to one segment earlier than
                    # requested (~3-4s).
                    self._samples_emitted = int(effective_s * sample_rate)
                    self._seeking = False
                    self._seq += 1
            return self.snapshot()

    # --- restart-based seek -----------------------------------------

    def _restart_decoder_at(self, target_s: float) -> float:
        """Tear down the current decoder + thread; build a new
        Decoder starting at or near `target_s`; start a new thread.
        Returns the effective start offset in seconds (= target_s
        for local files, = approximate segment start for DASH —
        which may be up to one segment before target_s).
        """
        seek_t0 = time.monotonic()
        self._stop_flag.set()
        # Cancel the decoder's source BEFORE joining the thread.
        # `stop_flag` only gets checked between PCM frames; if the
        # decoder is mid-segment-fetch in SegmentReader (the common
        # case during user seeks because the queue is small) we'd
        # otherwise wait out the rest of the HTTP read — 150-300 ms
        # of "nothing happens" between click and audio. cancel_source
        # closes the in-flight Response so iter_content raises and
        # the thread exits in single-digit ms.
        old_decoder = self._decoder
        if old_decoder is not None:
            try:
                old_decoder.cancel_source()
            except Exception:
                pass
        thread = self._decoder_thread
        self._decoder_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        t_thread_joined = time.monotonic()

        self._decoder = None
        if old_decoder is not None:
            try:
                old_decoder.close()
            except Exception:
                pass
        t_old_closed = time.monotonic()

        new_source, start_offset_s = self._build_source_at(target_s)
        t_source_built = time.monotonic()
        new_decoder = Decoder(new_source)
        # Match the existing OutputStream's configuration. The stream
        # stays open across a seek; the OLD decoder was configured to
        # emit at `_stream_sample_rate` / matching dtype via the
        # set_target_rate call in _open_output_stream. The NEW
        # Decoder() defaults to source-rate / source-format output —
        # which may not match the stream when shared-mode resampling
        # is in play (e.g. 96 kHz FLAC stream open at the device's
        # 48 kHz mixer rate as float32, but the new decoder produces
        # int32 at 96 kHz). The audio_callback then writes int32
        # bytes into a buffer PortAudio reads as float32, which gets
        # reinterpreted at the wrong scale and sounds like blown-out
        # bit-crushed audio. set_target_rate flips the new decoder
        # into the same flt-at-target_rate config the old one had,
        # or no-ops in the passthrough case (exclusive mode, where
        # stream rate == source rate).
        if self._stream_sample_rate:
            new_decoder.set_target_rate(self._stream_sample_rate)
        t_decoder_init = time.monotonic()
        # For local files the new container starts at t=0; tell it
        # to seek forward to the precise target. For DASH we already
        # opened at the right segment, so the decoder starts near
        # target_s naturally.
        if start_offset_s is None:
            new_decoder.request_seek(target_s)
            effective_s = target_s
        else:
            effective_s = start_offset_s

        with self._lock:
            self._decoder = new_decoder
            self._callback_carry = None
            # Fresh events + queue — see comment in _load_locked().
            self._stop_flag = threading.Event()
            self._decoder_done = threading.Event()
            self._pcm_queue = queue.Queue(maxsize=_PCM_QUEUE_MAX)

        self._start_decoder_thread()
        t_thread_started = time.monotonic()

        # Same shape as the load() perf line so users / devs can read
        # both with the same eyes. `thread_join` covers waiting on
        # the previous decoder to exit (capped at 2 s — long values
        # here mean a stuck producer); `source_build` is the segment-
        # picking arithmetic for DASH or path lookup for local;
        # `decoder_init` is libav opening the new source. Stream
        # stays open across a seek so there's no `stream_open`
        # phase here.
        kind = "local" if start_offset_s is None else "dash"
        print(
            f"[perf] seek target_s={target_s:.2f} kind={kind} "
            f"effective_s={effective_s:.2f} "
            f"total={(t_thread_started - seek_t0) * 1000.0:.0f}ms "
            f"thread_join={(t_thread_joined - seek_t0) * 1000.0:.0f}ms "
            f"old_close="
            f"{(t_old_closed - t_thread_joined) * 1000.0:.0f}ms "
            f"source_build="
            f"{(t_source_built - t_old_closed) * 1000.0:.0f}ms "
            f"decoder_init="
            f"{(t_decoder_init - t_source_built) * 1000.0:.0f}ms "
            f"thread_start="
            f"{(t_thread_started - t_decoder_init) * 1000.0:.0f}ms",
            file=sys.stderr,
            flush=True,
        )
        return effective_s

    def _build_source_at(
        self, target_s: float
    ) -> tuple[Union[str, SegmentReader], Optional[float]]:
        """Return (source, approx_start_s). approx_start_s is None
        for local files (precise seek via container.seek) and the
        segment's approximate start time for DASH.
        """
        if self._source_urls is not None:
            urls = self._source_urls
            num_media = len(urls) - 1
            duration_s = (self._current_duration_ms or 1000) / 1000.0
            if target_s <= 0 or num_media == 0:
                return SegmentReader(urls), 0.0
            idx = max(
                0,
                min(num_media - 1, int(target_s / duration_s * num_media)),
            )
            approx_start_s = (idx / num_media) * duration_s
            # init segment + remaining media segments from idx
            sliced = [urls[0]] + list(urls[1 + idx:])
            return SegmentReader(sliced), approx_start_s
        if self._source_path is not None:
            return self._source_path, None
        raise RuntimeError("no source to seek within")

    _DEBUG = False  # verbose stderr logging — flip on while
    # debugging. At False only [pcm] state-transition prints fire.

    def _dbg(self, msg: str) -> None:
        if PCMPlayer._DEBUG:
            print(f"[pcm] {msg}", file=sys.stderr, flush=True)

    def preload(
        self, track_id: str, quality: Optional[str] = None
    ) -> dict:
        """Pre-decode the next track into a buffer so the audio
        callback can splice it into the current OutputStream at
        end-of-current-track — sample-accurate gapless.

        Idempotent for the same (track_id, quality); swapping the
        request to a different track tears down the existing
        preload first. Serialized on `_pipeline_lock` alongside
        load/stop/seek.
        """
        self._dbg(f"preload ENTER track={track_id} quality={quality}")
        with self._pipeline_lock:
            with self._lock:
                existing = self._preload
                if (
                    existing is not None
                    and existing.track_id == track_id
                    and existing.quality == quality
                ):
                    self._dbg(f"preload already cached for track={track_id}")
                    return {"ok": True, "cached": True, "hit": True}
            # Different track (or no preload) — tear down any stale
            # one before we build the new one.
            self._drop_preload()

            try:
                source_spec, duration_s, stream_info, prefetched_bytes = (
                    self._resolve_source(track_id, quality)
                )
                source = _build_source(source_spec, prefetched=prefetched_bytes)
                decoder = Decoder(source)
            except Exception as exc:
                log.exception("preload resolve failed for %s", track_id)
                return {"ok": False, "error": str(exc)}

            # Configure the preload's output rate to match the active
            # stream so the gapless splice works for any source-rate
            # combination. In shared mode that's the device's mixer
            # rate, so a preloaded 96 k track decoded against a 48 k
            # stream produces 48 k output and joins seamlessly. In
            # exclusive mode we keep the preload at its own source
            # rate; if it doesn't match the active stream, the adopt
            # path reopens the stream (same ~50 ms gap as before).
            current_stream_rate = self._stream_sample_rate
            if not self._exclusive_mode and current_stream_rate:
                decoder.set_target_rate(current_stream_rate)

            q: queue.Queue[Optional[np.ndarray]] = queue.Queue(
                maxsize=_PCM_QUEUE_MAX
            )
            stop_flag = threading.Event()
            done = threading.Event()

            thread = threading.Thread(
                target=PCMPlayer._decoder_loop,
                args=(decoder, q, stop_flag, done),
                name="pcm-preload",
                daemon=True,
            )
            thread.start()

            urls = source_spec if isinstance(source_spec, list) else None
            path = source_spec if isinstance(source_spec, str) else None

            self._dbg(
                f"preload READY track={track_id} "
                f"rate={decoder.output_sample_rate} dtype={decoder.sounddevice_dtype} "
                f"(current stream rate={self._stream_sample_rate} "
                f"dtype={self._stream_sd_dtype})"
            )
            pre = _Preload(
                track_id=track_id,
                quality=quality,
                duration_ms=int(duration_s * 1000) if duration_s else 0,
                stream_info=stream_info,
                source_urls=list(urls) if urls is not None else None,
                source_path=path,
                decoder=decoder,
                queue=q,
                thread=thread,
                stop_flag=stop_flag,
                done=done,
                sample_rate=decoder.output_sample_rate,
                channels=decoder.channels,
                sd_dtype=decoder.sounddevice_dtype,
            )
            with self._lock:
                self._preload = pre
            return {
                "ok": True,
                "cached": True,
                "sample_rate": pre.sample_rate,
                "dtype": pre.sd_dtype,
            }

    def _drop_preload(self) -> None:
        """Stop the preload thread, close its decoder, clear the slot.
        Safe to call from either a locked pipeline operation or from
        the HTTP layer — takes the pipeline lock itself (RLock, so
        nesting is fine).
        """
        with self._pipeline_lock:
            with self._lock:
                pre = self._preload
                self._preload = None
            if pre is None:
                return
            pre.stop_flag.set()
            # Abort any in-flight HTTP fetch on the preload's decoder
            # so its thread exits promptly instead of waiting out the
            # rest of a 150-300 ms segment download. Drop-preload
            # fires on every track change while a preload is buffering;
            # without this, a fast skip stalls behind the dead preload.
            try:
                pre.decoder.cancel_source()
            except Exception:
                pass
            if pre.thread.is_alive():
                pre.thread.join(timeout=2.0)
            try:
                pre.decoder.close()
            except Exception:
                pass

    def set_volume(self, volume: int) -> PlayerSnapshot:
        volume = max(0, min(100, int(volume)))
        with self._lock:
            # Force Volume wins over caller requests — keeps the
            # signal chain bit-perfect. UI disables the slider in
            # that state, but external clients can still try and
            # we just ignore them.
            if not self._force_volume:
                self._volume = volume
            self._seq += 1
        return self.snapshot()

    def set_force_volume(self, enabled: bool) -> PlayerSnapshot:
        """Toggle Force Volume. Turning it on pins _volume to 100
        immediately so any subsequent playback is at full scale."""
        with self._lock:
            self._force_volume = bool(enabled)
            if self._force_volume:
                self._volume = 100
            self._seq += 1
        return self.snapshot()

    def set_muted(self, muted: bool) -> PlayerSnapshot:
        with self._lock:
            self._muted = bool(muted)
            self._seq += 1
        return self.snapshot()

    def set_external_output_active(self, active: bool) -> None:
        """Toggle local-output silencing.

        Called by the Cast / AirPlay / Tidal Connect managers when
        a remote output session opens or closes. While true, the
        audio callback writes silence to the local sounddevice
        OutputStream so the user doesn't hear duplicate audio from
        Mac speakers and the remote device. Source PCM still
        flows to the remote tap before silencing, so the receiver
        gets full-amplitude audio at its own volume control.

        Idempotent. Doesn't change `_volume` or `_muted` — the
        user's manual mute / volume settings are preserved across
        a cast cycle so they don't have to re-set them when audio
        returns to local.
        """
        with self._lock:
            self._external_output_active = bool(active)
            self._seq += 1

    # --- EQ --------------------------------------------------------
    #
    # Per-track coefficients depend on sample_rate, so the
    # Equalizer instance is built / rebuilt in load() /
    # _adopt_preload_locked / _bridge_to_preload, but the user's
    # bands + preamp are remembered on the player and re-applied
    # each rebuild.

    @staticmethod
    def eq_presets() -> list[dict]:
        return [{"index": p["index"], "name": p["name"]} for p in EQ_PRESETS]

    @staticmethod
    def eq_bands_count() -> int:
        return len(EQ_BAND_FREQUENCIES)

    @staticmethod
    def eq_band_frequencies() -> list[float]:
        return list(EQ_BAND_FREQUENCIES)

    def apply_equalizer(
        self, bands: list[float], preamp: Optional[float] = None
    ) -> None:
        """Apply band gains (dB) to the active EQ. Empty `bands` OR
        a flat curve with no preamp disables filtering entirely —
        no point paying 10 biquad operations per sample when the
        curve is unity.

        Remembered so that a stream reopen (cross-rate bridge,
        track load) rebuilds the EQ coefficients against the new
        sample rate without losing the user's curve.

        Calling this also clears any active AutoEQ profile —
        manual and profile modes are mutually exclusive in the
        player.
        """
        is_flat = bands and all(abs(b) < 1e-6 for b in bands) and (
            preamp is None or abs(preamp) < 1e-6
        )
        with self._lock:
            self._eq_bands = list(bands)
            self._eq_preamp = preamp
            self._eq_profile = None
            if self._eq is not None:
                if bands and not is_flat:
                    self._eq.set_bands(list(bands), preamp_db=preamp)
                else:
                    self._eq.clear()

    def set_equalizer_bypass(self, bypassed: bool) -> None:
        """Toggle the A/B bypass flag without touching the active
        EQ configuration. The audio callback reads this each
        frame, so the effect is on the next callback (~10 ms).
        Used by the Phase 4 player-UI button + keyboard shortcut
        for blink-test comparison."""
        with self._lock:
            self._eq_bypass = bool(bypassed)

    def equalizer_bypass(self) -> bool:
        """Read the current bypass flag — server uses it to expose
        the value via /api/eq/state."""
        with self._lock:
            return self._eq_bypass

    def apply_equalizer_profile(self, profile) -> None:
        """Switch to AutoEQ profile mode and apply `profile`. The
        profile object's bands compile to an SOS at the player's
        current sample rate; on a stream reopen the same profile
        is recompiled at the new rate (no loss across cross-rate
        bridges). Clears any manual-mode bands — the modes are
        mutually exclusive."""
        from app.audio.autoeq.apply import profile_to_sos

        with self._lock:
            self._eq_bands = []
            self._eq_preamp = None
            self._eq_profile = profile
            if self._eq is not None:
                sos = profile_to_sos(profile, self._eq.sample_rate())
                if sos.size == 0:
                    self._eq.clear()
                else:
                    self._eq.set_sos(sos, preamp_db=profile.preamp_db)

    def apply_equalizer_preset(self, preset_index: int) -> list[float]:
        """Apply a preset by index, push its curve to the live EQ,
        and return the resolved band amplitudes so the frontend's
        sliders can snap to it."""
        bands = eq_preset_bands(preset_index)
        self.apply_equalizer(bands, preamp=None)
        return bands

    # --- Device selection -------------------------------------------

    def list_output_devices(self) -> list[dict]:
        """Enumerate output-capable audio devices for the picker UI.

        Thin wrapper around `output_devices.list_output_devices`:
        captures `stream_active` under the lock so the listing
        function knows whether it's safe to re-init PortAudio
        (which would tear down a live stream).

        See `app/audio/output_devices.py` for per-platform filter
        rationale and the picker payload shape.
        """
        with self._lock:
            stream_active = self._stream is not None
        return list_output_devices(stream_active)

    def set_output_device(self, device_id: str) -> None:
        """Remember the device-id selection and, if a stream is
        currently open, reopen it on the new device without
        dropping the current decoder/queue. Expected ~50ms of
        silence during the reopen — small blip, same as any
        device-switch in any audio app.
        """
        device_id = device_id or ""
        with self._pipeline_lock:
            with self._lock:
                prev_id = self._selected_device_id
                self._selected_device_id = device_id
                current_stream = self._stream
                sample_rate = self._stream_sample_rate
                channels = self._stream_channels
                sd_dtype = self._stream_sd_dtype
                was_playing = (
                    self._state == "playing"
                    and current_stream is not None
                )
            if (
                not was_playing
                or prev_id == device_id
                or sample_rate is None
                or channels is None
                or sd_dtype is None
            ):
                # Either nothing's playing (picked-up on next load),
                # the device didn't actually change, or we're missing
                # the info we'd need to reopen. Nothing more to do.
                return

            # Close the current stream + open fresh on the new
            # device. The decoder thread keeps filling the PCM
            # queue in the background, so the new stream picks up
            # where the old one left off after its first callback.
            #
            # `_replacing_stream` muzzles the finished_callback that
            # `stream.stop()` triggers — without it, the callback
            # would interpret the stop as a natural end-of-track and
            # transition state to "ended" mid-swap, briefly telling
            # the frontend playback finished.
            with self._lock:
                self._replacing_stream = True
            try:
                try:
                    current_stream.stop()
                except Exception:
                    pass
                try:
                    current_stream.close()
                except Exception:
                    pass
                with self._lock:
                    self._stream = None
                    # Reset the callback's mid-frame carry so the
                    # first post-reopen callback starts on a fresh
                    # chunk boundary — otherwise we could re-emit a
                    # partial chunk of already-played samples.
                    self._callback_carry = None
                    self._open_output_stream(sample_rate, channels, sd_dtype)
                try:
                    self._stream.start()
                except Exception:
                    log.exception("set_output_device: stream start failed")
            finally:
                with self._lock:
                    self._replacing_stream = False

    def set_exclusive_mode(self, enabled: bool) -> None:
        """Flip exclusive-mode on the audio output stream. If a stream
        is open the reopen applies immediately (~50 ms of silence,
        same as a device switch); otherwise the flag kicks in on the
        next track load."""
        enabled = bool(enabled)
        with self._pipeline_lock:
            with self._lock:
                prev = self._exclusive_mode
                self._exclusive_mode = enabled
                current_stream = self._stream
                sample_rate = self._stream_sample_rate
                channels = self._stream_channels
                sd_dtype = self._stream_sd_dtype
                was_playing = (
                    self._state == "playing" and current_stream is not None
                )
            if (
                prev == enabled
                or not was_playing
                or sample_rate is None
                or channels is None
                or sd_dtype is None
            ):
                return

            # Same intentional-stop muzzle as set_output_device — see
            # the comment there.
            with self._lock:
                self._replacing_stream = True
            try:
                try:
                    current_stream.stop()
                except Exception:
                    pass
                try:
                    current_stream.close()
                except Exception:
                    pass
                with self._lock:
                    self._stream = None
                    self._callback_carry = None
                    self._open_output_stream(sample_rate, channels, sd_dtype)
                try:
                    self._stream.start()
                except Exception:
                    log.exception("set_exclusive_mode: stream start failed")
            finally:
                with self._lock:
                    self._replacing_stream = False

    def snapshot(self) -> PlayerSnapshot:
        with self._lock:
            pos_ms = 0
            if self._stream_sample_rate and self._samples_emitted > 0:
                pos_ms = int(
                    self._samples_emitted * 1000 / self._stream_sample_rate
                )
                # Clamp to duration so the UI's progress bar doesn't
                # overshoot when the callback reports samples beyond
                # the real track end (can happen for ~one frame).
                if self._current_duration_ms > 0:
                    pos_ms = min(pos_ms, self._current_duration_ms)
            return PlayerSnapshot(
                state=self._state,
                track_id=self._current_track_id,
                position_ms=pos_ms,
                duration_ms=self._current_duration_ms,
                volume=self._volume,
                muted=self._muted,
                error=self._last_error,
                seq=self._seq,
                stream_info=self._current_stream_info,
                force_volume=self._force_volume,
            )

    # --- internals --------------------------------------------------

    def _teardown(self) -> None:
        """Stop the pipeline fully. State transitions to idle BEFORE
        we close the stream, so the finished_callback short-circuits
        and doesn't emit a spurious 'ended' at user-initiated stops.
        Any preload is also dropped — a stale preload on a
        user-stopped pipeline would fire a bogus bridge when the
        next load() came in.
        """
        # Drop preload first so finished_callback's `pre` read sees
        # None when it fires during stream.close() below.
        self._drop_preload()
        with self._lock:
            # If we're already idle, nothing to do.
            if self._state == "idle" and self._stream is None:
                return
            self._state = "idle"
            self._seq += 1

        self._stop_flag.set()
        # Cancel the decoder's source BEFORE joining the thread.
        # The thread is very often mid-HTTP-fetch inside SegmentReader
        # when a track change fires, and stop_flag is only checked
        # between reads. Closing the source aborts the in-flight
        # request so read() raises on the thread and it exits in
        # single-digit ms instead of ~400ms.
        decoder = self._decoder
        if decoder is not None:
            try:
                decoder.cancel_source()
            except Exception:
                pass

        stream = self._stream
        self._stream = None
        if stream is not None:
            # abort() over stop(): stop() drains any buffered audio
            # to the device before returning, which on track-change
            # costs 400-900ms of nothing-is-happening. abort()
            # discards the pending buffer immediately and we start
            # the next track faster. Any in-flight tail audio just
            # gets cut off, which is what the user already expects
            # when clicking a new track.
            t_abort0 = time.monotonic()
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            print(
                f"[perf] teardown stream.abort+close="
                f"{(time.monotonic() - t_abort0) * 1000.0:.0f}ms",
                file=sys.stderr,
                flush=True,
            )

        thread = self._decoder_thread
        self._decoder_thread = None
        if thread is not None and thread.is_alive():
            t_join0 = time.monotonic()
            thread.join(timeout=2.0)
            print(
                f"[perf] teardown thread.join="
                f"{(time.monotonic() - t_join0) * 1000.0:.0f}ms",
                file=sys.stderr,
                flush=True,
            )

        self._decoder = None
        if decoder is not None:
            try:
                decoder.close()
            except Exception:
                pass

        with self._lock:
            self._drain_queue_locked()
            self._callback_carry = None
            self._samples_emitted = 0
            self._current_track_id = None
            self._current_duration_ms = 0
            self._current_stream_info = None
            self._stream_sample_rate = None
            self._source_urls = None
            self._source_path = None
            self._paused = False
            self._seeking = False

    def _resolve_source(
        self, track_id: str, quality: Optional[str]
    ) -> tuple[Union[str, list[str]], Optional[float], StreamInfo, dict[int, bytes]]:
        """Resolve a track id to a source spec.

        Returns either a local filesystem path (str) or a DASH URL
        list (list[str] — index 0 is the init segment). The caller
        constructs a `Decoder` from it via `_build_source()` and
        stores the spec on the player so seek can rebuild.

        The fourth return value is a map of pre-fetched segment
        bytes (idx -> bytes). Empty for local files and for stream
        sources that have not had byte-level prefetch run against
        them. When populated, SegmentReader seeds its buffer with
        those bytes so decoder_init does not touch the network.
        """
        if self._local_lookup is not None:
            local_path = self._local_lookup(track_id)
            if local_path and os.path.exists(local_path):
                info = _probe_local_stream_info(local_path)
                return (local_path, None, info or StreamInfo(source="local"), {})

        session = self._session_getter()
        if quality and self._quality_clamp is not None:
            try:
                clamped = self._quality_clamp(quality)
                if clamped and clamped != quality:
                    log.info(
                        "clamped streaming quality %r -> %r", quality, clamped
                    )
                    quality = clamped
            except Exception:
                pass

        # Cache hit path: skip the three serial Tidal round-trips
        # (track → stream → manifest) when a recent prefetch or play
        # already resolved this (track_id, quality) pair. 3-minute
        # TTL stays well inside Tidal's signed-URL expiry window.
        cache_key = (track_id, quality)
        t0 = time.monotonic()
        cached_urls_info = self._manifest_cache.lookup(cache_key)
        if cached_urls_info is not None:
            urls, duration, info, bytes_map = cached_urls_info
            print(
                f"[perf] resolve track={track_id} quality={quality} "
                f"cache=HIT elapsed={(time.monotonic() - t0) * 1000.0:.0f}ms "
                f"prefetched_segments={len(bytes_map)}",
                file=sys.stderr,
                flush=True,
            )
            return (list(urls), duration, info, bytes_map)

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
            # Per-phase timings so we can see which of the three
            # Tidal round-trips is the slow one on any given play.
            t_track = time.monotonic()
            track = session.track(int(track_id))
            t_stream = time.monotonic()
            stream = track.get_stream()
            t_manifest = time.monotonic()
            manifest = stream.get_stream_manifest()
            t_end = time.monotonic()
            if getattr(manifest, "is_encrypted", False):
                raise RuntimeError("encrypted stream — can't decode")
            urls = list(getattr(manifest, "urls", []) or [])
            if not urls:
                raise RuntimeError("manifest has no segment urls")
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
            duration_s = float(duration) if duration else None
            self._manifest_cache.store(cache_key, list(urls), duration_s, info)
            # After store, any previously-warmed bytes are preserved
            # — pick them back up so a MISS on manifest that still
            # has bytes cached returns the bytes too.
            cached = self._manifest_cache.lookup(cache_key)
            bytes_map = cached[3] if cached is not None else {}
            print(
                f"[perf] resolve track={track_id} quality={quality} cache=MISS "
                f"total={(t_end - t0) * 1000.0:.0f}ms "
                f"track={(t_stream - t_track) * 1000.0:.0f}ms "
                f"stream={(t_manifest - t_stream) * 1000.0:.0f}ms "
                f"manifest={(t_end - t_manifest) * 1000.0:.0f}ms "
                f"segments={len(urls)}",
                file=sys.stderr,
                flush=True,
            )
            return (list(urls), duration_s, info, bytes_map)
        finally:
            if override is not None:
                session.config.quality = original

    def cache_stats(self) -> dict:
        """Snapshot of the manifest cache for the /api/player/cache-stats
        endpoint. Read while testing the prefetch path to confirm
        hovers / album-mount prefetches are landing."""
        return self._manifest_cache.stats()

    def prefetch(
        self, track_id: str, quality: Optional[str] = None, *, warm_bytes: bool = True
    ) -> bool:
        """Populate the manifest cache for a track without starting
        playback. Safe to call speculatively from hover / album-mount
        warmers. Returns True if the cache now has an entry for this
        (track_id, quality), False on any failure — callers are
        fire-and-forget so errors are swallowed.

        When warm_bytes=True (default), follows up by downloading the
        init segment plus the first media segment in parallel and
        caches those bytes too, so the next click's decoder_init
        can skip the network entirely.

        No-ops when prefetch is disabled by the caller — the endpoint
        layer bails out on offline_mode before ever reaching this
        method, so we only get here when prefetch is wanted."""
        try:
            source_spec, _dur, _info, _bytes = self._resolve_source(track_id, quality)
        except Exception as exc:
            log.debug("prefetch manifest %s@%s failed: %s", track_id, quality, exc)
            return False
        if warm_bytes and isinstance(source_spec, list):
            self._warm_bytes((track_id, quality), source_spec)
        return True

    def _warm_bytes(
        self, key: tuple[str, Optional[str]], urls: list[str]
    ) -> None:
        """Best-effort download of the init + first media segments
        in parallel. Skips segments already in the cache so repeat
        hover events don't re-download. Fire-and-forget: any failure
        leaves the cache at URL-only and the eventual play path
        falls through to the normal SegmentReader fetch."""
        cached = self._manifest_cache.lookup(key)
        have = cached[3] if cached is not None else {}
        targets = [i for i in (0, 1) if i < len(urls) and i not in have]
        if not targets:
            return
        fetched: dict[int, bytes] = {}

        def _fetch(i: int) -> Optional[tuple[int, bytes]]:
            try:
                r = requests.get(urls[i], timeout=30)
                r.raise_for_status()
                return i, r.content
            except Exception as exc:
                log.debug("prefetch bytes seg=%d failed: %s", i, exc)
                return None

        with ThreadPoolExecutor(
            max_workers=min(2, len(targets)),
            thread_name_prefix="warm-bytes",
        ) as pool:
            for result in pool.map(_fetch, targets):
                if result is not None:
                    idx, content = result
                    fetched[idx] = content
        if fetched:
            total = sum(len(v) for v in fetched.values())
            self._manifest_cache.update_bytes(key, fetched)
            print(
                f"[perf] prefetch bytes track={key[0]} quality={key[1]} "
                f"segments={sorted(fetched.keys())} "
                f"total={total}B",
                file=sys.stderr,
                flush=True,
            )

    def _open_output_stream(self, sample_rate: int, channels: int, dtype: str) -> None:
        # If the user picked a specific output device, use it;
        # otherwise pass device=None so sounddevice routes to the
        # system default.
        device: Optional[int] = None
        if self._selected_device_id:
            try:
                device = int(self._selected_device_id)
            except ValueError:
                device = None

        # Decide the rate we'll actually feed sounddevice.
        # ─ Exclusive Mode: push the source rate straight at the device.
        #   The OS or driver is asked to reconfigure to that rate, and
        #   either succeeds (bit-perfect) or fails (we fall back below).
        # ─ Shared mode: ask the device for its mixer rate and resample
        #   to that rate inside the decoder. The OS then receives audio
        #   that already matches its mixer's rate, so its own resampler
        #   is a no-op and the only resampler in the chain is ours,
        #   where we control the headroom. When the source rate already
        #   matches the device's mixer rate (e.g. 44.1 source on a 44.1
        #   device), set_target_rate is a no-op and we stay bit-perfect
        #   even in shared mode.
        decoder = self._decoder
        if decoder is not None:
            if self._exclusive_mode:
                target_rate = decoder.sample_rate
            else:
                target_rate = self._query_device_mixer_rate(device, decoder.sample_rate)
            decoder.set_target_rate(target_rate)
            sample_rate = decoder.output_sample_rate
            dtype = decoder.sounddevice_dtype
            channels = decoder.channels

        # Exclusive Mode — push PCM straight at the device at its
        # native rate / bit depth. On macOS the CoreAudio flags ask
        # the driver to reconfigure the device and fail loudly
        # instead of silently resampling. On Windows we open WASAPI
        # exclusive. On other platforms the flag is a no-op.
        extra_settings = None
        if self._exclusive_mode:
            try:
                if sys.platform == "darwin":
                    extra_settings = sd.CoreAudioSettings(
                        change_device_parameters=True,
                        fail_if_conversion_required=True,
                    )
                elif sys.platform.startswith("win"):
                    extra_settings = sd.WasapiSettings(exclusive=True)
            except AttributeError:
                # Older sounddevice / PortAudio without these helpers.
                # Fall through to the shared-mode stream so the user
                # still gets audio, just without the exclusive guarantee.
                extra_settings = None

        stream_kwargs: dict = dict(
            samplerate=sample_rate,
            channels=channels,
            dtype=dtype,
            device=device,
            callback=self._audio_callback,
            finished_callback=self._on_stream_finished,
        )
        if extra_settings is not None:
            stream_kwargs["extra_settings"] = extra_settings

        try:
            self._stream = sd.OutputStream(**stream_kwargs)
        except Exception:
            # Exclusive Mode can fail on devices that refuse the
            # requested rate / format (e.g. a USB DAC pinned to 48k
            # when the track is 44.1k). Fall back to shared mode so
            # playback keeps working. We also need to switch the
            # decoder to mixer-rate output for the same intersample-
            # peak reason as above, since the fallback puts us in
            # shared mode.
            if extra_settings is not None:
                log.warning(
                    "Exclusive-mode stream open failed; falling back to shared"
                )
                stream_kwargs.pop("extra_settings", None)
                if decoder is not None:
                    fallback_rate = self._query_device_mixer_rate(
                        device, decoder.sample_rate
                    )
                    decoder.set_target_rate(fallback_rate)
                    stream_kwargs["samplerate"] = decoder.output_sample_rate
                    stream_kwargs["dtype"] = decoder.sounddevice_dtype
                    sample_rate = decoder.output_sample_rate
                    channels = decoder.channels
                self._stream = sd.OutputStream(**stream_kwargs)
            else:
                raise

        # Pin the cached stream-state to whatever we actually opened
        # at. set_exclusive_mode and set_output_device read these to
        # decide gapless compatibility, so the values must reflect
        # the post-reconfig rate, not the source rate. Doing this
        # here (vs. at every callsite) keeps the rule in one place.
        self._stream_sample_rate = sample_rate
        self._stream_sd_dtype = dtype
        self._stream_channels = channels

        # Diagnostic line for the audio open. Shows the exact rate /
        # depth / channel count / host API / exclusive flag we ended
        # up using. Critical for triaging Windows-side audio bugs
        # where a stutter or freeze typically traces back to a rate
        # mismatch between the decoder and the device, or to
        # exclusive-mode being on for a device that only supports a
        # subset of the rates we're feeding. Printed unconditionally
        # so it lands in the user's console log without needing a
        # debug flag — the volume is one line per stream open.
        try:
            dev_info = sd.query_devices(device, kind="output")
            ha = sd.query_hostapis(dev_info.get("hostapi"))
            ha_name = ha.get("name") if isinstance(ha, dict) else "?"
        except Exception:
            ha_name = "?"
        print(
            f"[audio] stream open: device={device} rate={sample_rate}Hz "
            f"channels={channels} dtype={dtype!r} hostapi={ha_name!r} "
            f"exclusive={self._exclusive_mode}",
            file=sys.stderr,
            flush=True,
        )

        # Rebuild the EQ against the new sample rate. Preserves
        # whichever mode is active — manual bands or AutoEQ profile.
        self._eq = Equalizer(sample_rate=sample_rate, channels=channels)
        if self._eq_profile is not None:
            try:
                from app.audio.autoeq.apply import profile_to_sos

                sos = profile_to_sos(self._eq_profile, sample_rate)
                if sos.size == 0:
                    self._eq.clear()
                else:
                    self._eq.set_sos(
                        sos, preamp_db=self._eq_profile.preamp_db
                    )
            except Exception:
                log.exception("autoeq profile coefficient build failed")
                self._eq.clear()
        elif self._eq_bands:
            try:
                self._eq.set_bands(self._eq_bands, preamp_db=self._eq_preamp)
            except Exception:
                log.exception("eq coefficient build failed")
                self._eq.clear()

    @staticmethod
    def _query_device_mixer_rate(device: Optional[int], fallback: int) -> int:
        """Look up the rate the OS will mix at for `device` in shared
        mode. If sounddevice can't tell us, return `fallback` so the
        decoder stays at source rate (the previous behavior). Rounded
        to int because PortAudio reports it as a float."""
        try:
            info = sd.query_devices(device, kind="output")
        except Exception:
            log.warning("query_devices failed; using source rate as fallback")
            return fallback
        rate = info.get("default_samplerate") if isinstance(info, dict) else None
        if not rate or rate <= 0:
            return fallback
        return int(round(rate))

    def _start_decoder_thread(self) -> None:
        # Bind the decoder / queue / flags to the thread's locals at
        # creation time. The loop must NOT read self.* fields — a
        # concurrent load() may rebind those, which previously caused
        # two decoder threads to share one PyAV generator (hence
        # "generator already executing" errors under rapid back-to-
        # back play_track calls).
        decoder = self._decoder
        pcm_queue = self._pcm_queue
        stop_flag = self._stop_flag
        done_event = self._decoder_done
        t = threading.Thread(
            target=self._decoder_loop,
            args=(decoder, pcm_queue, stop_flag, done_event),
            name="pcm-decoder",
            daemon=True,
        )
        self._decoder_thread = t
        t.start()

    @staticmethod
    def _decoder_loop(
        decoder: Optional[Decoder],
        pcm_queue: "queue.Queue[Optional[np.ndarray]]",
        stop_flag: threading.Event,
        done_event: threading.Event,
    ) -> None:
        if decoder is None:
            done_event.set()
            return
        try:
            while not stop_flag.is_set():
                pcm = decoder.next_pcm()
                if pcm is None:
                    break
                while not stop_flag.is_set():
                    try:
                        pcm_queue.put(pcm, timeout=0.5)
                        break
                    except queue.Full:
                        continue
        except Exception:
            log.exception("decoder thread crashed")
        finally:
            done_event.set()
            # Push the sentinel onto the thread's OWN queue, not
            # whatever self._pcm_queue happens to point at now. If
            # a gapless swap rebound self._pcm_queue to a different
            # queue mid-loop, pushing the sentinel there would have
            # incorrectly flagged the new decoder as done.
            try:
                pcm_queue.put_nowait(None)
            except queue.Full:
                pass

    def _log_callback_status(self, status) -> None:
        """Rate-limited diagnostic for PortAudio over/underruns
        signalled via the callback's `status` arg. One message + a
        running count per second; without rate limiting a sustained
        glitch at 86 Hz callback cadence would saturate stderr.
        Critical diagnostic on Windows where stutter complaints
        typically trace back to this path."""
        now = time.monotonic()
        self._cb_status_count += 1
        if now - self._cb_status_last_print >= 1.0:
            count = self._cb_status_count
            self._cb_status_count = 0
            self._cb_status_last_print = now
            print(
                f"[audio] callback status={status} "
                f"(count_since_last={count})",
                file=sys.stderr,
                flush=True,
            )

    def _log_callback_heartbeat(self) -> None:
        """DEBUG-only per-100-callback heartbeat. Confirms the
        callback is running + advancing samples and shows the
        queue depth so a "decoder is filling but callback isn't
        draining" condition is visible at a glance."""
        self._callback_counter += 1
        if self._callback_counter % 100 == 0:
            qsize = self._pcm_queue.qsize()
            print(
                f"[pcm] callback tick #{self._callback_counter} "
                f"samples_emitted={self._samples_emitted} "
                f"queue={qsize}/{_PCM_QUEUE_MAX} "
                f"state={self._state} "
                f"track={self._current_track_id}",
                file=sys.stderr, flush=True,
            )

    def _log_callback_starvation(self) -> None:
        """Rate-limited diagnostic for our-side underruns — the
        decoder didn't push a chunk in time. Distinct from
        `_log_callback_status` because it pinpoints the cause as
        ours (slow disk / network / CPU) rather than driver-side."""
        now = time.monotonic()
        self._cb_starve_count += 1
        if now - self._cb_starve_last_print >= 1.0:
            count = self._cb_starve_count
            self._cb_starve_count = 0
            self._cb_starve_last_print = now
            print(
                f"[audio] queue starvation: decoder behind "
                f"the callback (count_since_last={count})",
                file=sys.stderr,
                flush=True,
            )

    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self._log_callback_status(status)
        if PCMPlayer._DEBUG:
            self._log_callback_heartbeat()

        # Paused → output silence, don't drain queue, don't advance
        # position. Zero-latency resume: on unpause the next call
        # finds the queue exactly where it was.
        if self._paused:
            outdata.fill(0)
            return

        # Seeking → also output silence. Without this, the brief
        # window where the old decoder has exited and the new one
        # hasn't started would let the callback raise CallbackStop
        # and end the stream mid-seek.
        if self._seeking:
            outdata.fill(0)
            return

        written = 0
        channels = outdata.shape[1]

        while written < frames:
            if self._callback_carry is None:
                try:
                    chunk = self._pcm_queue.get_nowait()
                except queue.Empty:
                    chunk = None
                if chunk is None:
                    if self._decoder_done.is_set():
                        # Primary track exhausted. If a compatible
                        # preload is waiting, splice it into the
                        # SAME OutputStream — that's the gapless
                        # moment. Re-enter the loop with the new
                        # queue.
                        if self._try_gapless_swap():
                            continue
                        outdata[written:] = 0
                        raise sd.CallbackStop
                    # Underrun — decoder hasn't caught up. Silence
                    # the rest of this callback. Critically we still
                    # advance samples_emitted below so the scrubber
                    # keeps moving through the underrun window —
                    # otherwise position would freeze on every
                    # hiccup.
                    self._log_callback_starvation()
                    outdata[written:] = 0
                    self._samples_emitted += frames
                    return
                self._callback_carry = chunk

            take = min(frames - written, self._callback_carry.shape[0])
            src = self._callback_carry[:take]
            if src.shape[1] == channels:
                outdata[written : written + take] = src
            elif src.shape[1] == 2 and channels == 1:
                outdata[written : written + take, 0] = src.mean(axis=1)
            else:
                outdata[written : written + take] = 0
            written += take
            if take >= self._callback_carry.shape[0]:
                self._callback_carry = None
            else:
                self._callback_carry = self._callback_carry[take:]

        # AirPlay tap. When an AirPlay session is active, push a
        # copy of the raw decoded PCM to the AirPlay pipe BEFORE
        # EQ / volume / mute run. The AirPlay receiver has its own
        # volume control, so sending pre-volume audio keeps local
        # mute from silencing the remote speaker. Import is lazy to
        # avoid loading pyatv's import tree on machines that never
        # use AirPlay, and to sidestep a circular-import risk
        # between app.audio and server startup.
        try:
            from app.audio import airplay as _airplay_mod
            if _airplay_mod.AirPlayManager.is_available():
                mgr = _airplay_mod.AirPlayManager.instance()
                if mgr.is_connected():
                    # np.ascontiguousarray is a no-op when outdata is
                    # already contiguous (which it always is coming
                    # from sounddevice), so no copy cost on the hot
                    # path. The encoder runs off-thread and is
                    # tolerant of dropped chunks.
                    mgr.push_pcm(np.ascontiguousarray(outdata))
        except Exception:
            # Never let AirPlay errors take down local playback.
            pass

        # Cast tap. Same pre-EQ / pre-volume position as AirPlay —
        # the Cast device has its own volume control, so muting
        # locally shouldn't silence the remote speaker. The
        # is_active() probe is lock-free in the common 'no
        # session' case, so the cost when nobody's casting is one
        # attribute read per audio callback. Tidal sources decode
        # to int16 or int32, but on Windows WASAPI shared mode the
        # OutputStream comes out as float32 (device mixer format),
        # so we hand all three through; cast_manager.push_pcm
        # converts float32 to int32 internally for the FLAC encoder.
        try:
            from app.audio import cast as _cast_mod
            if _cast_mod.cast_manager.is_active():
                if outdata.dtype == np.int16:
                    _dtype_name = "int16"
                elif outdata.dtype == np.int32:
                    _dtype_name = "int32"
                elif outdata.dtype == np.float32:
                    _dtype_name = "float32"
                else:
                    _dtype_name = None
                if _dtype_name is not None:
                    _cast_mod.cast_manager.push_pcm(
                        np.ascontiguousarray(outdata),
                        sample_rate=self._stream_sample_rate or 44100,
                        dtype=_dtype_name,
                    )
        except Exception:
            # Never let Cast errors take down local playback.
            pass

        # External output active: silence local. Done AFTER the
        # AirPlay / Cast / Tidal Connect taps above (so the remote
        # receiver gets full-amplitude audio at its own volume
        # control) and BEFORE the volume / mute logic below (so the
        # silencing is unconditional regardless of user volume
        # state). The OutputStream still runs — we just hand it
        # zeros — which keeps the realtime callback driving and
        # avoids the underrun-recovery dance that stopping +
        # restarting would cost when the user toggles back to
        # local.
        if self._external_output_active:
            outdata.fill(0)
            self._samples_emitted += frames
            # Bump seq so the frontend's position scrubber still
            # advances even though local is silent — playback is
            # still happening, just on the remote.
            self._seq_bump_counter += 1
            if self._seq_bump_counter >= 20:
                self._seq += 1
                self._seq_bump_counter = 0
            return

        # EQ (10-band biquad). Active only when the user has a
        # non-flat curve set — when disabled, `apply()` is an early
        # return and doesn't touch `outdata`, so bit-perfect
        # pass-through is preserved at flat EQ + full volume + not
        # muted. Filtering requires float32; we round-trip through
        # float32 when the output dtype is int16/int32.
        if (
            self._eq is not None
            and self._eq.is_active()
            and not self._eq_bypass
        ):
            if outdata.dtype == np.float32:
                self._eq.apply(outdata)
            else:
                # Scale to float32 in the range [-1, 1], filter,
                # scale back. int range constants chosen so the
                # round-trip is exact for unmodified samples.
                scale_in = (
                    32768.0 if outdata.dtype == np.int16 else 2_147_483_648.0
                )
                buf = outdata.astype(np.float32, copy=True) / scale_in
                self._eq.apply(buf)
                np.clip(buf * scale_in, -scale_in, scale_in - 1.0, out=buf)
                # np.rint rounds to nearest (banker's); plain astype
                # would truncate toward zero and bias samples slightly
                # negative on average.
                outdata[:] = np.rint(buf).astype(outdata.dtype)

        # Volume + mute post-processing. At volume=100 and not muted
        # (and no EQ above), bit-perfect pass-through still holds when
        # the decoder isn't doing internal SRC; when it is, the
        # decoder has already attenuated by ~1 dB upstream so peaks
        # land below full scale.
        if self._muted or self._volume <= 0:
            outdata.fill(0)
        elif self._volume < 100:
            vol = self._volume / 100.0
            if outdata.dtype == np.float32:
                outdata *= vol
            else:
                # int16 / int32: scale via float roundtrip. The float
                # multiplier is safe for a 32-bit int dtype because
                # we clamp in astype().
                scaled = outdata.astype(np.float32, copy=False) * vol
                outdata[:] = scaled.astype(outdata.dtype)

        self._samples_emitted += frames
        # Bump seq so the frontend's SSE dedupe lets through the
        # position update. The callback fires ~90Hz at 44.1k /512
        # frames, so ~every 20th call keeps us close to 4-5Hz —
        # smooth for a scrubber, cheap on CPU.
        self._seq_bump_counter += 1
        if self._seq_bump_counter >= 20:
            self._seq += 1
            self._seq_bump_counter = 0

    def _adopt_preload_locked(self, pre: _Preload) -> PlayerSnapshot:
        """Synchronous version of the callback's gapless swap,
        triggered from load() when the frontend fires play_track
        for a track we already have preloaded.

        Must be called with `self._lock` held. Stops the current
        decoder thread, swaps refs to the preload's pipeline, and
        either keeps the OutputStream alive (same rate/dtype —
        gapless) or re-opens it (different rate — small reopen
        gap). Does not tear the preload's decoder thread: it keeps
        running against its own queue, which now becomes the
        primary queue.
        """
        # Capture pieces we need to clean up OUTSIDE the lock so we
        # don't hold it during slow I/O (thread.join, decoder.close).
        old_thread = self._decoder_thread
        old_stop_flag = self._stop_flag
        old_decoder = self._decoder
        rate_matches = (
            pre.sample_rate == self._stream_sample_rate
            and pre.sd_dtype == self._stream_sd_dtype
            and pre.channels == self._stream_channels
        )
        old_stream = None if rate_matches else self._stream

        # Adopt the preload's pipeline. Callback is free to read
        # from the new queue the instant after these assignments.
        # `_swap_pipeline_to` writes rate/dtype/channels
        # unconditionally; that's a no-op in the same-rate case
        # because the values already match the active stream.
        self._swap_pipeline_to(pre)
        self._preload = None
        self._state = "playing"
        self._last_error = None
        self._seq += 1

        # Outside the lock (well — RLock held; cleanup stays
        # short): signal + close the old pipeline's resources.
        old_stop_flag.set()
        # Cancel the old decoder's source BEFORE joining. Same
        # reasoning as _teardown / _restart_decoder_at: stop_flag
        # only gets checked between PCM frames, and the old thread
        # is very often mid-HTTP-fetch on a track change. Closing
        # the source aborts the in-flight read so the join returns
        # in milliseconds instead of waiting the segment out.
        if old_decoder is not None:
            try:
                old_decoder.cancel_source()
            except Exception:
                pass
        if old_thread is not None and old_thread.is_alive():
            # Short bounded join: gives the thread a chance to
            # notice the stop flag and release its PyAV container
            # + SegmentReader session. If it's blocked in network
            # I/O past the timeout we give up (it's a daemon and
            # won't prevent process exit), but we've tried rather
            # than dropping the ref and guaranteeing a ~30s leak.
            old_thread.join(timeout=0.5)
        if old_decoder is not None:
            try:
                old_decoder.close()
            except Exception:
                pass
        if old_stream is not None:
            # abort() over stop() — same reason as _teardown above:
            # stop() drains the buffer and stalls the cross-rate
            # bridge by half a second. Any tail audio we discard
            # is about to be drowned by the new stream anyway.
            try:
                old_stream.abort()
            except Exception:
                pass
            try:
                old_stream.close()
            except Exception:
                pass
            # Re-open at the new rate.
            self._open_output_stream(
                pre.sample_rate, pre.channels, pre.sd_dtype
            )
            try:
                self._stream.start()
            except Exception as exc:
                log.exception("adopt_preload: stream.start failed")
                self._last_error = str(exc)
                self._state = "error"
                self._seq += 1

        self._emit()
        return self.snapshot()

    def _try_gapless_swap(self) -> bool:
        """Called from the audio callback when the current queue is
        empty + primary decoder is done. Swaps in the preloaded
        track's decoder/queue/thread if the sample rate + dtype +
        channel count match. Returns True on success; False if
        there's no preload or it's incompatible.

        Emits `ended` BEFORE the swap and `playing` after. The
        frontend's advance logic fires on `ended`, updates its
        `expectedTrackIdRef` via `playAtIndex`, and the redundant
        `play_track(next)` that follows lands on Path 0 (no-op
        because current_track_id already matches). Without the
        `ended` emit, the frontend's expected-guard would drop all
        new snapshots because its expected is still the previous
        track.

        Assignments are individually atomic under the GIL; the
        callback is the sole modifier during the track-boundary
        moment.
        """
        # Atomically capture + clear the preload slot so a racing
        # `_drop_preload` on the HTTP thread can't close the
        # preload's decoder between our capability check below and
        # the ref swap. The lock is held only for the pointer grab
        # (microseconds) — not during the actual swap or emit —
        # to keep realtime-callback jitter minimal.
        with self._lock:
            pre = self._preload
            if pre is None:
                return False
            if (
                pre.sample_rate != self._stream_sample_rate
                or pre.sd_dtype != self._stream_sd_dtype
                or pre.channels != self._stream_channels
            ):
                # Incompatible — fall through to CallbackStop and
                # let _on_stream_finished spawn the bridge thread.
                return False
            self._preload = None
        log.info(
            "gapless swap (inline) -> track=%s rate=%dHz dtype=%s",
            pre.track_id, pre.sample_rate, pre.sd_dtype,
        )
        # Phase 1: emit `ended` so the frontend's advance logic
        # wires up expectedTrackIdRef for the new track.
        self._state = "ended"
        self._seq += 1
        self._emit()
        # Phase 2: actual swap. Old decoder + thread have already
        # finished (done event is set), we just replace refs.
        self._swap_pipeline_to(pre)
        self._state = "playing"
        self._seq += 1
        self._emit()
        return True

    def _on_stream_finished(self) -> None:
        # Called by sounddevice when the stream ends. Three reasons it
        # might fire:
        #   1. Natural EOF — callback raised CallbackStop because the
        #      decoder finished and the queue drained.
        #   2. Intentional stream replacement (set_output_device,
        #      set_exclusive_mode, cross-rate bridge) — `_replacing_
        #      stream` is set; ignore this firing entirely, the
        #      replacement code is opening a new stream right now.
        #   3. Device loss — headphones unplugged, USB DAC pulled,
        #      Bluetooth out of range. PortAudio aborts the stream
        #      because the underlying CoreAudio device is gone, but
        #      our decoder still has frames buffered. Recover by
        #      reopening on the system default so playback continues
        #      on speakers without pausing — that's how every other
        #      streaming app behaves and what users expect.
        #
        # Capture AND clear the preload pointer under the same lock
        # acquisition so a concurrent _drop_preload on the HTTP
        # thread (e.g. if the user also clicked Stop at the same
        # microsecond as natural EOF) cannot close the preload's
        # decoder between our read of self._preload and the bridge
        # thread's use of it. We "own" the preload for the duration
        # of the bridge attempt; if the bridge fails, it's
        # responsible for disposing of the decoder itself.
        with self._lock:
            if self._state in ("idle", "error"):
                return
            if self._replacing_stream:
                # Intentional stream swap — see set_output_device /
                # set_exclusive_mode. Don't transition state; don't
                # trigger device-loss recovery.
                return
            # Device-loss heuristic: stream ended unexpectedly, with
            # work still pending. Two ways to know the work isn't done:
            #
            #   1. Decoder hasn't finished decoding the rest of the
            #      track (`not decoder_done`).
            #   2. Decoder IS done but the PCM queue still has chunks
            #      the callback never got to render (`qsize > 0`). This
            #      is the case when the device disappears late in a
            #      short track — the decoder finished filling the queue
            #      before the unplug, so `decoder_done` is True even
            #      though several seconds of audio are still waiting to
            #      play.
            #
            # Natural EOF, by contrast, has both the decoder done AND
            # the queue drained — the callback got everything out
            # before raising CallbackStop.
            decoder_done = self._decoder_done.is_set()
            queue_pending = (
                self._pcm_queue.qsize() > 0
                if self._pcm_queue is not None
                else False
            )
            was_playing = self._state == "playing"
            pre = self._preload
            should_recover = was_playing and pre is None and (
                not decoder_done or queue_pending
            )
            if should_recover:
                log.info(
                    "device-loss detected: triggering recovery "
                    "(decoder_done=%s, queue_pending=%s)",
                    decoder_done, queue_pending,
                )
                # Recovery runs off-thread because the finished_callback
                # runs on sounddevice's own thread which we shouldn't
                # re-enter stream machinery from.
                threading.Thread(
                    target=self._recover_from_device_loss,
                    name="pcm-device-recovery",
                    daemon=True,
                ).start()
                return
            self._preload = None

        # Cross-rate bridge: preload exists but has a different
        # sample rate / dtype than the current stream, so the
        # callback-level gapless swap was skipped. Re-open the
        # OutputStream with the preload's params. Off-thread
        # because sounddevice's finished_callback runs on its
        # own thread which shouldn't re-enter stream machinery.
        if pre is not None:
            threading.Thread(
                target=self._bridge_to_preload,
                args=(pre,),
                name="pcm-bridge",
                daemon=True,
            ).start()
            return

        with self._lock:
            self._transition("ended")
        self._emit()

    def _recover_from_device_loss(self) -> None:
        """Re-bind the output stream to a working device after a
        CoreAudio routing change. Two scenarios this covers:

          A. The currently-selected device disappeared (headphones
             unplugged, USB DAC removed, AirPods drifted out of
             range). The reopen on the original device fails; we
             fall back to the system default.

          B. The device list changed but the selected device is
             still around (a NEW device just plugged in — e.g.
             headphones reconnected while audio is on speakers).
             The reopen on the original device succeeds; the user's
             selection is preserved AND PortAudio's enumeration
             gets refreshed in the same step so the picker shows
             the new device next time it opens.

        Standard streaming-app behavior: unplug headphones, audio
        keeps playing on speakers; plug them back in, the picker
        sees them again. ~50 ms of silence while CoreAudio
        renegotiates, same as a manual device switch.

        The decoder thread keeps feeding the PCM queue throughout,
        so once we open a fresh stream the first callback drains
        the queued frames and audio resumes seamlessly.
        """
        with self._pipeline_lock:
            with self._lock:
                # Bail if state moved on — user pressed stop, a
                # natural EOF arrived, error path fired, etc.
                if self._state in ("idle", "error", "ended"):
                    return
                if self._stream is None:
                    return
                sample_rate = self._stream_sample_rate
                channels = self._stream_channels
                sd_dtype = self._stream_sd_dtype
                old_stream = self._stream
                self._stream = None
                self._callback_carry = None
                # KEEP the user's selection. We try it first below;
                # only fall back to default if the reopen fails
                # (which is what means "the device actually
                # disappeared" vs "device list just changed").
                original_device_id = self._selected_device_id
                self._replacing_stream = True

            if (
                sample_rate is None
                or channels is None
                or sd_dtype is None
            ):
                with self._lock:
                    self._replacing_stream = False
                return

            try:
                old_stream.close()
            except Exception:
                pass

            # Force PortAudio to re-enumerate CoreAudio. Without
            # this, devices that plugged in since the last init
            # are invisible to PortAudio even though CoreAudio
            # sees them.
            try:
                sd._terminate()
            except Exception:
                pass
            try:
                sd._initialize()
            except Exception:
                log.exception("device-recovery: PortAudio reinit failed")

            # First attempt: original device. Succeeds in the
            # "device list changed but my pick is still around"
            # case (scenario B above).
            try:
                with self._lock:
                    self._open_output_stream(
                        sample_rate, channels, sd_dtype
                    )
                self._stream.start()
                log.info(
                    "device-recovery: reopened on original device "
                    "id=%r (rate=%d, channels=%d)",
                    original_device_id, sample_rate, channels,
                )
                with self._lock:
                    self._replacing_stream = False
                return
            except Exception as exc:
                log.info(
                    "device-recovery: original device id=%r "
                    "unavailable (%r); falling back to system default",
                    original_device_id, exc,
                )
                # Reset state for the second attempt.
                with self._lock:
                    self._selected_device_id = ""
                    self._stream = None
                    self._callback_carry = None

            # Second attempt: system default (scenario A: device
            # actually disappeared).
            try:
                with self._lock:
                    self._open_output_stream(
                        sample_rate, channels, sd_dtype
                    )
                self._stream.start()
                log.info(
                    "device-recovery: reopened on system default "
                    "(rate=%d, channels=%d)",
                    sample_rate, channels,
                )
            except Exception:
                log.exception(
                    "device-recovery: system-default fallback also failed"
                )
                with self._lock:
                    self._transition("error")
                    self._stream = None
                self._emit()
            finally:
                with self._lock:
                    self._replacing_stream = False

    def _bridge_to_preload(self, pre: _Preload) -> None:
        """Stream re-open + swap for a cross-rate preload. Produces
        ~50ms of silence between tracks — same limitation every
        gapless player has when sample rates change.

        Serialized on `_pipeline_lock` so a concurrent `stop()` /
        `load()` on the HTTP thread can't run teardown while we're
        mid-bridge. If teardown drove us to idle before we acquired
        the lock, the preload we captured may have been dropped —
        bail out and let the user-initiated state stand.
        """
        with self._pipeline_lock:
            with self._lock:
                if self._state == "idle":
                    # Teardown ran between _on_stream_finished and
                    # here. Our captured `pre` may have been closed
                    # by _drop_preload; don't try to adopt it.
                    try:
                        pre.stop_flag.set()
                        pre.decoder.close()
                    except Exception:
                        pass
                    return

            # Mirror `_try_gapless_swap`'s pattern: emit `ended` for
            # the OUTGOING track BEFORE swapping. The frontend's
            # advance logic keys off this snapshot to wire its
            # `expectedTrackIdRef` up to the new track id; without
            # the emit, every subsequent snapshot for the new track
            # fails the late-echo guard and the now-playing bar gets
            # stuck on the previous track even though audio has
            # moved on. Same-rate transitions don't hit this path
            # (they go through `_try_gapless_swap` which already
            # emits `ended`), so the bug only surfaced on cross-rate
            # transitions — typically album → Artist Radio where
            # the radio tracks have a different sample rate than
            # the album that just finished.
            with self._lock:
                self._state = "ended"
                self._seq += 1
            self._emit()

            log.info(
                "gapless bridge (cross-rate) -> track=%s rate %d->%dHz dtype %s->%s",
                pre.track_id,
                self._stream_sample_rate or 0,
                pre.sample_rate,
                self._stream_sd_dtype,
                pre.sd_dtype,
            )
            # The old stream already fired finished_callback, so it's
            # stopped. Close it for good measure and release
            # resources.
            old_stream = self._stream
            self._stream = None
            if old_stream is not None:
                try:
                    old_stream.close()
                except Exception:
                    pass
            # Close the old decoder (thread has already exited since
            # decoder_done fired).
            old_decoder = self._decoder
            if old_decoder is not None:
                try:
                    old_decoder.close()
                except Exception:
                    pass

            with self._lock:
                # Clear the preload slot BEFORE opening a new
                # stream so a racing preload() call (unlikely this
                # early) can't re-stomp it.
                self._preload = None
                self._swap_pipeline_to(pre)
                self._open_output_stream(
                    pre.sample_rate, pre.channels, pre.sd_dtype
                )
                self._transition("playing")
            try:
                self._stream.start()
            except Exception:
                log.exception("bridge: stream.start failed")
                with self._lock:
                    self._last_error = "stream reopen failed"
                    self._transition("error")
                self._emit()
                return
            self._emit()

    def _drain_queue_locked(self) -> None:
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                break

    def _transition(self, state: str, track_id: Optional[str] = None) -> None:
        self._state = state
        if track_id is not None:
            self._current_track_id = track_id
        self._seq += 1

    def _emit(self) -> None:
        snap = self.snapshot()
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(snap)
            except Exception:
                log.exception("listener raised")

    def _swap_pipeline_to(self, pre: "_Preload") -> None:
        """Replace the active pipeline refs with those from `pre`.

        Single source of truth for the field copies that drive a
        track swap. Same shape was hand-rolled in three places
        (_try_gapless_swap, _bridge_to_preload, _adopt_preload_locked)
        and inevitably drifted — the cross-rate gapless bug we just
        shipped a fix for came from one of those copies forgetting a
        partner emit. Centralising it here means a new field on
        `_Preload` only has to be wired up once.

        Atomicity: each assignment is one bytecode op under the GIL,
        but the *sequence* of swaps is not atomic. Callers must
        ensure the audio callback can't observe a half-swapped
        state. The three current callers each handle this:

          - `_try_gapless_swap` runs IN the callback; the callback
            is the sole modifier at the track-boundary moment.
          - `_bridge_to_preload` runs after `finished_callback`
            fired, so the callback isn't running on the old stream.
          - `_adopt_preload_locked` holds `_lock` and (for
            cross-rate) closes the old stream before calling.

        Always copies the rate / dtype / channels triple along with
        the rest. Same-rate swaps no-op those (the values match the
        active stream); the alternative — conditional copies — was
        the contract drift this helper exists to prevent.
        """
        self._decoder = pre.decoder
        self._pcm_queue = pre.queue
        self._decoder_thread = pre.thread
        self._stop_flag = pre.stop_flag
        self._decoder_done = pre.done
        self._current_track_id = pre.track_id
        self._current_duration_ms = pre.duration_ms
        self._current_stream_info = pre.stream_info
        self._source_urls = pre.source_urls
        self._source_path = pre.source_path
        self._stream_sample_rate = pre.sample_rate
        self._stream_sd_dtype = pre.sd_dtype
        self._stream_channels = pre.channels
        self._samples_emitted = 0
        self._callback_carry = None


# ---------------------------------------------------------------------------
# Stream-info helpers (mutagen probe for local files)
# ---------------------------------------------------------------------------


def _safe_int(v: object) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _normalize_codec(raw: object) -> Optional[str]:
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


def _build_source(
    spec: Union[str, list[str]],
    prefetched: Optional[dict[int, bytes]] = None,
) -> Union[str, SegmentReader]:
    """Materialize a source spec into something Decoder can open.
    If the caller already fetched the first few segments via the
    byte-level prefetch path, hand them to SegmentReader so it
    skips the corresponding network fetches."""
    if isinstance(spec, str):
        return spec
    return SegmentReader(spec, prefetched=prefetched)


def _probe_local_stream_info(path: str) -> Optional[StreamInfo]:
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
    mime_list = getattr(info, "mime", None) or []
    if mime_list:
        codec = _normalize_codec(mime_list[0])
    if codec is None:
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        codec = _normalize_codec(ext)
    return StreamInfo(
        source="local",
        codec=codec,
        bit_depth=_safe_int(getattr(info, "bits_per_sample", None)),
        sample_rate_hz=_safe_int(getattr(info, "sample_rate", None)),
    )
