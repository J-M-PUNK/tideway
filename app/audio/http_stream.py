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

    def __init__(self, max_bytes: int = 8 * 1024 * 1024) -> None:
        self._max = max_bytes
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._closed = False
        # Single-active-consumer guard. read() is destructive (it
        # deletes the bytes it returns), so two HTTP serve threads on
        # the same buffer would split the FLAC stream and corrupt
        # both. The Cast track-change reload issues a fresh play_media
        # per track, and the receiver's new GET can briefly overlap
        # the old connection. attach() bumps this generation; an
        # older serve loop sees it move and exits, leaving exactly
        # one consumer.
        self._gen = 0

    def attach(self) -> int:
        """Register as the current consumer and supersede any prior
        one. Returns this consumer's generation token; pass it to
        read()/is_superseded(). Wakes a blocked older reader so it
        notices it's been superseded promptly instead of after its
        next idle timeout."""
        with self._cv:
            self._gen += 1
            self._cv.notify_all()
            return self._gen

    def is_superseded(self, gen: int) -> bool:
        """True once a newer consumer has attach()ed. The serve loop
        checks this each iteration and returns, so a stale connection
        stops draining the shared buffer."""
        return gen != self._gen

    def write(self, data: bytes) -> None:
        with self._cv:
            if self._closed:
                return
            overflow = len(self._buf) + len(data) - self._max
            if overflow > 0:
                # Silent drops mask a stalled receiver. Log at
                # debug so the first real-hardware test surfaces
                # the fact that bytes are being lost.
                log.debug(
                    "ring buffer overflow: dropping %d bytes", overflow
                )
                del self._buf[:overflow]
            self._buf.extend(data)
            self._cv.notify_all()

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
            return chunk

    def flush(self) -> None:
        """Drop all buffered bytes, keep the stream open.

        Used on a Cast track-change reset: the encoder is rebuilt so
        a fresh FLAC header leads the stream, and the receiver's new
        connection then starts at the live edge instead of replaying
        the seconds of backlog that pile up FIFO. That backlog is the
        permanent latency floor today.
        """
        with self._cv:
            self._buf.clear()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def is_closed(self) -> bool:
        return self._closed


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
        """Send the 200 + headers shared by HEAD and GET.

        Takes the isinstance-validated server so it never reaches
        through the unnarrowed self.server for the session config.
        """
        self.send_response(200)
        self.send_header("Content-Type", server.content_type)
        # Chunked transfer lets us keep writing audio frames as long
        # as the session is alive. Tells the receiver it's a stream,
        # not a file with a known length.
        self.send_header("Transfer-Encoding", "chunked")
        if server.dlna:
            # DLNA's transferMode.dlna.org negotiation: a renderer asks
            # for a mode and a compliant server states the mode it
            # granted. We always serve an open-ended FLAC, which IS
            # Streaming, so we grant Streaming regardless of what was
            # requested rather than echoing the request value back
            # (echoing would both claim a mode we don't honor and
            # reflect an unvalidated client string into a response
            # header). Cast receivers aren't DLNA and never see this.
            self.send_header("transferMode.dlna.org", "Streaming")
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
        self._send_stream_headers(server)
        buf = server.buffer
        # Become the sole consumer. A later GET (the Cast per-track
        # reload's new connection) supersedes this one so two threads
        # never drain the destructive buffer at once.
        my_gen = buf.attach()
        try:
            while True:
                if buf.is_superseded(my_gen):
                    # A newer connection took over. End this stream
                    # cleanly and stop reading the shared buffer.
                    self._write_chunk(b"")
                    return
                chunk = buf.read(16384, timeout=2.0, gen=my_gen)
                if not chunk:
                    # Idle read with a closed buffer ends the stream.
                    if buf.is_closed or buf.is_superseded(my_gen):
                        self._write_chunk(b"")
                        return
                    continue
                self._write_chunk(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Receiver disconnected / stopped pulling. Clean exit;
            # the session-end path will tear down the server.
            return

    def _write_chunk(self, data: bytes) -> None:
        """Write one HTTP chunked-transfer frame."""
        header = f"{len(data):x}\r\n".encode()
        self.wfile.write(header)
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
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
