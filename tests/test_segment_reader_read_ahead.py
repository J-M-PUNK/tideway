"""SegmentReader start-up latency: connection reuse and read-ahead.

A track's first audio waits on the init segment and then the first
media segment. These pin the two things that keep that short: the
two download side by side rather than one after the other, and a new
reader (the next track, a seek) reuses the pooled CDN connection
instead of paying a fresh handshake. They also pin the limit — never
more than one segment downloading ahead of the one being read — and
that close() still aborts everything in flight.
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.audio.segment_reader import SegmentReader

SEGMENT_BYTES = 4096


class _Handler(BaseHTTPRequestHandler):
    # Keep-alive, like the CDN; HTTP/1.0 would close every connection
    # and hide whether the client reuses them.
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        srv = self.server
        with srv.lock:  # type: ignore[attr-defined]
            srv.in_flight += 1  # type: ignore[attr-defined]
            srv.max_in_flight = max(srv.max_in_flight, srv.in_flight)  # type: ignore[attr-defined]
            srv.client_ports.append(self.client_address[1])  # type: ignore[attr-defined]
        try:
            time.sleep(srv.delays.get(self.path, srv.delay))  # type: ignore[attr-defined]
            body = self.path.encode().ljust(SEGMENT_BYTES, b".")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            with srv.lock:  # type: ignore[attr-defined]
                srv.in_flight -= 1  # type: ignore[attr-defined]

    def log_message(self, *args, **kwargs):
        return


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.lock = threading.Lock()  # type: ignore[attr-defined]
    srv.in_flight = 0  # type: ignore[attr-defined]
    srv.max_in_flight = 0  # type: ignore[attr-defined]
    srv.client_ports = []  # type: ignore[attr-defined]
    srv.delay = 0.3  # type: ignore[attr-defined]
    srv.delays = {}  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    srv.base = f"http://{host}:{port}"  # type: ignore[attr-defined]
    yield srv
    srv.shutdown()
    srv.server_close()


def _urls(srv, n: int) -> list[str]:
    return [f"{srv.base}/seg{i}" for i in range(n)]


def test_init_and_first_media_segment_download_side_by_side(server):
    t0 = time.monotonic()
    reader = SegmentReader(_urls(server, 4))
    # What av.open does: read the init segment and into the first
    # media segment.
    data = reader.read(SEGMENT_BYTES + 16)
    elapsed = time.monotonic() - t0
    reader.close()

    assert data.startswith(b"/seg0")
    assert data[SEGMENT_BYTES:].startswith(b"/seg1"[:16])
    # One 0.3 s round-trip, not two back to back.
    assert elapsed < 0.5


def test_reading_on_finds_the_next_segment_already_downloaded(server):
    reader = SegmentReader(_urls(server, 4))
    reader.read(SEGMENT_BYTES * 2)  # init + first media segment
    # The decoder spends real time on each segment; the next one
    # downloads meanwhile.
    time.sleep(0.4)
    t0 = time.monotonic()
    data = reader.read(SEGMENT_BYTES)
    elapsed = time.monotonic() - t0
    reader.close()

    assert data.startswith(b"/seg2")
    assert elapsed < 0.1


def test_never_more_than_one_segment_ahead(server):
    server.delay = 0.05
    reader = SegmentReader(_urls(server, 6))
    while reader.read(SEGMENT_BYTES):
        pass
    reader.close()

    # The segment being read plus the one downloading ahead of it.
    assert server.max_in_flight <= 2
    assert len(server.client_ports) == 6  # each segment fetched once


def test_a_new_reader_reuses_the_pooled_connection(server):
    server.delay = 0.0
    first = SegmentReader(_urls(server, 1))
    first.read(SEGMENT_BYTES)
    first.close()
    used = set(server.client_ports)

    second = SegmentReader(_urls(server, 1))
    second.read(SEGMENT_BYTES)
    second.close()

    # The next track's (or a seek's) reader rides an existing
    # connection instead of opening a new one.
    assert server.client_ports[-1] in used


def test_close_aborts_a_read_ahead_in_flight(server):
    server.delay = 0.0
    server.delays["/seg1"] = 1.0
    reader = SegmentReader(_urls(server, 4))
    reader.read(SEGMENT_BYTES)  # init only; seg1 is downloading ahead
    ahead = reader._ahead  # type: ignore[attr-defined]
    assert ahead is not None and ahead[0] == 1

    t0 = time.monotonic()
    reader.close()
    # close() never waits on the read-ahead...
    assert time.monotonic() - t0 < 0.1
    # ...which drops whatever it gets instead of handing bytes back
    # for a closed reader.
    assert ahead[1].result(timeout=3.0) is None
    assert reader.read(SEGMENT_BYTES) == b""
