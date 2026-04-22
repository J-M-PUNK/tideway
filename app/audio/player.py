"""PCMPlayer — PyAV + sounddevice replacement for VLCPlayer.

Phase 3 scope: full single-track parity with the old VLCPlayer
(pause/resume/seek/volume/mute, local files, position reporting,
StreamInfo for the quality badge), plus bit-perfect output — the
sounddevice OutputStream opens in the source's native sample rate
AND native format, no software resampling or conversion.

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
from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np
import sounddevice as sd  # type: ignore
import tidalapi

from app.audio.decoder import Decoder
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
        self._teardown()
        with self._lock:
            self._transition("loading", track_id=track_id)

        try:
            source_spec, duration_s, stream_info = self._resolve_source(
                track_id, quality
            )
            initial_source = _build_source(source_spec)
            decoder = Decoder(initial_source)
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
        """Combined load + play. Matches the VLCPlayer API so the
        server's import-path swap in Phase 7 stays mechanical. The
        pipeline lock keeps load + play atomic relative to other
        state changes."""
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

    _DEBUG = True  # verbose stderr logging — flip off once PCM is
    # solid enough to stop needing it.

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
        with self._pipeline_lock:
            with self._lock:
                existing = self._preload
                if (
                    existing is not None
                    and existing.track_id == track_id
                    and existing.quality == quality
                ):
                    return {"ok": True, "cached": True, "hit": True}
            # Different track (or no preload) — tear down any stale
            # one before we build the new one.
            self._drop_preload()

            try:
                source_spec, duration_s, stream_info = self._resolve_source(
                    track_id, quality
                )
                source = _build_source(source_spec)
                decoder = Decoder(source)
            except Exception as exc:
                log.exception("preload resolve failed for %s", track_id)
                return {"ok": False, "error": str(exc)}

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
                f"rate={decoder.sample_rate} dtype={decoder.sounddevice_dtype} "
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
                sample_rate=decoder.sample_rate,
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
            self._volume = volume
            self._seq += 1
        return self.snapshot()

    def set_muted(self, muted: bool) -> PlayerSnapshot:
        with self._lock:
            self._muted = bool(muted)
            self._seq += 1
        return self.snapshot()

    # --- EQ stubs (Phase 5 replaces these) --------------------------
    #
    # Same class-method shape as VLCPlayer so server.py can call into
    # them without branching. Until biquad filters land, the EQ
    # sliders in the UI save + persist fine, they just don't shape
    # audio. Bands/preamp values round-trip through settings.json so
    # nothing is lost when we swap engines.
    _STUB_EQ_FREQUENCIES = [
        60.0, 170.0, 310.0, 600.0, 1000.0,
        3000.0, 6000.0, 12000.0, 14000.0, 16000.0,
    ]

    @staticmethod
    def eq_presets() -> list[dict]:
        return []

    @staticmethod
    def eq_bands_count() -> int:
        return len(PCMPlayer._STUB_EQ_FREQUENCIES)

    @staticmethod
    def eq_band_frequencies() -> list[float]:
        return list(PCMPlayer._STUB_EQ_FREQUENCIES)

    def apply_equalizer(
        self, bands: list[float], preamp: Optional[float] = None
    ) -> None:
        # No-op until Phase 5 implements biquad filtering.
        return

    def apply_equalizer_preset(self, preset_index: int) -> list[float]:
        return [0.0] * self.eq_bands_count()

    # --- Device selection -------------------------------------------

    def list_output_devices(self) -> list[dict]:
        """Enumerate output-capable audio devices via sounddevice.
        Returns the same shape as VLCPlayer.list_output_devices():
        `[{"id": "<int-as-str>", "name": "<human>"}]`, with an
        empty-id "System default" entry first.
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
        """Remember the device-id selection. The next time the
        OutputStream opens (track load or cross-rate bridge) it's
        constructed on this device. Phase 6 will reopen the stream
        live; for now changes take effect at the next track.
        """
        with self._lock:
            self._selected_device_id = device_id or ""

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
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

        thread = self._decoder_thread
        self._decoder_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        decoder = self._decoder
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
    ) -> tuple[Union[str, list[str]], Optional[float], StreamInfo]:
        """Resolve a track id to a source spec.

        Returns either a local filesystem path (str) or a DASH URL
        list (list[str] — index 0 is the init segment). The caller
        constructs a `Decoder` from it via `_build_source()` and
        stores the spec on the player so seek can rebuild.
        """
        if self._local_lookup is not None:
            local_path = self._local_lookup(track_id)
            if local_path and os.path.exists(local_path):
                info = _probe_local_stream_info(local_path)
                return (local_path, None, info or StreamInfo(source="local"))

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
            return (list(urls), float(duration) if duration else None, info)
        finally:
            if override is not None:
                session.config.quality = original

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
        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype=dtype,
            device=device,
            callback=self._audio_callback,
            finished_callback=self._on_stream_finished,
        )

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
            print(f"[pcm] audio status: {status}", file=sys.stderr, flush=True)

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

        # Volume + mute post-processing. At volume=100 and not muted
        # we skip entirely, preserving bit-perfect pass-through.
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

        # Outside the lock: signal + close the old pipeline's
        # resources. The old decoder thread will notice its stop
        # flag and exit on its next iteration.
        #
        # Python doesn't let us `.release()` an RLock we're
        # inside; let the caller drop the lock after this method
        # returns by keeping cleanup here but short.
        old_stop_flag.set()
        if old_thread is not None and old_thread.is_alive():
            # Non-blocking: if the thread is mid-I/O, let it die
            # on its own. It's a daemon.
            pass
        if old_decoder is not None:
            try:
                old_decoder.close()
            except Exception:
                pass
        if old_stream is not None:
            try:
                old_stream.stop()
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

        Emits `ended` BEFORE the swap and `playing` after. This
        matches the libvlc path's frontend-contract: the frontend's
        advance logic fires on `ended`, updates its
        `expectedTrackIdRef` via `playAtIndex`, and the redundant
        `play_track(next)` that follows lands on Path 0 (no-op
        because current_track_id already matches). Without the
        `ended` emit, the frontend's expected-guard drops all new
        snapshots because its expected is still the previous track.

        Assignments are individually atomic under the GIL; the
        callback is the sole modifier during the track-boundary
        moment.
        """
        pre = self._preload
        if pre is None:
            return False
        if (
            pre.sample_rate != self._stream_sample_rate
            or pre.sd_dtype != self._stream_sd_dtype
            or pre.channels != self._stream_channels
        ):
            # Incompatible — the stream will need a full reopen.
            # Let the callback raise CallbackStop; _on_stream_finished
            # picks up the preload and opens a new stream for it.
            return False
        print(
            f"[pcm] gapless swap (inline) -> track={pre.track_id} "
            f"same rate={pre.sample_rate}Hz dtype={pre.sd_dtype}",
            file=sys.stderr, flush=True,
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
        self._preload = None
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
        with self._lock:
            if self._state in ("idle", "error"):
                return
            pre = self._preload

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
        """
        print(
            f"[pcm] gapless bridge (cross-rate) -> track={pre.track_id} "
            f"rate {self._stream_sample_rate}->{pre.sample_rate}Hz "
            f"dtype {self._stream_sd_dtype}->{pre.sd_dtype}",
            file=sys.stderr, flush=True,
        )
        # The old stream already fired finished_callback, so it's
        # stopped. Close it for good measure and release resources.
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
            # Clear the preload slot BEFORE opening a new stream so
            # a racing preload() call (unlikely this early) can't
            # re-stomp it.
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


def _build_source(spec: Union[str, list[str]]) -> Union[str, SegmentReader]:
    """Materialize a source spec into something Decoder can open."""
    if isinstance(spec, str):
        return spec
    return SegmentReader(spec)


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
