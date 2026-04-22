"""File-like wrapper over a list of Tidal DASH segment URLs.

libav's DASH demuxer rejects Tidal's MPD manifests (the Phase 1
probe proved this across all quality tiers). But tidalapi already
pre-resolves each MPD into a flat `urls` list — index 0 is the
init segment, indices 1..N are the fragmented-MP4 media segments.
Concatenating those bytes into a single stream produces a
fragmented MP4 that PyAV reads natively.

`SegmentReader` is the streaming version of that concatenation.
It exposes a read-only file-like interface that fetches each
segment on demand as libav asks for bytes, caches what it's
fetched so backward seeks inside the header region work, and
returns empty bytes at the end. It never loads the whole track
into memory unless libav reads to the end.
"""
from __future__ import annotations

import io
import threading
from typing import Optional

import requests

# libav commonly asks for 32KB at a time. Segments are ~2-3MB each,
# so one segment covers many reads. We fetch one segment per
# cache-miss and append to a rolling bytes buffer.


class SegmentReader(io.RawIOBase):
    def __init__(self, urls: list[str]):
        if not urls:
            raise ValueError("SegmentReader: empty url list")
        self._urls = list(urls)
        self._next_segment_idx = 0
        # Bytes we've fetched so far, flat. Grows as segments arrive.
        self._buf = bytearray()
        self._pos = 0
        self._lock = threading.Lock()
        self._session = requests.Session()
        # Eager-fetch the init segment so callers that only probe
        # headers (av.open's stream-probing phase) don't get EOF.
        self._fetch_next_segment()

    # --- io.RawIOBase interface ------------------------------------

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def close(self) -> None:
        super().close()
        try:
            self._session.close()
        except Exception:
            pass

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        with self._lock:
            if size is None or size < 0:
                # Consume everything. Fetch all remaining segments.
                while self._next_segment_idx < len(self._urls):
                    self._fetch_next_segment()
                out = bytes(self._buf[self._pos:])
                self._pos = len(self._buf)
                return out
            # Fetch segments until the read range is fully in buf —
            # covers both "reading forward past current tail" and
            # "reading after a seek to a byte beyond what's fetched."
            needed = self._pos + size
            while needed > len(self._buf) and self._next_segment_idx < len(self._urls):
                self._fetch_next_segment()
            end = min(needed, len(self._buf))
            out = bytes(self._buf[self._pos:end])
            self._pos = end
            return out

    def readinto(self, b):  # type: ignore[override]
        # Python's io machinery falls back to read() if we don't
        # override this, but av/libav uses readinto for efficiency.
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:  # type: ignore[override]
        with self._lock:
            if whence == io.SEEK_SET:
                target = offset
            elif whence == io.SEEK_CUR:
                target = self._pos + offset
            elif whence == io.SEEK_END:
                # libav asks SEEK_END during av.open() probing. We
                # do NOT fetch every segment to answer it — at
                # hi-res that's ~100MB of eager download before the
                # decoder can even start, which added ~5s to every
                # load(). Instead, report a stable large size; libav
                # uses this as "end of file far away." When reads
                # eventually hit the actual tail of the segments,
                # read() returns empty and libav sees EOF normally.
                #
                # The size is a conservative over-estimate: 250MB
                # covers ~15 minutes of 24-bit/96kHz FLAC or many
                # hours of lossy. Fine-grained accuracy isn't needed
                # — libav doesn't use this for the decoder's notion
                # of track length (that comes from the MP4 moov).
                _FAKE_END = 250 * 1024 * 1024
                target = _FAKE_END + offset
            else:
                raise ValueError(f"unknown whence {whence}")
            if target < 0:
                target = 0
            self._pos = target
            return self._pos

    def tell(self) -> int:  # type: ignore[override]
        return self._pos

    # --- internals -------------------------------------------------

    def _fetch_next_segment(self) -> None:
        if self._next_segment_idx >= len(self._urls):
            return
        url = self._urls[self._next_segment_idx]
        # Tidal CDN URLs are signed with time-bounded policy params,
        # so this is the only place we hit the network. We keep the
        # requests.Session around for HTTP/2 connection reuse.
        r = self._session.get(url, timeout=30)
        r.raise_for_status()
        self._buf.extend(r.content)
        self._next_segment_idx += 1

