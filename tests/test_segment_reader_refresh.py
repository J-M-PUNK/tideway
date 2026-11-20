"""SegmentReader recovery from a mid-track segment failure.

An expired signed URL or a dropped connection used to propagate out of
libav into the decoder thread, which ended the track early and let the
queue advance: the "songs randomly skip" report. With a refresher the
reader re-resolves the links once and downloads the failed segment
again, so the byte stream continues exactly where it broke.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from app.audio.segment_reader import SegmentReader

SEGMENT_BYTES = 4096


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        srv = self.server
        with srv.lock:  # type: ignore[attr-defined]
            srv.requests.append(self.path)  # type: ignore[attr-defined]
            fail = self.path in srv.fail  # type: ignore[attr-defined]
        if fail:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Body depends only on the segment, not the signature: the same
        # file served from new links.
        seg = self.path.split("?", 1)[0]
        body = seg.encode().ljust(SEGMENT_BYTES, b".")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        return


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.lock = threading.Lock()  # type: ignore[attr-defined]
    srv.requests = []  # type: ignore[attr-defined]
    srv.fail = set()  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _urls(srv, media: int, sig: str) -> list[str]:
    host, port = srv.server_address
    base = f"http://{host}:{port}"
    return [f"{base}/init?sig={sig}"] + [
        f"{base}/seg{i}?sig={sig}" for i in range(1, media + 1)
    ]


def _expected(media: int, first: int = 1) -> bytes:
    out = b"/init".ljust(SEGMENT_BYTES, b".")
    for i in range(first, media + 1):
        out += f"/seg{i}".encode().ljust(SEGMENT_BYTES, b".")
    return out


def _read_all(reader: SegmentReader) -> bytes:
    out = bytearray()
    while True:
        chunk = reader.read(SEGMENT_BYTES)
        if not chunk:
            return bytes(out)
        out.extend(chunk)


def test_expired_links_mid_track_are_refreshed(server):
    """Segments 3+ have expired on the old signature. The stream must
    come out byte-identical, with one refresh."""
    server.fail = {f"/seg{i}?sig=old" for i in range(3, 6)}
    refreshes = []

    def _refresh():
        refreshes.append(1)
        return _urls(server, 5, "new")

    reader = SegmentReader(_urls(server, 5, "old"), refresh_urls=_refresh)
    assert _read_all(reader) == _expected(5)
    assert refreshes == [1]
    assert "/seg3?sig=new" in server.requests
    assert "/seg5?sig=new" in server.requests


def test_seek_reader_maps_onto_the_full_list(server):
    """A seek reader holds [init] + urls[1 + offset:]. Its segments must
    be taken from the same positions of the refreshed full list."""
    server.fail = {"/seg5?sig=old"}
    full_old = _urls(server, 6, "old")
    reader = SegmentReader(
        full_old[:1] + full_old[1 + 3:],
        refresh_urls=lambda: _urls(server, 6, "new"),
        media_offset=3,
    )
    assert _read_all(reader) == _expected(6, first=4)
    assert "/seg5?sig=new" in server.requests


def test_no_refresher_keeps_failing(server):
    server.fail = {"/seg2?sig=old"}
    reader = SegmentReader(_urls(server, 3, "old"))
    with pytest.raises(requests.RequestException):
        _read_all(reader)


def test_second_failure_on_fresh_links_propagates(server):
    """One retry per failed segment: if the new links fail too, it is an
    outage, and the error reaches the decoder."""
    server.fail = {"/seg2?sig=old", "/seg2?sig=new"}
    reader = SegmentReader(
        _urls(server, 3, "old"),
        refresh_urls=lambda: _urls(server, 3, "new"),
    )
    with pytest.raises(requests.HTTPError):
        _read_all(reader)


def test_refreshed_layout_mismatch_is_refused(server):
    """A different segment count means a different file; splicing it in
    would corrupt the stream, so the reader refuses."""
    server.fail = {"/seg2?sig=old"}
    reader = SegmentReader(
        _urls(server, 3, "old"),
        refresh_urls=lambda: _urls(server, 4, "new"),
    )
    with pytest.raises(RuntimeError, match="segments"):
        _read_all(reader)


def test_close_is_not_retried(server):
    """close() aborts downloads by closing their responses, which the
    downloading thread sees as a connection error. That must stay an
    exit, never trigger a refresh."""
    refreshes = []
    reader = SegmentReader(
        _urls(server, 3, "old"),
        refresh_urls=lambda: refreshes.append(1) or _urls(server, 3, "new"),
    )
    reader.close()
    aborted = requests.ConnectionError("response closed by close()")
    with pytest.raises(requests.ConnectionError):
        reader._recover_segment(1, reader._url_gen, aborted)  # type: ignore[attr-defined]
    assert refreshes == []
