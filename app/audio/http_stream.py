"""Shared infrastructure for serving Tideway's audio over HTTP.

The Chromecast sender (`app/audio/cast.py`) and the UPnP/DLNA
sender (`app/audio/upnp.py`) both deliver audio the same way: hand
the receiver a LAN URL that streams an open-ended FLAC. The
delivery side lives here so neither has to reimplement it:

  RingBuffer        — a bounded byte buffer the encoder writes to
                      and the HTTP serve loop reads from.

  FlacStreamEncoder — PCM in, FLAC bytes out. PyAV-backed. Output
                      is a continuous, open-ended FLAC stream that
                      the receiver can consume mid-flight; FLAC's
                      frame structure makes this work without a
                      seekable container.

  StreamHTTPServer  — tiny dedicated HTTP listener bound to the
                      LAN IP, serving the FLAC bytes from the
                      RingBuffer at a configurable path. Has to be
                      separate from the FastAPI server because
                      FastAPI binds 127.0.0.1 (intentional — every
                      other /api/* endpoint must NOT be LAN-
                      reachable) but a Cast / DLNA receiver is on
                      the LAN and can't reach loopback.

  primary_lan_ip()  — picks the right LAN-facing interface so the
                      URL we hand the receiver is actually
                      reachable.

These primitives are protocol-agnostic. Cast and DLNA layer their
own control protocols on top; the audio-delivery side is identical.
"""
from __future__ import annotations

import http.server
import logging
import socket
import socketserver
import threading
import time
import urllib.parse
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# DLNA `additionalInfo` / `contentFeatures.dlna.org` value for our
# open-ended FLAC stream. Strict UPnP renderers (USB Audio Player PRO,
# some TVs) validate this against their Sink protocolInfo and silently
# reject a bare "*". Shared with app/audio/openhome.py's DIDL builder so
# the DIDL `protocolInfo` and the HTTP `contentFeatures.dlna.org` header
# advertise exactly the same thing.
#   DLNA.ORG_OP=01  byte-range seek supported (we answer Range with 206,
#                   see _send_stream_headers), no time-seek.
#   DLNA.ORG_CI=0   content is delivered as-is, not transcoded.
#   DLNA.ORG_FLAGS  0x05700000 in the top 32 bits, then 96 reserved zero
#                   bits: SN_INCREASING(26) | STREAMING(24) |
#                   BACKGROUND(22) | CONNECTION_STALL(21) | DLNA_V15(20).
#                   This is the flag set philippe44's squeeze2upnp uses
#                   for a live, non-transcoded stream
#                   (application/squeezelite/mimetypes.c:format_to_dlna).
DLNA_CONTENT_FEATURES = (
    "DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
    "DLNA.ORG_FLAGS=05700000000000000000000000000000"
)

# The stream is open-ended, but a strict renderer's Range probe wants a
# finite total to seek against before its decoder-init proceeds. We
# advertise a large synthetic size it can never reach (it only reads the
# live edge and never actually seeks there). 1 TiB, matching the
# technique squeeze2upnp uses for renderers that reject an open range.
_STREAM_SYNTHETIC_TOTAL = 1 << 40

# Upper bound on how long attach() waits for the outgoing consumer to
# serve its first chunk before superseding it. Returns the instant that
# chunk is served, so this cap only bites when the encoder hasn't
# produced any bytes yet — see RingBuffer.attach().
_SUPERSEDE_GRACE_S = 1.0


class RingBuffer:
    """A bounded byte buffer with blocking read.

    Writers (the FLAC encoder) push bytes in. Readers (the HTTP
    serve loop) pull them out at the rate the receiver consumes.
    On overflow we drop the oldest bytes — better an audible
    glitch on the receiver than blocking the audio engine's
    callback.

    Not a speed-critical path: writers produce at realtime audio
    rate (a few hundred KB/s for hi-res FLAC), well under any
    reasonable buffer limit.
    """

    def __init__(
        self,
        max_bytes: int = 8 * 1024 * 1024,
        # 8MB head cache so concurrent probe+init+streaming
        # connections (UAPP opens 4 in parallel) can all read
        # from the non-destructive cache without competing for
        # the destructive RingBuffer. At 24/96 stereo FLAC,
        # 8MB ≈ 14s of audio — covers probe + decoder init +
        # the first seconds of streaming.
        head_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._max = max_bytes
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._closed = False
        # True when the data source (encoder) has finished writing and
        # no more bytes will ever arrive.  The HTTP serve loop checks
        # this so it can close the connection promptly — without it the
        # renderer blocks on a stale HTTP connection and delays
        # track-change processing.
        self._source_done = False
        # Set by the encoder after it has written enough data (FLAC
        # header + first audio frame) for the receiver's decoder to
        # init. The HTTP handler waits on this before sending response
        # headers, so the receiver never sees an empty stream.
        self._data_ready = threading.Event()
        # Single-active-consumer guard. read() is destructive (it
        # deletes the bytes it returns), so two HTTP serve threads on
        # the same buffer would split the FLAC stream and corrupt
        # both. The Cast track-change reload issues a fresh play_media
        # per track, and the receiver's new GET can briefly overlap
        # the old connection. attach() bumps this generation; an
        # older serve loop sees it move and exits, leaving exactly
        # one consumer.
        self._gen = 0
        # Whether the current generation's consumer has been handed at
        # least one non-empty chunk. Strict renderers (UAPP) open two
        # connections in quick succession — a probe, then the real
        # fetch. If the second attach() superseded the first before it
        # served anything, the probe's HTTP client saw an empty
        # response and treated the whole stream as broken. We hold the
        # supersede until the outgoing consumer has served a chunk (or a
        # grace window lapses), so the probe always gets real bytes.
        self._served = False
        # Phase-3 lock: True while the current consumer is in the
        # destructive RingBuffer phase. When True, attach() does NOT
        # supersede — instead, the new caller becomes "head-only"
        # (gen stays the same; the new caller reads only from head
        # cache). This prevents UAPP probe/init connections from
        # killing the main streaming connection mid-track.
        self._ring_active = False
        # Head cache: non-destructive copy of the first head_bytes
        # written. Served to any consumer that needs bytes from the
        # beginning of the stream (e.g. FFmpeg probe/init connecting
        # after the RingBuffer's destructive head has been consumed).
        self._head = bytearray()
        self._head_max = head_bytes
        # Total bytes ever written / destructively consumed. Used to
        # determine which byte-offset _buf[0] represents, so a consumer
        # can skip to the right position after serving from head cache.
        self._total_written = 0
        self._total_read = 0
        # Track identifier set by the UPnP session on each track change.
        # The HTTP handler validates that incoming requests carry a
        # matching ``?ts=`` query param, rejecting stale requests from
        # a previous track that would otherwise steal the ring lock
        # and starve the legitimate consumer. 0 = unset (initial /
        # transitional).
        self._track_id = 0

    def attach(self, force: bool = False) -> tuple:
        """Register as a consumer. Returns (gen, head_only) tuple.

        - If force=False and the prior consumer is in Phase 3
          (_ring_active=True), DO NOT supersede. Return
          (current_gen, True). The caller is "head-only" — it must
          read only from read_head(). This lets UAPP's probe/init
          connections coexist with the main streaming connection.

        - If force=True, ALWAYS supersede regardless of _ring_active.
          Used when the new caller needs to read from the RingBuffer
          (e.g., UAPP seeks beyond head cache). The old consumer's
          read() returns b"" immediately on supersede, so there's no
          destructive-read race.

        - If no prior consumer exists, OR the prior consumer hasn't
          entered Phase 3 yet (_ring_active=False), supersede it and
          return (new_gen, False).

        If a prior consumer exists that hasn't served a chunk yet, wait
        (bounded by _SUPERSEDE_GRACE_S) for it to serve one before
        superseding, so a renderer's probe connection isn't killed with
        zero bytes."""
        with self._cv:
            # If the current consumer is in Phase 3 (RingBuffer),
            # don't supersede UNLESS force=True. The new caller
            # becomes head-only.
            if (
                self._ring_active
                and self._served
                and not self._closed
                and not force
            ):
                return (self._gen, True)
            if self._gen > 0 and not self._served and not self._closed:
                deadline = time.monotonic() + _SUPERSEDE_GRACE_S
                while not self._served and not self._closed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cv.wait(timeout=remaining)
            self._gen += 1
            self._served = False
            self._ring_active = False
            self._cv.notify_all()
            return (self._gen, False)

    def is_superseded(self, gen: int) -> bool:
        """True once a newer consumer has attach()ed. The serve loop
        checks this each iteration and returns, so a stale connection
        stops draining the shared buffer."""
        return gen != self._gen

    def set_ring_active(self, gen: int) -> None:
        """Mark the current consumer as having entered Phase 3
        (destructive RingBuffer reads). Once set, attach() will NOT
        supersede this consumer — new callers become head-only.

        Only the consumer whose gen matches self._gen may set this.
        Called by do_GET just before entering the Phase 3 loop."""
        with self._cv:
            if gen == self._gen:
                self._ring_active = True
                self._cv.notify_all()

    def set_track_id(self, track_id: int) -> None:
        """Set the current track identifier.

        Called by the UPnP session after flush() and before
        notifying the renderer of a new track, so the HTTP handler
        can reject stale requests carrying a previous track's
        ?ts= value."""
        with self._cv:
            self._track_id = track_id

    @property
    def track_id(self) -> int:
        return self._track_id

    def clear_ring_active(self, gen: int) -> None:
        """Clear _ring_active when the Phase 3 consumer exits.

        Without this, _ring_active stays True forever after the
        first connection breaks, and ALL subsequent connections
        become head-only — no one ever reads from the RingBuffer
        again, and the stream dies.

        Only clears if gen == self._gen (defensive: a superseded
        consumer shouldn't clobber a newer one's flag, though in
        practice a Phase 3 consumer can't be superseded)."""
        with self._cv:
            if gen == self._gen:
                if self._ring_active:
                    self._ring_active = False
                    self._cv.notify_all()

    def write(
        self,
        data: bytes,
        block: bool = False,
        block_timeout: float = 60.0,
    ) -> bool:
        """Write data to the buffer.

        With block=False (default, used by FlacStreamEncoder / PCM
        path), drops oldest bytes on overflow — the audio callback
        cannot block, so a glitch is preferable to a stall.

        With block=True (used by FlacPassthroughEncoder), waits up
        to block_timeout seconds for the consumer to drain enough
        space. Returns True if written, False if closed.
        """
        deadline = time.monotonic() + block_timeout
        with self._cv:
            # Fill head cache under the lock so write() and flush()
            # don't race on _head (flush clears it under the lock).
            if len(self._head) < self._head_max:
                space = self._head_max - len(self._head)
                self._head.extend(data[:space])
            while True:
                if self._closed:
                    return False
                overflow = len(self._buf) + len(data) - self._max
                if overflow <= 0:
                    self._buf.extend(data)
                    self._total_written += len(data)
                    self._signal_data_ready(len(data))
                    self._cv.notify_all()
                    return True
                if not block:
                    log.debug(
                        "ring buffer overflow: dropping %d bytes", overflow
                    )
                    del self._buf[:overflow]
                    self._total_read += overflow
                    self._buf.extend(data)
                    self._total_written += len(data)
                    self._signal_data_ready(len(data))
                    self._cv.notify_all()
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(
                        "ring buffer blocking write timed out after "
                        "%.1fs (buf=%d/%d); dropping %d bytes",
                        block_timeout, len(self._buf), self._max, overflow,
                    )
                    del self._buf[:overflow]
                    self._total_read += overflow
                    self._buf.extend(data)
                    self._total_written += len(data)
                    self._signal_data_ready(len(data))
                    self._cv.notify_all()
                    return True
                self._cv.wait(timeout=min(0.5, remaining))

    def _signal_data_ready(self, written: int) -> None:
        """Set data_ready on the first write after creation or flush.

        Called from ``write()`` when ``_total_written`` transitions
        from 0 to a positive value (i.e., actual data was added).
        ``written`` is the byte count just added — if
        ``_total_written == written`` the counter was 0 before this
        write. This wakes ``do_GET()`` which waits on ``data_ready``
        before responding to the renderer's HTTP request. Without
        this, Cast sessions (FlacStreamEncoder never touches
        ``data_ready``) would time out and return 503."""
        if self._total_written == written:
            self._data_ready.set()

    def read(
        self, n: int, timeout: float = 1.0, gen: Optional[int] = None
    ) -> bytes:
        deadline = time.monotonic() + timeout
        with self._cv:
            while not self._buf and not self._closed:
                if gen is not None and gen != self._gen:
                    # Superseded by a newer consumer while we were
                    # blocked here — return empty so the stale serve
                    # loop exits instead of consuming the new
                    # consumer's bytes.
                    return b""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cv.wait(timeout=remaining)
            if gen is not None and gen != self._gen:
                return b""
            chunk = bytes(self._buf[:n])
            del self._buf[: len(chunk)]
            self._total_read += len(chunk)
            if chunk and (gen is None or gen == self._gen):
                # This consumer has now been handed real bytes; a
                # pending attach() may stop waiting and supersede us.
                self._served = True
                self._cv.notify_all()
            return chunk

    def flush(self) -> None:
        """Drop all buffered bytes, keep the stream open.

        Used on a Cast track-change reset: the encoder is rebuilt so
        a fresh FLAC header leads the stream, and the receiver's new
        connection then starts at the live edge instead of replaying
        the seconds of backlog that pile up FIFO. That backlog is the
        permanent latency floor today.

        Clears data_ready so the next do_GET waits for the new
        encoder to write its header before responding.

        CRITICAL: bumps _gen so any stale consumer waiting in read()
        is immediately invalidated (gen mismatch → returns b"").
        Without this, a stale HTTP serve loop from the previous track
        picks up data written by the new encoder and sends it to the
        renderer's old socket — the new track's FLAC header arrives
        mid-stream on the old connection, corrupting the decoder.
        """
        with self._cv:
            self._head.clear()
            self._total_written = 0
            self._total_read = 0
            self._source_done = False
            self._buf.clear()
            self._ring_active = False
            self._track_id = 0
            self._gen += 1
            self._data_ready.clear()
            self._cv.notify_all()

    def fill_ratio(self) -> float:
        with self._cv:
            return len(self._buf) / self._max if self._max > 0 else 0.0

    def read_head(self, offset: int, size: int) -> bytes:
        """Non-destructive read from head cache. Returns up to
        ``size`` bytes starting at ``offset``. Returns b"" when
        ``offset`` is past the end of cache."""
        if offset >= len(self._head):
            return b""
        end = min(offset + size, len(self._head))
        return bytes(self._head[offset:end])

    @property
    def head_size(self) -> int:
        return len(self._head)

    @property
    def ring_start_offset(self) -> int:
        """Byte offset of ``_buf[0]`` in the stream. A consumer that
        has already served ``N`` bytes from head cache can skip to
        this offset to avoid overlap / gaps."""
        return self._total_read

    def skip_to(
        self,
        target_offset: int,
        gen: Optional[int] = None,
        timeout: float = 5.0,
    ) -> bool:
        """Destructively skip bytes until ``_buf[0]`` represents
        ``target_offset``. Returns True once positioned, False on
        timeout / closed / superseded."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._total_read < target_offset:
                if self._closed:
                    return False
                if gen is not None and gen != self._gen:
                    return False
                if self._buf:
                    skip = min(len(self._buf), target_offset - self._total_read)
                    del self._buf[:skip]
                    self._total_read += skip
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cv.wait(timeout=min(0.5, remaining))
            return True

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._ring_active = False
            self._cv.notify_all()

    @property
    def data_ready(self) -> threading.Event:
        return self._data_ready

    @property
    def is_closed(self) -> bool:
        return self._closed

    def source_done(self) -> None:
        """Signal that the source (encoder) has finished writing.

        Once all remaining bytes are consumed the serve loop will
        close the connection so the renderer sees end-of-stream.
        """
        with self._cv:
            self._source_done = True
            self._cv.notify_all()

    @property
    def is_source_done(self) -> bool:
        return self._source_done


class FlacStreamEncoder:
    """PCM chunks in, FLAC bytes out.

    Open an output container to a `BytesIO`-like sink and push
    encoded frames as they come out. FLAC's frame structure is
    self-contained, so an open-ended encoded stream is valid to
    consume mid-flight; the receiver doesn't need a seekable
    container.

    FLAC is integer-only — the encoder rejects floating-point
    sample formats. Pass int16 or int32 PCM; the Cast / DLNA
    pipeline upstream is responsible for not handing
    over float audio.
    """

    def __init__(self, sample_rate: int, channels: int, dtype: str) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        if dtype == "int16":
            self.av_format = "s16"
            self.np_dtype = np.int16
        elif dtype == "int32":
            self.av_format = "s32"
            self.np_dtype = np.int32
        else:
            raise ValueError(
                f"FlacStreamEncoder: unsupported dtype {dtype!r}. "
                "FLAC is integer-only; pass int16 or int32."
            )
        self._setup()

    def _setup(self) -> None:
        import av  # type: ignore

        self._buffer = bytearray()

        class _SinkFile:
            """File-like sink PyAV writes to. The FLAC muxer writes
            linearly during live encoding, but on container close
            it seeks back to the start to rewrite the STREAMINFO
            header with the now-known total sample count. So we
            do need to support seek / tell, even if mid-stream it
            never fires. The underlying bytearray grows as writes
            hit new positions.
            """

            def __init__(self, sink: bytearray) -> None:
                self._sink = sink
                self._pos = 0

            def write(self, data: bytes) -> int:
                needed = self._pos + len(data)
                if needed > len(self._sink):
                    self._sink.extend(b"\x00" * (needed - len(self._sink)))
                self._sink[self._pos:needed] = data
                self._pos = needed
                return len(data)

            def tell(self) -> int:
                return self._pos

            def seek(self, offset: int, whence: int = 0) -> int:
                if whence == 0:
                    self._pos = offset
                elif whence == 1:
                    self._pos += offset
                elif whence == 2:
                    self._pos = len(self._sink) + offset
                else:
                    raise ValueError(f"unsupported seek whence {whence}")
                return self._pos

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        self._sink = _SinkFile(self._buffer)
        # `mode="w"` + `format="flac"` opens a FLAC muxer. Live
        # encoding writes frames linearly; STREAMINFO rewrite at
        # container-close seeks back to byte 0, but by that point
        # we're tearing the session down anyway.
        self._container = av.open(self._sink, mode="w", format="flac")  # type: ignore
        self._stream = self._container.add_stream(  # type: ignore
            "flac", rate=self.sample_rate
        )
        # PyAV 17 moved channel / format configuration onto the
        # codec_context and made the stream shortcut attributes
        # read-only. Configure through codec_context. FLAC only
        # accepts integer sample formats (s16 / s32 / s32p); we
        # never send "flt" to a FLAC stream.
        codec_ctx = self._stream.codec_context  # type: ignore
        codec_ctx.layout = "stereo" if self.channels == 2 else "mono"
        codec_ctx.format = av.AudioFormat(self.av_format)  # type: ignore
        codec_ctx.sample_rate = self.sample_rate

    def encode(self, pcm: np.ndarray) -> bytes:
        """Feed one chunk of interleaved PCM. Returns whatever
        encoded bytes came out. May return b"" if PyAV buffered
        the frame."""
        import av  # type: ignore

        if pcm.ndim != 2:
            raise ValueError(f"expected 2-D PCM, got shape {pcm.shape}")
        _frames, ch = pcm.shape
        if ch != self.channels:
            raise ValueError(
                f"channel mismatch: encoder set for {self.channels}, got {ch}"
            )
        # FLAC codec takes packed (interleaved) samples. PyAV's
        # `from_ndarray` expects packed arrays shaped (1, frames*ch)
        # with all samples concatenated in channel-interleaved order.
        # Input here is already interleaved as (frames, channels), so
        # a row-major flatten followed by reshape to (1, -1) lands in
        # the exact layout the encoder wants without a real copy if
        # the input is already contiguous.
        flat = np.ascontiguousarray(
            pcm.astype(self.np_dtype, copy=False)
        ).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(  # type: ignore
            flat,
            format=self.av_format,
            layout="stereo" if self.channels == 2 else "mono",
        )
        frame.rate = self.sample_rate
        before = len(self._buffer)
        for packet in self._stream.encode(frame):  # type: ignore
            self._container.mux(packet)  # type: ignore
        return bytes(self._buffer[before:])

    def close(self) -> bytes:
        """Flush + close. Returns final trailing bytes."""
        before = len(self._buffer)
        try:
            for packet in self._stream.encode():  # type: ignore
                self._container.mux(packet)  # type: ignore
            self._container.close()  # type: ignore
        except Exception:
            pass
        return bytes(self._buffer[before:])


# ---------------------------------------------------------------------
# HTTP serving
# ---------------------------------------------------------------------

_FLAC_SAMPLE_SIZE_MAP = {0: 16, 1: 8, 2: 12, 3: 16, 4: 16, 5: 20, 6: 24, 7: 32}


def _parse_flac_frame_bps(frame_bytes: bytes) -> int:
    """Read bits_per_sample from the first FLAC frame header.

    Frame header byte layout (after 2-byte sync):
      byte 2: [block_size(4), sample_rate(4)]
      byte 3: [channels(4), sample_size(3), reserved(1)]
    """
    if len(frame_bytes) < 4:
        return 16
    code = (frame_bytes[3] >> 1) & 0x07
    return _FLAC_SAMPLE_SIZE_MAP.get(code, 16)


def _build_flac_stream_header(
    sample_rate: int,
    channels: int,
    bits_per_sample: int,
    total_samples: int = 0,
    streaminfo_bytes: Optional[bytes] = None,
) -> bytes:
    """Build a minimal valid FLAC STREAMINFO header for passthrough.

    The initial bytes we hand a strict renderer must be a proper FLAC
    stream marker followed by a STREAMINFO metadata block. We pack the
    metadata fields directly instead of relying on libav's muxer so we
    can emit a deterministic header even when the source extradata is
    absent or malformed.
    """
    if channels <= 0:
        channels = 2
    if sample_rate <= 0:
        sample_rate = 44100
    if bits_per_sample <= 0:
        bits_per_sample = 16

    if streaminfo_bytes and len(streaminfo_bytes) >= 34:
        streaminfo = bytearray(streaminfo_bytes[:34])
        metadata_len = 34
    else:
        streaminfo = bytearray(34)
        metadata_len = 34
        # min/max block size (big-endian; 4096 is a sane default)
        streaminfo[0] = 0x10
        streaminfo[1] = 0x00
        streaminfo[2] = 0x10
        streaminfo[3] = 0x00

    # FLAC STREAMINFO byte layout for bytes 10-17 (sample rate 20 bits,
    # channels-1 3 bits, bits-per-sample-1 5 bits, total samples 36 bits):
    #
    #   byte 10:      sample_rate[19:12]
    #   byte 11:      sample_rate[11:4]
    #   byte 12[7:4]: sample_rate[3:0]
    #   byte 12[3:1]: (channels - 1)
    #   byte 12[0]:   (bits_per_sample - 1)[4]
    #   byte 13[7:4]: (bits_per_sample - 1)[3:0]
    #   byte 13[3:0]: total_samples[35:32]
    #   byte 14:      total_samples[31:24]
    #   byte 15:      total_samples[23:16]
    #   byte 16:      total_samples[15:8]
    #   byte 17:      total_samples[7:0]
    sr = sample_rate & 0xFFFFF
    ch_minus_1 = (channels - 1) & 0x07
    bps_minus_1 = (bits_per_sample - 1) & 0x1F

    streaminfo[10] = (sr >> 12) & 0xFF
    streaminfo[11] = (sr >> 4) & 0xFF

    total_samples &= 0xFFFFFFFFF  # 36-bit mask
    if total_samples:
        # Caller supplied a known total — build every field from params.
        streaminfo[12] = ((sr & 0xF) << 4) | (ch_minus_1 << 1) | ((bps_minus_1 >> 4) & 1)
        streaminfo[13] = ((bps_minus_1 & 0xF) << 4) | ((total_samples >> 32) & 0xF)
        streaminfo[14] = (total_samples >> 24) & 0xFF
        streaminfo[15] = (total_samples >> 16) & 0xFF
        streaminfo[16] = (total_samples >> 8) & 0xFF
        streaminfo[17] = total_samples & 0xFF
    else:
        if streaminfo_bytes and len(streaminfo_bytes) >= 34:
            # Keep the extradata as-is (block sizes, total_samples, MD5).
            # The caller may supply sr/ch/bps which we apply here.
            streaminfo[12] = ((sr & 0xF) << 4) | (ch_minus_1 << 1) | ((bps_minus_1 >> 4) & 1)
            streaminfo[13] = ((bps_minus_1 & 0xF) << 4) | (streaminfo[13] & 0x0F)
        else:
            # No extradata — build bps from caller's parameters.
            streaminfo[12] = ((sr & 0xF) << 4) | (ch_minus_1 << 1) | ((bps_minus_1 >> 4) & 1)
            streaminfo[13] = (bps_minus_1 & 0xF) << 4

    block_header = bytes([
        0x80,
        (metadata_len >> 16) & 0xFF,
        (metadata_len >> 8) & 0xFF,
        metadata_len & 0xFF,
    ])
    return b"fLaC" + block_header + bytes(streaminfo)


class FlacPassthroughEncoder:
    """Demuxes fMP4 source and remuxes raw FLAC packets into a
    continuous FLAC byte stream. No PCM decode. Output is written
    directly into a RingBuffer on a background thread.

    This preserves the original STREAMINFO (with real total_samples)
    and seektable from the Tidal encoder, which strict DLNA renderers
    like UAPP require for seek-back and decoder initialization.
    """

    def __init__(
        self,
        source,
        buffer: "RingBuffer",
        stop_flag=None,
        done_event=None,
    ) -> None:
        self._source = source
        self._buffer = buffer
        self._stop_flag = stop_flag
        self._done_event = done_event
        self._thread = None
        self._container_in = None
        self._failed = False

    def start(self) -> None:
        import threading
        self._thread = threading.Thread(
            target=self._run, name="flac-passthrough", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        import av
        # Clear data_ready: this encoder pass will set it again once
        # the FLAC header has been written. Handles the case where
        # start() is called without an intervening flush().
        self._buffer.data_ready.clear()
        try:
            # Open fMP4 source (same as Decoder does)
            self._container_in = av.open(
                self._source,
                format="mp4",
                options={"probesize": "131072", "analyzeduration": "200000"},
            )
            stream_in = next(
                s for s in self._container_in.streams if s.type == "audio"
            )

            # --- True raw-packet passthrough ---
            #
            # Tidal sends FLAC frames inside fMP4. Each demuxed packet's
            # `data` bytes are already raw FLAC audio frames (sync word
            # 0xFFF8...). We just need a valid FLAC stream header
            # ("fLaC" + STREAMINFO block) before them.
            #
            # The STREAMINFO block is available as codec extradata on the
            # fMP4 audio stream — it's the 34-byte blob the MP4 muxer
            # stashed in the `alac`/`dfla` box. PyAV exposes it as
            # stream.codec_context.extradata. We just wrap it as a
            # proper FLAC STREAMINFO metadata block with the
            # LAST_METADATA_BLOCK flag set (since we emit no seektable
            # or other blocks).
            #
            # This preserves total_samples from Tidal's original encode
            # (UAPP needs it for its decoder-init seek), unlike any
            # approach that re-muxes through PyAV's FLAC muxer (which
            # always writes total_samples=0 for open-ended streams).

            extradata = getattr(
                stream_in.codec_context, "extradata", None
            ) or b""
            log.debug(
                "[upnp] passthrough: extradata (%dB) hex=%s",
                len(extradata), extradata[:48].hex(),
            )
            # PyAV/libav wraps the raw STREAMINFO in an extra 4-byte
            # "dfla" version header on some builds. Strip it: STREAMINFO
            # data always starts with the min/max block/frame sizes
            # (4+4 bits = first 4 bytes of payload), NOT with 0x80
            # (the LAST_METADATA_BLOCK|STREAMINFO marker). Detect the
            # wrapper by checking if the first byte looks like a
            # metadata-block header.
            if len(extradata) >= 4 and extradata[0] == 0x80:
                # Wrapped — skip the 4-byte dfla header
                raw_streaminfo = extradata[4:]
            else:
                raw_streaminfo = extradata

            if len(raw_streaminfo) < 18:
                print(
                    f"[upnp] passthrough: extradata too short "
                    f"({len(raw_streaminfo)} bytes), using fallback header",
                    flush=True,
                )
                raw_streaminfo = b""

            log.debug(
                "[upnp] passthrough: raw_streaminfo (%dB) hex=%s",
                len(raw_streaminfo), raw_streaminfo[:48].hex(),
            )

            si_len = len(raw_streaminfo) if raw_streaminfo else 34

            demux_iter = self._container_in.demux(stream_in)
            first_packet = next(demux_iter)
            raw_first = bytes(first_packet)
            frame_bps = _parse_flac_frame_bps(raw_first) if raw_first else 16

            # Parse the true audio bps from the extradata STREAMINFO.
            extradata_bps = 16
            if raw_streaminfo and len(raw_streaminfo) >= 18:
                extradata_bps = (
                    ((raw_streaminfo[12] & 1) << 4)
                    | (raw_streaminfo[13] >> 4)
                ) + 1

            if raw_streaminfo:
                header = _build_flac_stream_header(
                    sample_rate=stream_in.codec_context.sample_rate,
                    channels=getattr(
                        stream_in.codec_context.layout, "nb_channels", 2
                    ) or 2,
                    bits_per_sample=extradata_bps,
                    total_samples=0,
                    streaminfo_bytes=raw_streaminfo,
                )
            else:
                header = _build_flac_stream_header(
                    sample_rate=stream_in.codec_context.sample_rate,
                    channels=getattr(
                        stream_in.codec_context.layout, "nb_channels", 2
                    ) or 2,
                    bits_per_sample=frame_bps,
                    total_samples=0,
                    streaminfo_bytes=None,
                )
            self._buffer.write(header, block=True)
            # Signal the HTTP handler that data is available, so it can
            # start responding to the renderer's GET. Must happen after
            # at least the FLAC header is in the buffer — the renderer
            # needs valid FLAC bytes on its first read to init its
            # decoder.
            self._buffer.data_ready.set()
            log.debug(
                "[upnp] passthrough: wrote FLAC header "
                "(%d bytes, STREAMINFO=%dB bps=%d) hex=%s",
                len(header), si_len, extradata_bps, header.hex()[:64],
            )

            # Use the frame-detected bps for pacing.
            _sr = stream_in.codec_context.sample_rate or 44100
            _ch = getattr(stream_in.codec_context.layout, "nb_channels", 2) or 2
            _PCM_BYTE_RATE = max(1, _sr * _ch * frame_bps // 8)
            packets_sent = 0

            def _write_packet(raw: bytes) -> bool:
                nonlocal packets_sent
                if packets_sent < 3:
                    sync_ok = raw[:2] == b"\xff\xf8" or raw[:2] == b"\xff\xf9"
                    log.debug(
                        "[upnp] passthrough: pkt#%d len=%d sync=%s hex=%s",
                        packets_sent, len(raw), sync_ok, raw[:32].hex(),
                    )
                if not self._buffer.write(raw, block=True, block_timeout=600.0):
                    print(
                        "[upnp] passthrough: buffer closed, stopping",
                        flush=True,
                    )
                    return False
                sleep_s = len(raw) / _PCM_BYTE_RATE
                if sleep_s > 0 and sleep_s < 1.0:
                    if self._stop_flag is not None:
                        if self._stop_flag.wait(timeout=sleep_s):
                            return False
                    else:
                        time.sleep(sleep_s)
                packets_sent += 1
                return True

            _ok = not raw_first or _write_packet(raw_first)
            if _ok:
                for packet in demux_iter:
                    if self._stop_flag is not None and self._stop_flag.is_set():
                        break
                    raw = bytes(packet)
                    if not raw:
                        continue
                    if not _write_packet(raw):
                        break

            print(
                f"[upnp] passthrough: demux complete, {packets_sent} packets sent",
                flush=True,
            )
        except Exception as exc:
            self._failed = True
            print(
                f"[upnp] passthrough encode failed: {exc!r}",
                flush=True,
            )
        finally:
            try:
                if self._container_in is not None:
                    self._container_in.close()
            except Exception:
                pass
            # Signal source_done only on failure so the HTTP serve loop
            # can close the connection once remaining data is drained.
            # On normal completion we do NOT signal — the encoder
            # finishes before the PCM decoder advances to the next
            # track, and closing the connection here would break
            # gapless playback (UAPP sees EOF and never receives the
            # SetAVTransportURI for the next track).
            if self._failed:
                self._buffer.source_done()
            if self._done_event is not None:
                self._done_event.set()
            print("[upnp] passthrough thread ended", flush=True)

    def close(self) -> None:
        """Stop the encoder thread and clean up.

        Ordering is CRITICAL: the thread must exit BEFORE we close
        the PyAV container. Closing the container while the thread
        is inside av_read_frame() (called by container.demux())
        causes use-after-free in libav's internal buffers → segfault.

        Safe sequence:
        1. Set stop_flag (thread checks this between packets)
        2. Close the source (interrupts blocked I/O in av_read_frame)
        3. Join the thread (waits for clean exit, up to 5s)
        4. Close the container (now safe, thread is done with it)
        """
        if self._stop_flag is not None:
            self._stop_flag.set()
        # Close the source next — interrupts any blocked read() in
        # libav's av_read_frame, causing demux() to raise. The
        # thread's try/except catches it and exits.
        try:
            if hasattr(self._source, "close"):
                self._source.close()
        except Exception:
            pass
        # Wait for the thread to exit.
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        # NOW safe to close the container — the thread is done with it.
        try:
            if self._container_in is not None:
                self._container_in.close()
        except Exception:
            pass


class StreamHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Dedicated HTTP server, bound to 0.0.0.0 on an ephemeral port,
    used solely for serving an audio stream to a LAN receiver.

    Why not just expose the stream endpoint on the main FastAPI
    server: FastAPI binds to 127.0.0.1 in both dev and packaged
    builds. A Cast / DLNA receiver is on the LAN and can't reach
    that address. Binding FastAPI to 0.0.0.0 would fix the
    reachability but would also expose every other /api/* endpoint
    to the LAN, which is a security regression. Running a tiny
    dedicated listener just for the stream endpoint keeps the blast
    radius to exactly this one stream.

    Lifecycle is bolted to the session — Cast or DLNA manager
    starts it on connect, shuts it down on disconnect. Serves a
    single configurable path; everything else 404s.
    """

    allow_reuse_address = True
    daemon_threads = True

    # Populated by the manager when binding so the handler can read
    # the session's ring buffer and configured path without
    # plumbing them through the HTTP-server constructor chain.
    buffer: Optional["RingBuffer"] = None
    stream_path: str = "/stream"
    content_type: str = "audio/flac"
    # DLNA renderers negotiate transferMode.dlna.org; Cast receivers
    # don't and shouldn't see the header. Set per session so the
    # shared handler only emits DLNA-specific headers on DLNA streams.
    dlna: bool = False


class _StreamRequestHandler(http.server.BaseHTTPRequestHandler):
    # Python's BaseHTTPRequestHandler defaults to HTTP/1.0. Combined
    # with our chunked Transfer-Encoding, that's a spec violation —
    # chunked encoding is HTTP/1.1+ only. Cast / DLNA receivers
    # that see "HTTP/1.0 200 OK" plus "Transfer-Encoding: chunked"
    # apply HTTP/1.0's "close after body" semantics, get a stream
    # that never closes a body they can't parse, and bail silently.
    # Hisense's Cast receiver in particular drops back to its prior
    # screen with no error to either side. Force HTTP/1.1 so the
    # response line and the chunked encoding agree.
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # type: ignore[override]
        # Quiet by default; switch to log.debug so `run.sh` doesn't
        # drown in per-chunk access-log lines during streaming.
        log.debug("http_stream: " + format, *args)

    # DLNA renderers carry their intent in these request headers. We
    # echo/answer the ones a strict renderer (UAPP, Hisense TVs)
    # checks, and log the lot so a "device stays silent" report comes
    # with the exact bytes the renderer sent instead of guesswork.
    _DLNA_REQUEST_HEADERS = (
        "transferMode.dlna.org",
        "getcontentFeatures.dlna.org",
        "getAvailableSeekRange.dlna.org",
        "getMediaInfo.sec",
        "Range",
        "User-Agent",
    )

    def _request_path(self) -> str:
        """Path portion of the request, query string stripped.

        Strict renderers append their own query params to the URL we
        hand them (Hisense pulls `/dlna/stream?mediaPlayerId=1&playMode=2`).
        Compare on the path alone so those params don't 404 the stream.
        """
        return urllib.parse.urlsplit(self.path).path

    def _log_connection(self, method: str) -> None:
        # One high-signal line per connection (not per chunk): did the
        # renderer reach our stream URL, and with what DLNA intent?
        # Pairs with the encoder-failure print in UpnpManager so a
        # "stream never plays" report splits into "device never
        # connected" (no line) vs "connected but got no audio" (line
        # prints, byte counter stays at 0). The header dump turns
        # "UAPP won't play" into the renderer's literal request.
        headers = []
        for name in self._DLNA_REQUEST_HEADERS:
            value = self.headers.get(name)
            if value is not None:
                headers.append(f"{name}={value}")
        suffix = f" [{', '.join(headers)}]" if headers else ""
        print(
            f"[http_stream] {method} {self.path} from "
            f"{self.client_address[0]}{suffix}",
            flush=True,
        )

    def _send_stream_headers(self, server: "StreamHTTPServer") -> None:
        """Send the response line + headers shared by HEAD and GET.

        Takes the isinstance-validated server so it never reaches
        through the unnarrowed self.server for the session config.

        DLNA: always 206 + Content-Length + Content-Range, never
        chunked. Strict renderers (UAPP) do a plain GET without a
        Range header and still need Content-Length for SEEK_END during
        decoder-init. Chunked → contentLength=-1 → seek fails.

        Cast: plain 200 + chunked transfer, unchanged.
        """
        # Body offset the do_GET loop starts serving from. DLNA
        # parse of Range: bytes=N- updates this to N so the body
        # actually starts at N (not 0). Cast path leaves it at 0.
        self._range_start = 0
        if server.dlna:
            range_hdr = self.headers.get("Range", "")
            start = 0
            if range_hdr.startswith("bytes="):
                try:
                    start = int(range_hdr[6:].split("-")[0] or 0)
                except ValueError:
                    start = 0
            self._range_start = start
            self.send_response(206)
            self.send_header(
                "Content-Range",
                f"bytes {start}-{_STREAM_SYNTHETIC_TOTAL - 1}/{_STREAM_SYNTHETIC_TOTAL}",
            )
            self.send_header("Content-Length", str(_STREAM_SYNTHETIC_TOTAL - start))
            self._chunked = False
            self.send_header("Accept-Ranges", "bytes")
        else:
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self._chunked = True
        self.send_header("Content-Type", server.content_type)
        if server.dlna:
            self.send_header("transferMode.dlna.org", "Streaming")
            if self.headers.get("getcontentFeatures.dlna.org"):
                self.send_header(
                    "contentFeatures.dlna.org", DLNA_CONTENT_FEATURES
                )
        self.send_header("Connection", "close")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib API
        # Some receivers (and plenty of middleboxes) probe headers
        # with a HEAD before GET to check Content-Type and confirm
        # the stream exists. Answer with the same response line GET
        # uses so they don't fall back or abort.
        self._log_connection("HEAD")
        server = self.server  # type: ignore[assignment]
        if not isinstance(server, StreamHTTPServer) or server.buffer is None:
            self.send_error(503, "stream session not ready")
            return
        if self._request_path() != server.stream_path:
            self.send_error(404, "not found")
            return
        self._send_stream_headers(server)

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        self._log_connection("GET")
        server = self.server  # type: ignore[assignment]
        if not isinstance(server, StreamHTTPServer) or server.buffer is None:
            self.send_error(503, "stream session not ready")
            return
        if self._request_path() != server.stream_path:
            self.send_error(404, "not found")
            return
        buf = server.buffer
        # Parse ?ts= from the request to detect stale requests from
        # a previous track. UAPP sometimes sends a Range request to
        # an old URL after a track change — if we let it reach
        # Phase 2/3, skip_to() succeeds (buffer has fresh data) and
        # set_ring_active() steals the ring lock, leaving the
        # legitimate new-track connection stuck in head-only mode.
        # Instead of rejecting with 410 (which breaks UAPP's
        # recovery path), serve from head cache only and return
        # without ever attaching as a consumer. UAPP gets the data
        # it needs (or a partial response) while the ring lock
        # stays free for the real connection.
        _parsed = urllib.parse.urlsplit(self.path)
        _req_ts = int(urllib.parse.parse_qs(_parsed.query).get("ts", [0])[0])
        _cur_id = buf.track_id
        _stale = bool(_req_ts and _cur_id and _req_ts != _cur_id)
        # Wait for the encoder to have written at least the FLAC
        # header before sending any response. If we send the 206
        # with Content-Length before data is available, the
        # renderer's HTTP client reads 0 bytes, closes the
        # connection, and enters a seek-thrash loop.
        if not buf.data_ready.wait(timeout=10.0):
            self.send_error(503, "stream data not ready")
            return
        self._send_stream_headers(server)
        # Set a socket write timeout so that _write_chunk doesn't
        # block for minutes when the receiver stops reading the
        # socket (UAPP pauses reading for ~3.5 min after each seek).
        # Without this, the TCP send buffer fills, write() blocks,
        # and the stream hangs until UAPP's OkHttp times out and
        # issues a new Range request — by which time the RingBuffer
        # has moved past the seek target and the connection dies
        # with an avcodec error.
        self.request.settimeout(30.0)
        # Stale request from a previous track's URL. Serve from head
        # cache only (non-destructive) and return without calling
        # attach(). UAPP gets whatever data is available at the
        # requested offset without ever calling set_ring_active(),
        # so the legitimate new-track connection keeps the ring.
        if _stale:
            _stale_sent = self._range_start
            while _stale_sent < buf.head_size:
                _chunk = buf.read_head(_stale_sent, 16384)
                if not _chunk:
                    break
                self._write_chunk(_chunk)
                _stale_sent += len(_chunk)
            print(
                f"[http_stream] STALE start={self._range_start} "
                f"sent={_stale_sent} from {self.client_address[0]}",
                flush=True,
            )
            return
        # Become a consumer. If a main streaming connection is
        # already in Phase 3 (RingBuffer), we become "head-only":
        # same gen, but we read only from head cache and never
        # touch the destructive RingBuffer. This lets UAPP's
        # probe/init connections coexist with the streaming one.
        # If the Range start is beyond the head cache, this is a real
        # seek (not a probe). Force-supersede so we become the main
        # consumer and can read from the RingBuffer. Without this,
        # head_only connections return 0 bytes when UAPP seeks beyond
        # head cache size → "unexpected end of stream" → crash.
        # For DLNA connections, force-supersede when the Range start
        # is beyond the head cache (a genuine seek, not a probe).
        # For Cast connections, always force-supersede so reconnects
        # pick up the live edge instead of becoming head-only.
        _force = not server.dlna or self._range_start >= buf.head_size
        my_gen, head_only = buf.attach(force=_force)
        # Start serving at the Range offset parsed in
        # _send_stream_headers (0 if no Range header was sent).
        bytes_sent = self._range_start
        bytes_from_head = 0
        bytes_from_ring = 0
        _log_start = bytes_sent
        end_reason = "closed"
        try:
            if server.dlna:
                # Phase 1 (DLNA only) — serve from head cache
                # (non-destructive), starting at the Range offset.
                # Multiple connections can read the same head cache
                # without conflict; this gives UAPP's probe and
                # decoder-init connections access to bytes
                # 0..head_size without going through the destructive
                # RingBuffer. Cast reconnects skip this: they should
                # start at the live edge, not replay the intro.
                while bytes_sent < buf.head_size:
                    if buf.is_superseded(my_gen):
                        end_reason = "superseded"
                        if self._chunked:
                            self._write_chunk(b"")
                        return
                    chunk = buf.read_head(bytes_sent, 16384)
                    if not chunk:
                        break
                    self._write_chunk(chunk)
                    bytes_sent += len(chunk)
                    bytes_from_head += len(chunk)
                # Phase 2/3 (DLNA only) — head-only connections
                # (UAPP probe/init) stop here: they've served what
                # they need from head cache and exit cleanly.
                if head_only:
                    if self._chunked:
                        self._write_chunk(b"")
                    end_reason = "head_only_done"
                    return
                # Phase 2 — align the RingBuffer to bytes_sent.
                # Always call (not only when > 0):
                #   start=0, head served 2MB → skip to 2MB
                #   start=192KB, head served 1.8MB → skip to 2MB
                #   start=3MB, head served 0 bytes → skip to 3MB
                if not buf.skip_to(bytes_sent, gen=my_gen, timeout=5.0):
                    # Seek position unreachable — the RingBuffer has
                    # already discarded that data or the encoder can't
                    # keep up. Close the connection so UAPP doesn't
                    # receive data from the wrong stream offset.
                    end_reason = "skip_failed"
                    if self._chunked:
                        self._write_chunk(b"")
                    return
                # Mark ourselves as the active RingBuffer consumer.
                # From now on, attach() won't supersede us — new
                # callers become head-only instead. Cast reconnects
                # skip this so they always supersede the old consumer.
                buf.set_ring_active(my_gen)
            # Phase 3 — stream from RingBuffer (destructive).
            # For DLNA, this runs after Phase 2 and set_ring_active.
            # For Cast, it runs directly — no head cache, no skip_to,
            # no ring lock — preserving the old supersede-and-serve-
            # live semantics that Cast reconnects depend on.
            while True:
                if buf.is_superseded(my_gen):
                    end_reason = "superseded"
                    if self._chunked:
                        self._write_chunk(b"")
                    return
                chunk = buf.read(16384, timeout=2.0, gen=my_gen)
                if not chunk:
                    if buf.is_source_done or buf.is_closed or buf.is_superseded(my_gen):
                        end_reason = "source_done" if buf.is_source_done else ("closed" if buf.is_closed else "superseded")
                        if self._chunked:
                            self._write_chunk(b"")
                        return
                    continue
                self._write_chunk(chunk)
                bytes_from_ring += len(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            end_reason = "broken_pipe"
            return
        finally:
            # CRITICAL: clear _ring_active so the next attach() can
            # become the new main consumer. Without this, _ring_active
            # stays True forever after the first connection breaks,
            # and ALL future connections become head-only — no one
            # ever reads from the RingBuffer and the stream dies.
            if not head_only:
                buf.clear_ring_active(my_gen)
            print(
                f"[http_stream] SERVED start={_log_start} "
                f"head={bytes_from_head} ring={bytes_from_ring} "
                f"total={bytes_from_head + bytes_from_ring} "
                f"end={end_reason} "
                f"{'FORCE ' if _force else ''}"
                f"{'HEADONLY ' if head_only else ''}"
                f"from {self.client_address[0]}",
                flush=True,
            )

    def _write_chunk(self, data: bytes) -> None:
        """Write one frame of the HTTP response body.

        When streaming to a DLNA Range request (Content-Length mode,
        ``self._chunked is False``), write raw bytes directly. When
        streaming to a regular Cast or non-range DLNA request (chunked
        mode), wrap the data in HTTP chunked-transfer framing so the
        receiver knows the stream is open-ended.
        """
        if self._chunked:
            header = f"{len(data):x}\r\n".encode()
            self.wfile.write(header)
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
        else:
            self.wfile.write(data)
        self.wfile.flush()


def start_stream_http_server(
    buffer: RingBuffer,
    stream_path: str = "/stream",
    content_type: str = "audio/flac",
    dlna: bool = False,
) -> StreamHTTPServer:
    """Start a stream-serving HTTP listener on an ephemeral port.

    Returns the server object so the caller can pull `server_address`
    out and build the URL handed to the receiver. Caller is
    responsible for `shutdown()` + `server_close()` when the session
    ends.

    Set `dlna=True` for UPnP/DLNA sessions so the handler emits the
    DLNA-specific response headers (transferMode.dlna.org). Cast
    sessions leave it False; Chromecast isn't DLNA and shouldn't see
    those headers.
    """
    server = StreamHTTPServer(("0.0.0.0", 0), _StreamRequestHandler)
    server.buffer = buffer
    server.stream_path = stream_path
    server.content_type = content_type
    server.dlna = dlna
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"http-stream{stream_path}",
        daemon=True,
    )
    thread.start()
    return server


def primary_lan_ip() -> str:
    """Best-effort local IP address for a LAN receiver to reach
    back to this machine.

    Connecting a UDP socket to a public address without sending
    forces the OS to populate the socket's source address, which
    gives us the right interface. Falls back to 127.0.0.1 if
    something blocks the lookup; that won't work for a real
    receiver but keeps the app from crashing on disconnected
    networks.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass
