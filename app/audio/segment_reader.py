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
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

import requests

from app.http import STREAM_SESSION

# libav commonly asks for 32KB at a time. Segments are ~2-3MB each,
# so one segment covers many reads. We fetch one segment per
# cache-miss and append to a rolling bytes buffer, with the segment
# after it already downloading (see _start_read_ahead).

# Runs the one-segment read-ahead for every live reader: the playing
# track's decoder, a buffering preload, a seek's fresh reader, a DLNA
# passthrough. Each holds at most one fetch here, so this only has to
# be wider than the number of readers alive at once.
_READ_AHEAD_POOL = ThreadPoolExecutor(
    max_workers=16, thread_name_prefix="segment-ahead"
)


class SegmentReader(io.RawIOBase):
    def __init__(
        self,
        urls: list[str],
        prefetched: Optional[dict[int, bytes]] = None,
    ):
        if not urls:
            raise ValueError("SegmentReader: empty url list")
        self._urls = list(urls)
        self._next_segment_idx = 0
        # Bytes we've fetched so far, flat. Grows as segments arrive.
        self._buf = bytearray()
        self._pos = 0
        self._lock = threading.Lock()
        # Cancellation plumbing. The decoder thread blocks inside
        # _fetch_next_segment for ~150-300ms per segment on a track
        # change or seek. The player tears the thread down by calling
        # close() from the foreground. Without active cancellation,
        # close() would have to wait for the in-flight HTTP read to
        # complete on its own, which is the bulk of the visible
        # teardown latency on the play and seek paths. We track every
        # open Response (the segment being read plus the read-ahead)
        # and close them from close() to abort the reads; the threads
        # see a connection error and exit.
        self._closed = False
        self._open_responses: set[requests.Response] = set()
        # Separate, finer-grained lock for the response handles so
        # close() can preempt a fetch in progress without waiting on
        # the main read lock (which the fetching thread is holding).
        self._response_lock = threading.Lock()
        # The next segment, downloading while libav reads this one:
        # (segment index, future of its bytes, or of None if the
        # reader closed mid-download).
        self._ahead: Optional[tuple[int, Future]] = None
        # Byte-level prefetch fast path: if the caller hands us
        # pre-downloaded bytes for segment 0, 1, ..., N in order,
        # seed the buffer with them and advance _next_segment_idx
        # past them. The decoder's av.open call then reads through
        # those bytes without touching the network at all, which
        # is the whole point of byte-level prefetch.
        #
        # Only accept a contiguous prefix starting at 0 — a gap
        # (we have 0 but not 1) means a half-prefetch that we
        # can't use directly. Fall back to eager init fetch in
        # that case.
        seeded_any = False
        if prefetched:
            idx = 0
            while idx in prefetched and idx < len(self._urls):
                chunk = prefetched[idx]
                if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                    break
                self._buf.extend(chunk)
                self._next_segment_idx = idx + 1
                idx += 1
                seeded_any = True
        # Eager-fetch the init segment so callers that only probe
        # headers (av.open's stream-probing phase) don't get EOF —
        # unless prefetch already seeded it. av.open needs the first
        # media segment straight after the init segment, so start that
        # one first and let the two download side by side: the start of
        # a track then costs one round-trip instead of two.
        if not seeded_any:
            self._start_read_ahead(1)
            self._fetch_next_segment()
        else:
            self._start_read_ahead(self._next_segment_idx)

    # --- io.RawIOBase interface ------------------------------------

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        # Report non-seekable so libav treats the source as a live
        # stream. Otherwise libav scans every moof atom in the
        # fragmented MP4 during av.open() to build a seek table,
        # which at hi-res is dozens of segment fetches and the bulk
        # of the startup latency we're trying to kill.
        #
        # Backward / tell-me-the-size seeks that libav would have
        # done during probing are skipped in this mode, so the
        # decoder returns from av.open after the first media
        # segment's header has been read. In-track user scrubbing
        # is driven by PCMPlayer recreating the Decoder at the
        # target segment rather than calling container.seek, so
        # losing seekability on the file object does not cost us
        # the scrubbing feature.
        return False

    def writable(self) -> bool:
        return False

    def close(self) -> None:
        # Idempotent close. The decoder's cancel_source() path calls
        # this from the foreground thread to abort an in-flight fetch
        # on the decoder thread; close() is also called normally when
        # the decoder is torn down after the thread has already exited.
        super().close()
        with self._response_lock:
            self._closed = True
            responses = list(self._open_responses)
            self._open_responses.clear()
        for r in responses:
            # Closing a Response mid-body drops its urllib3 connection
            # rather than returning it to the shared pool, and raises a
            # ConnectionError on the thread blocked in iter_content.
            # That's the whole point: the decoder thread sees the
            # exception and exits, instead of waiting out the rest of a
            # 150-300ms segment download. The session itself is shared
            # with every other reader, so it stays open.
            try:
                r.close()
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
            # Cap how far past the end of our fetched buffer we're
            # willing to fetch to satisfy a single read. Legitimate
            # forward-linear reads land at (or 1 byte past) the tail
            # and are served by fetching the next segment or two.
            # A seek near SEEK_END lands megabytes past the tail and
            # would otherwise fetch every remaining segment trying
            # to reach the position — that's the bug that used to
            # pull the whole track before av.open returned.
            _MAX_CATCHUP = 8 * 1024 * 1024  # 8 MB, ~3 hi-res segments
            if self._pos > len(self._buf) + _MAX_CATCHUP:
                return b""
            # Fetch segments until the read range is fully in buf —
            # covers sequential-playback reads and any backward
            # seeks to bytes inside the already-fetched region.
            needed = self._pos + size
            while needed > len(self._buf) and self._next_segment_idx < len(self._urls):
                self._fetch_next_segment()
            # If pos is still past the buffer after all fetches
            # (segments ran out), signal EOF cleanly.
            if self._pos >= len(self._buf):
                return b""
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
                # libav calls SEEK_END during av.open() to find a
                # trailing moov box and caches the returned position
                # as the stream's total size. If we report our
                # tiny fetched-so-far number, libav stops reading
                # after the init segment and playback dies two
                # seconds in. If we report the real end, we'd have
                # to fetch every segment — also bad.
                #
                # Compromise: report a stable far-future value so
                # libav never caps us at playback time. read() has
                # its own gap-too-large guard that catches the
                # "libav seeks near the fake end to probe for a
                # trailing moov" pattern and returns EOF without
                # fetching the whole track.
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
        if self._closed:
            # close() was called between segments. Mark EOF so the
            # caller's read() returns b"" cleanly instead of trying
            # the next segment.
            self._next_segment_idx = len(self._urls)
            return
        idx = self._next_segment_idx
        ahead = self._ahead
        if ahead is not None and ahead[0] == idx:
            # Already downloading (or done) on the read-ahead pool. A
            # failed download re-raises here, exactly as an inline
            # fetch would have.
            self._ahead = None
            data = ahead[1].result()
        else:
            # The init segment, fetched while the read-ahead holds the
            # first media segment — leave that one running.
            data = self._download(idx)
        if data is None:
            # Closed mid-download. _next_segment_idx is left short of
            # the end so a subsequent read() would re-attempt — but in
            # practice close() means the SegmentReader is on its way
            # out and read() won't be called again.
            return
        self._buf.extend(data)
        self._next_segment_idx += 1
        self._start_read_ahead(self._next_segment_idx)

    def _start_read_ahead(self, idx: int) -> None:
        """Start downloading segment `idx` in the background, so it's
        already here (or close) when libav asks for it. One segment
        ahead and no more: that hides each segment's round-trip behind
        the one being decoded, which is the same single-request
        overlap the downloader uses, and keeps this reader to at most
        two concurrent requests for its track."""
        if self._closed or self._ahead is not None or idx >= len(self._urls):
            return
        self._ahead = (idx, _READ_AHEAD_POOL.submit(self._download, idx))

    def _download(self, idx: int) -> Optional[bytes]:
        """GET segment `idx`. Returns None if close() lands first.

        Runs on the reading thread or the read-ahead pool, so it never
        touches the buffer or the segment index; the caller does.
        """
        url = self._urls[idx]
        # Tidal CDN URLs are signed with time-bounded policy params,
        # so this is the only place we hit the network.
        #
        # `stream=True` is the part that makes the read interruptible.
        # Without it, requests internally calls Response.content,
        # which blocks until the full body has been buffered and
        # cannot be preempted. With stream=True we hold the Response
        # ourselves and iter_content yields chunks as the socket
        # delivers them; close() can then abort the active socket
        # mid-fetch by closing the Response.
        t0 = time.monotonic()
        r = STREAM_SESSION.get(url, timeout=30, stream=True)
        with self._response_lock:
            if self._closed:
                # Lost the race: close() fired after we issued the GET.
                try:
                    r.close()
                except Exception:
                    pass
                return None
            self._open_responses.add(r)
        chunks: list[bytes] = []
        size = 0
        try:
            r.raise_for_status()
            # 64 KB is large enough to keep iter_content overhead
            # negligible on a 2-3 MB segment but small enough that
            # close() during a fetch interrupts within the time to
            # download one chunk (~5 ms on a residential connection).
            for chunk in r.iter_content(chunk_size=65536):
                if self._closed:
                    return None
                if chunk:
                    chunks.append(chunk)
                    size += len(chunk)
        finally:
            with self._response_lock:
                # close() may have already taken it out of the set and
                # closed it itself.
                self._open_responses.discard(r)
            try:
                r.close()
            except Exception:
                pass
        if self._closed:
            return None
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        print(
            f"[perf] segment idx={idx} size={size}B elapsed={elapsed_ms:.0f}ms",
            file=sys.stderr,
            flush=True,
        )
        return b"".join(chunks)
