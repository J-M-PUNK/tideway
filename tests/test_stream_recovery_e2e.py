"""End to end: real fragmented-MP4 FLAC, real SegmentReader and Decoder,
served over HTTP, with the signed links expiring mid-track.

Pins the property the recovery relies on: re-fetching a failed segment
from fresh links continues the byte stream so exactly that libav
decodes every sample once, with nothing dropped or repeated.
"""
from __future__ import annotations

import io
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

av = pytest.importorskip("av")

from app.audio.decoder import Decoder  # noqa: E402
from app.audio.segment_reader import SegmentReader  # noqa: E402

RATE = 44100
SECONDS = 12


def _fmp4_flac_segments() -> tuple[list[bytes], np.ndarray]:
    """Encode a ramp signal as FLAC in fragmented MP4 and split it the
    way TIDAL serves it: [init (ftyp+moov), moof+mdat, ...]."""
    t = np.arange(RATE * SECONDS)
    left = ((t % 30000) - 15000).astype(np.int16)  # distinct, sample-exact
    stereo = np.stack([left, -left])
    buf = io.BytesIO()
    out = av.open(
        buf, "w", format="mp4",
        options={
            "movflags": "frag_keyframe+empty_moov+default_base_moof",
            "frag_duration": "1000000",
        },
    )
    st = out.add_stream("flac", rate=RATE)
    st.layout = "stereo"
    for i in range(0, stereo.shape[1], 4096):
        fr = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(stereo[:, i:i + 4096].T).reshape(1, -1),
            format="s16", layout="stereo",
        )
        fr.sample_rate = RATE
        fr.pts = i
        for pkt in st.encode(fr):
            out.mux(pkt)
    for pkt in st.encode(None):
        out.mux(pkt)
    out.close()
    data = buf.getvalue()

    boxes, pos = [], 0
    while pos < len(data):
        size, typ = struct.unpack(">I4s", data[pos:pos + 8])
        boxes.append((typ, data[pos:pos + size]))
        pos += size
    init = b"".join(b for typ, b in boxes if typ in (b"ftyp", b"moov"))
    segments, cur = [init], b""
    for typ, b in boxes:
        if typ in (b"ftyp", b"moov", b"mfra"):
            continue
        cur += b
        if typ == b"mdat":
            segments.append(cur)
            cur = b""
    return segments, left


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        srv = self.server
        path, _, query = self.path.partition("?")
        idx = int(path.rsplit("/", 1)[1])
        if query == "sig=old" and idx >= srv.expire_from:  # type: ignore[attr-defined]
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = srv.segments[idx]  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **k):
        return


def test_track_decodes_whole_across_expired_links():
    segments, expected_left = _fmp4_flac_segments()
    assert len(segments) > 6
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.segments = segments  # type: ignore[attr-defined]
    srv.expire_from = len(segments) // 2  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address

        def urls(sig):
            return [
                f"http://{host}:{port}/seg/{i}?sig={sig}"
                for i in range(len(segments))
            ]

        refreshes = []
        reader = SegmentReader(
            urls("old"),
            refresh_urls=lambda: refreshes.append(1) or urls("new"),
        )
        decoder = Decoder(reader)
        chunks = []
        while True:
            pcm = decoder.next_pcm()
            if pcm is None:
                break
            chunks.append(pcm)
        decoded = np.concatenate(chunks)
    finally:
        srv.shutdown()
        srv.server_close()

    assert refreshes == [1]
    assert decoded.shape[0] == expected_left.shape[0]
    assert np.array_equal(decoded[:, 0], expected_left)
