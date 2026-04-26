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
from app.audio.segment_reader import SegmentReader

log = logging.getLogger(__name__)


# Back-pressure between the decoder and the audio callback. Each
# item is one AudioFrame's worth of PCM (~1024 samples typical).
# 30 chunks ~= 0.7s at 44.1k — enough to ride out a slow segment
# fetch without starving the output.
_PCM_QUEUE_MAX = 30


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

        # Stream-manifest cache. Keyed by (track_id, quality_or_None);
        # value is (urls, duration_s, stream_info, cached_at_monotonic,
        # bytes_map). The bytes_map holds optional pre-downloaded
        # segment bytes (idx -> bytes) so byte-level prefetch can
        # skip the network entirely when the user clicks play on a
        # warmed track. Empty dict = URLs-only warm (manifest only,
        # no pre-fetched bytes).
        #
        # Kept short (3 min) because Tidal's signed CDN URLs expire
        # inside that window. Frontend hover / album-mount prefetch
        # writes here so the first real click is free of the
        # playbackinfo round-trip and, if bytes are warmed, of the
        # init + first-media segment fetches too.
        self._manifest_cache: dict[
            tuple[str, Optional[str]],
            tuple[list[str], Optional[float], StreamInfo, float, dict[int, bytes]],
        ] = {}
        self._manifest_cache_lock = threading.Lock()
        self._manifest_cache_ttl = 180.0
        # Hit / miss counters for the cache, surfaced by
        # /api/player/cache-stats so you can watch them while testing.
        self._manifest_cache_hits = 0
        self._manifest_cache_misses = 0
        # Rolling byte-level memory cap. Each cached bytes_map can
        # run ~1 MB at max quality (init + first media segment).
        # FIFO-evict entries when we exceed the cap so a long
        # playlist doesn't blow memory. 30 MB fits ~30 max-quality
        # tracks or hundreds at low quality.
        self._manifest_cache_bytes = 0
        self._manifest_cache_bytes_cap = 30 * 1024 * 1024

        # 10-band biquad EQ applied in the audio callback. One
        # instance owned for the lifetime of the player; when the
        # output stream rate changes (cross-rate bridge) we rebuild
        # it so the filter coefficients are recomputed against the
        # new sample rate. Remembered user settings (bands / preamp)
        # survive the rebuild.
        self._eq: Optional[Equalizer] = None
        self._eq_bands: list[float] = []
        self._eq_preamp: Optional[float] = None

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
            print(
                f"[perf] load track={track_id} "
                f"total={(t_decoder - load_t0) * 1000.0:.0f}ms "
                f"teardown={(t_teardown - load_t0) * 1000.0:.0f}ms "
                f"resolve={(t_resolved - t_teardown) * 1000.0:.0f}ms "
                f"decoder_init={(t_decoder - t_resolved) * 1000.0:.0f}ms",
                file=sys.stderr,
                flush=True,
            )
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
            self._open_output_stream(
                decoder.sample_rate, decoder.channels, decoder.sounddevice_dtype
            )
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

        self._start_decoder_thread()
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
        self._stop_flag.set()
        thread = self._decoder_thread
        self._decoder_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        old_decoder = self._decoder
        self._decoder = None
        if old_decoder is not None:
            try:
                old_decoder.close()
            except Exception:
                pass

        new_source, start_offset_s = self._build_source_at(target_s)
        new_decoder = Decoder(new_source)
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
        """
        is_flat = bands and all(abs(b) < 1e-6 for b in bands) and (
            preamp is None or abs(preamp) < 1e-6
        )
        with self._lock:
            self._eq_bands = list(bands)
            self._eq_preamp = preamp
            if self._eq is not None:
                if bands and not is_flat:
                    self._eq.set_bands(list(bands), preamp_db=preamp)
                else:
                    self._eq.clear()

    def apply_equalizer_preset(self, preset_index: int) -> list[float]:
        """Apply a preset by index, push its curve to the live EQ,
        and return the resolved band amplitudes so the frontend's
        sliders can snap to it."""
        bands = eq_preset_bands(preset_index)
        self.apply_equalizer(bands, preamp=None)
        return bands

    # --- Device selection -------------------------------------------

    def list_output_devices(self) -> list[dict]:
        """Enumerate output-capable audio devices via sounddevice.
        Returns `[{"id": "<int-as-str>", "name": "<human>"}]`, with
        an empty-id "System default" entry first.
        """
        out: list[dict] = [{"id": "", "name": "System default"}]
        try:
            devices = sd.query_devices()
        except Exception:
            log.exception("sd.query_devices failed")
            return out
        for i, d in enumerate(devices):
            if int(d.get("max_output_channels", 0) or 0) > 0:
                name = d.get("name") or f"Device {i}"
                out.append({"id": str(i), "name": name})
        return out

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
        cached_urls_info = self._cache_lookup(cache_key)
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
            self._cache_store(cache_key, list(urls), duration_s, info)
            # After _cache_store, any previously-warmed bytes are
            # preserved — pick them back up so a MISS on manifest
            # that still has bytes cached returns the bytes too.
            cached = self._cache_lookup(cache_key)
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

    def _cache_lookup(
        self, key: tuple[str, Optional[str]]
    ) -> Optional[tuple[list[str], Optional[float], StreamInfo, dict[int, bytes]]]:
        now = time.monotonic()
        with self._manifest_cache_lock:
            entry = self._manifest_cache.get(key)
            if entry is None:
                self._manifest_cache_misses += 1
                return None
            urls, duration, info, cached_at, bytes_map = entry
            if now - cached_at > self._manifest_cache_ttl:
                self._manifest_cache_bytes -= sum(len(v) for v in bytes_map.values())
                self._manifest_cache.pop(key, None)
                self._manifest_cache_misses += 1
                return None
            self._manifest_cache_hits += 1
            return list(urls), duration, info, dict(bytes_map)

    def _cache_update_bytes(
        self, key: tuple[str, Optional[str]], new_bytes: dict[int, bytes]
    ) -> None:
        """Merge pre-fetched segment bytes into an existing cache
        entry. Only stores bytes for entries we already have URLs
        for (no-op if the URLs got evicted mid-prefetch). Runs the
        FIFO eviction pass afterwards so a long prefetch queue
        can't blow past the memory cap."""
        if not new_bytes:
            return
        with self._manifest_cache_lock:
            entry = self._manifest_cache.get(key)
            if entry is None:
                return
            urls, duration, info, cached_at, bytes_map = entry
            merged = dict(bytes_map)
            added = 0
            for idx, data in new_bytes.items():
                if idx in merged:
                    continue
                merged[idx] = data
                added += len(data)
            self._manifest_cache[key] = (urls, duration, info, cached_at, merged)
            self._manifest_cache_bytes += added
            self._evict_bytes_over_cap_locked()

    def _evict_bytes_over_cap_locked(self) -> None:
        """FIFO-evict byte-level entries until we're under the cap.
        Drops bytes only, preserves the URL/manifest metadata so
        subsequent plays still skip the Tidal round-trips. Caller
        holds the cache lock."""
        if self._manifest_cache_bytes <= self._manifest_cache_bytes_cap:
            return
        # Python 3.7+ dict preserves insertion order — pop in insertion
        # order until we're back under the cap.
        for key in list(self._manifest_cache.keys()):
            urls, duration, info, cached_at, bytes_map = self._manifest_cache[key]
            if not bytes_map:
                continue
            freed = sum(len(v) for v in bytes_map.values())
            self._manifest_cache[key] = (urls, duration, info, cached_at, {})
            self._manifest_cache_bytes -= freed
            if self._manifest_cache_bytes <= self._manifest_cache_bytes_cap:
                break

    def cache_stats(self) -> dict:
        """Snapshot of the manifest cache for the /api/player/cache-stats
        endpoint. Read while testing the prefetch path to confirm
        hovers / album-mount prefetches are landing."""
        with self._manifest_cache_lock:
            now = time.monotonic()
            entries = [
                {
                    "track_id": tid,
                    "quality": q,
                    "age_ms": int((now - cached_at) * 1000.0),
                    "segments": len(urls),
                    "prefetched_segments": len(bytes_map),
                    "prefetched_bytes": sum(len(v) for v in bytes_map.values()),
                }
                for (tid, q), (urls, _dur, _info, cached_at, bytes_map) in list(
                    self._manifest_cache.items()
                )
            ]
            return {
                "hits": self._manifest_cache_hits,
                "misses": self._manifest_cache_misses,
                "ttl_seconds": int(self._manifest_cache_ttl),
                "size": len(self._manifest_cache),
                "bytes_cached": self._manifest_cache_bytes,
                "bytes_cap": self._manifest_cache_bytes_cap,
                "entries": entries,
            }

    def _cache_store(
        self,
        key: tuple[str, Optional[str]],
        urls: list[str],
        duration: Optional[float],
        info: StreamInfo,
    ) -> None:
        with self._manifest_cache_lock:
            # Preserve any pre-fetched bytes from a prior prefetch
            # for the same key so re-resolving doesn't wipe the
            # warmed segments. Caller will merge bytes in later via
            # _cache_update_bytes if a fresh prefetch is running.
            existing = self._manifest_cache.get(key)
            existing_bytes = existing[4] if existing is not None else {}
            self._manifest_cache[key] = (
                urls, duration, info, time.monotonic(), existing_bytes,
            )
            # Evict expired siblings while we have the lock; keeps
            # memory bounded without a background janitor thread.
            if len(self._manifest_cache) > 128:
                cutoff = time.monotonic() - self._manifest_cache_ttl
                stale = [k for k, v in self._manifest_cache.items() if v[3] < cutoff]
                for k in stale:
                    dropped = self._manifest_cache.pop(k, None)
                    if dropped is not None:
                        self._manifest_cache_bytes -= sum(
                            len(v) for v in dropped[4].values()
                        )

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
        cached = self._cache_lookup(key)
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
            self._cache_update_bytes(key, fetched)
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

        # Rebuild the EQ against the new sample rate. Preserves the
        # user's bands / preamp across tracks.
        self._eq = Equalizer(sample_rate=sample_rate, channels=channels)
        if self._eq_bands:
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

    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            # Under/overruns surface here. log.warning rather than
            # print so it's controllable at runtime.
            log.warning("sounddevice status: %s", status)

        # Rate-limited heartbeat so we can see the callback is
        # actually running + advancing. Logs once per second at
        # typical 44.1k/512-frame cadence.
        if PCMPlayer._DEBUG:
            self._callback_counter = getattr(self, "_callback_counter", 0) + 1
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

        # EQ (10-band biquad). Active only when the user has a
        # non-flat curve set — when disabled, `apply()` is an early
        # return and doesn't touch `outdata`, so bit-perfect
        # pass-through is preserved at flat EQ + full volume + not
        # muted. Filtering requires float32; we round-trip through
        # float32 when the output dtype is int16/int32.
        if self._eq is not None and self._eq.is_active():
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
        _cb_counter = getattr(self, "_seq_bump_counter", 0) + 1
        if _cb_counter >= 20:
            self._seq += 1
            _cb_counter = 0
        self._seq_bump_counter = _cb_counter

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
        if not rate_matches:
            self._stream_sample_rate = pre.sample_rate
            self._stream_sd_dtype = pre.sd_dtype
            self._stream_channels = pre.channels
        self._samples_emitted = 0
        self._callback_carry = None
        self._preload = None
        self._state = "playing"
        self._last_error = None
        self._seq += 1

        # Outside the lock (well — RLock held; cleanup stays
        # short): signal + close the old pipeline's resources.
        old_stop_flag.set()
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
        self._samples_emitted = 0
        self._callback_carry = None
        self._state = "playing"
        self._seq += 1
        self._emit()
        return True

    def _on_stream_finished(self) -> None:
        # Called by sounddevice when the stream ends — either from
        # CallbackStop (natural EOF) or from stream.stop() during
        # teardown. _teardown() sets state to idle FIRST, so we can
        # distinguish: idle here means user-initiated stop; anything
        # else means the track ended naturally.
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
            pre = self._preload
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
