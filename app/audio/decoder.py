"""PyAV decoder wrapper — yields PCM in the source's native format.

For bit-perfect output, the decoder auto-detects the source's
sample format and configures its `AudioResampler` to emit packed
(interleaved) samples in the SAME format — no format conversion,
no sample-rate conversion. The resampler is only there because
PyAV's decoded frames come out in planar layouts
(`s16p`/`s32p`/`fltp`) and sounddevice needs packed; interleaving
is lossless.

Format mapping:
  FLAC 16-bit → `s16`  → numpy int16   → sounddevice `int16`
  FLAC 24-bit → `s32`  → numpy int32   → sounddevice `int32`
  AAC / MP3   → `fltp` → numpy float32 → sounddevice `float32`

Anything unrecognized falls through to float32, which sounddevice
always supports.
"""
from __future__ import annotations

import io
import logging
import sys
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Union

import av  # type: ignore
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class CodecInfo:
    codec: str
    sample_rate: int
    channels: int
    source_format: Optional[str]
    duration_seconds: Optional[float]
    bit_depth: Optional[int]


# Source format name -> (packed resampler format, numpy dtype, sd dtype, bit_depth)
_FORMAT_MAP = {
    "s16":  ("s16", np.int16,   "int16",   16),
    "s16p": ("s16", np.int16,   "int16",   16),
    "s32":  ("s32", np.int32,   "int32",   24),
    "s32p": ("s32", np.int32,   "int32",   24),
    "flt":  ("flt", np.float32, "float32", None),
    "fltp": ("flt", np.float32, "float32", None),
    "dbl":  ("flt", np.float32, "float32", None),
    "dblp": ("flt", np.float32, "float32", None),
}


class Decoder:
    """One `Decoder` = one track.

    `source` may be a filesystem path (for local files) or any
    file-like with read/seek (for streamed DASH via `SegmentReader`).

    Not thread-safe: the decoder thread is the sole caller of
    `next_pcm()`. `request_seek()` may be called from any thread
    — it's just a flag the decode loop reads between frames.
    """

    def __init__(self, source: Union[str, io.IOBase]):
        self._source = source
        # libav's default probesize (5 MB) and analyzeduration
        # (5 seconds of audio) cause av.open to read several DASH
        # segments before it's satisfied it knows what the stream
        # is — adding seconds to every play on hi-res. We already
        # know from Tidal's manifest that it's FLAC (or AAC) in
        # fMP4; the moov atom in the init segment plus ~200ms of
        # audio is plenty. If these values ever reject a legitimate
        # stream we fall back to the libav defaults before giving up.
        open_opts = {
            "probesize": "131072",
            "analyzeduration": "200000",
        }
        # Tidal serves fragmented MP4 at every quality tier, so we
        # can short-circuit libav's format-detection phase by naming
        # the container explicitly. Only applies to the streaming
        # path — local files go through default sniffing so arbitrary
        # downloaded formats (flac, mp3, m4a) still open.
        open_fmt = "mp4" if not isinstance(source, str) else None
        t0 = time.monotonic()
        try:
            self._container = av.open(
                source, format=open_fmt, options=open_opts
            )
        except Exception as first_exc:
            # Reopen with defaults as a safety net. The source must
            # be re-seekable for this to work; both our SegmentReader
            # and local filesystem paths support that.
            print(
                f"[perf] decoder av.open with probesize={open_opts['probesize']} "
                f"failed ({first_exc!r}); retrying with libav defaults",
                file=sys.stderr,
                flush=True,
            )
            try:
                if hasattr(source, "seek"):
                    source.seek(0)  # type: ignore[union-attr]
            except Exception:
                pass
            self._container = av.open(source)
        t_open = time.monotonic()
        streams = [s for s in self._container.streams if s.type == "audio"]
        if not streams:
            self._container.close()
            raise RuntimeError("no audio stream in source")
        self._stream = streams[0]
        cc = self._stream.codec_context
        self._sample_rate = int(cc.sample_rate)
        print(
            f"[perf] decoder av.open={((t_open - t0) * 1000.0):.0f}ms "
            f"codec_setup={((time.monotonic() - t_open) * 1000.0):.0f}ms",
            file=sys.stderr,
            flush=True,
        )
        channels = getattr(cc, "channels", None)
        if channels is None:
            layout = getattr(cc, "layout", None) or getattr(cc, "channel_layout", None)
            channels = getattr(layout, "nb_channels", None) or 2
        self._channels = int(channels)
        src_fmt = getattr(cc, "format", None)
        src_fmt_name = getattr(src_fmt, "name", None)
        out_fmt, dtype, sd_dtype, bit_depth = _FORMAT_MAP.get(
            src_fmt_name or "", ("flt", np.float32, "float32", None)
        )
        self._source_format = src_fmt_name
        self._output_format = out_fmt
        self._output_dtype = dtype
        self._sd_dtype = sd_dtype
        self._bit_depth = bit_depth
        self._resampler = self._make_resampler()
        self._iter: Optional[Iterator[av.AudioFrame]] = None
        self._done = False
        # request_seek() writes this; the decode loop reads it.
        self._pending_seek_s: Optional[float] = None

    # --- public API -------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def output_dtype(self) -> type:
        return self._output_dtype

    @property
    def sounddevice_dtype(self) -> str:
        return self._sd_dtype

    def codec_info(self) -> CodecInfo:
        return CodecInfo(
            codec=self._stream.codec_context.name,
            sample_rate=self._sample_rate,
            channels=self._channels,
            source_format=self._source_format,
            duration_seconds=self._duration_seconds(),
            bit_depth=self._bit_depth,
        )

    def request_seek(self, position_seconds: float) -> None:
        """Mark that the next `next_pcm()` call should first seek the
        underlying container to `position_seconds`. Thread-safe
        because Python assignment to a simple attribute is atomic
        under the GIL."""
        self._pending_seek_s = float(max(0.0, position_seconds))

    def next_pcm(self) -> Optional[np.ndarray]:
        """Return the next chunk of packed native-format PCM, shape
        `(N, channels)`, dtype `self.output_dtype`. Returns None at
        EOF."""
        if self._done:
            return None
        # Apply any pending seek BEFORE pulling the next frame. This
        # coordinates with PCMPlayer.seek() — the player clears the
        # pcm_queue + sets pending_seek, and the decoder applies the
        # seek on its next iteration without needing a restart.
        if self._pending_seek_s is not None:
            target_s = self._pending_seek_s
            self._pending_seek_s = None
            try:
                # container.seek wants AV_TIME_BASE units (microseconds).
                self._container.seek(
                    int(target_s * 1_000_000),
                    stream=self._stream,
                    any_frame=False,
                )
            except Exception:
                log.exception("container.seek failed for target_s=%s", target_s)
            # After a seek, PyAV's codec buffers still hold pre-seek
            # packets that can raise a spurious StopIteration on the
            # next decode. Flush them so the new iterator starts
            # clean at the target position.
            try:
                self._stream.codec_context.flush_buffers()
            except Exception:
                # Some codecs/versions don't expose flush_buffers on
                # the CodecContext directly — fall through; worst
                # case the first few samples post-seek are stale.
                pass
            # Resampler buffers samples internally; after a seek those
            # are pre-seek audio we need to discard. Rebuild it.
            self._resampler = self._make_resampler()
            self._iter = None
            # A seek back from an at-EOF state must un-stick _done.
            self._done = False

        if self._iter is None:
            self._iter = self._container.decode(self._stream)
        while True:
            try:
                frame = next(self._iter)
            except StopIteration:
                tail = self._resampler.resample(None)
                self._done = True
                if tail:
                    return _frames_to_stereo(tail, self._output_dtype)
                return None
            resampled = self._resampler.resample(frame)
            if not resampled:
                continue
            return _frames_to_stereo(resampled, self._output_dtype)

    def cancel_source(self) -> None:
        """Close just the underlying file-like source, without touching
        the container. Safe to call from a thread other than the one
        using the decoder — for SegmentReader this aborts any pending
        HTTP request so the decoder loop unblocks and notices the
        stop flag quickly. The container is left for close() to
        clean up once the decoder thread has exited."""
        if hasattr(self._source, "close"):
            try:
                self._source.close()  # type: ignore[union-attr]
            except Exception:
                pass

    def close(self) -> None:
        try:
            self._container.close()
        except Exception:
            pass
        if hasattr(self._source, "close"):
            try:
                self._source.close()  # type: ignore[union-attr]
            except Exception:
                pass

    # --- internals --------------------------------------------------

    def _make_resampler(self) -> av.AudioResampler:
        # layout="stereo" packs 2 channels; rate=source rate means
        # no SRC. format=source-packed means no format conversion
        # for lossless sources. End result is a lossless
        # planar→packed repack.
        return av.AudioResampler(
            format=self._output_format,
            layout="stereo",
            rate=self._sample_rate,
        )

    def _duration_seconds(self) -> Optional[float]:
        dur = self._container.duration
        if dur is None or dur <= 0:
            return None
        return float(dur) / 1_000_000.0


def _frames_to_stereo(frames: list, dtype: type) -> np.ndarray:
    """Stack PyAV AudioFrames (packed stereo) into one (N, 2) array."""
    chunks: list[np.ndarray] = []
    for f in frames:
        arr = f.to_ndarray()
        if arr.ndim == 2 and arr.shape[0] == 1:
            arr = arr.reshape(-1, 2)
        elif arr.ndim == 2 and arr.shape[0] == 2:
            arr = arr.T
        chunks.append(arr)
    if not chunks:
        return np.zeros((0, 2), dtype=dtype)
    return np.concatenate(chunks, axis=0).astype(dtype, copy=False)
