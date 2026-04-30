"""Short-TTL cache for resolved Tidal manifests.

Keyed by `(track_id, quality)`. Each entry stores the segment-URL list
plus the duration + StreamInfo for the manifest, plus an optional
byte-level segment cache so repeat plays can skip the network entirely
on the init + first media segments.

Owned by `PCMPlayer` but extracted because the cache logic is
self-contained — TTL expiry, FIFO eviction under a memory cap, hit/
miss counters, snapshot-for-stats — and tying it to the player's
threading model would invite drift the way the four swap paths drifted.

`StreamInfo` is held opaquely (`Any`); the cache doesn't read its
fields. Type stays strong at player.py's API surface.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional


# Tidal's signed CDN URLs expire ~3 minutes after issue. Beyond that
# the cached manifest's URLs return 403 from CloudFront, so anything
# older than this is worse than re-resolving.
_DEFAULT_TTL_SECONDS = 180.0

# Rolling byte-level memory cap. Each cached bytes_map can run
# ~1 MB at max quality (init + first media segment). 30 MB fits
# ~30 max-quality tracks or hundreds at low quality.
_DEFAULT_BYTES_CAP = 30 * 1024 * 1024

# Soft entry-count ceiling. When exceeded on `store`, we sweep
# expired entries opportunistically; keeps the cache bounded
# without a background janitor thread.
_ENTRY_SWEEP_THRESHOLD = 128


# Cache value layout. Kept as a tuple so `lookup` can hand the caller
# a copy without paying for object construction on the hot path.
#   urls:       list[str]   — DASH segment URLs, [0] = init, [1..] = media
#   duration:   Optional[float] — seconds, None if Tidal didn't report it
#   info:       Any (StreamInfo) — opaque, passed back to caller
#   cached_at:  float — time.monotonic() at insertion
#   bytes_map:  dict[int, bytes] — pre-fetched segment bytes by index
_Entry = tuple[list[str], Optional[float], Any, float, dict[int, bytes]]


class ManifestCache:
    """Thread-safe (track_id, quality) → manifest cache.

    All public methods take their own lock; callers don't need to
    coordinate. The lock is a plain `threading.Lock` (not RLock) so
    accidental re-entry from inside a method is a fail-fast bug, not
    a silent deadlock-of-the-future.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        bytes_cap: int = _DEFAULT_BYTES_CAP,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, Optional[str]], _Entry] = {}
        self._ttl = ttl_seconds
        self._bytes_cap = bytes_cap
        self._hits = 0
        self._misses = 0
        self._bytes = 0

    def lookup(
        self, key: tuple[str, Optional[str]]
    ) -> Optional[tuple[list[str], Optional[float], Any, dict[int, bytes]]]:
        """Return a fresh copy of the entry, or None on miss / expiry.

        Returns copies of `urls` and `bytes_map` so the caller can
        mutate freely without poisoning the cache.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            urls, duration, info, cached_at, bytes_map = entry
            if now - cached_at > self._ttl:
                self._bytes -= sum(len(v) for v in bytes_map.values())
                self._entries.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            return list(urls), duration, info, dict(bytes_map)

    def store(
        self,
        key: tuple[str, Optional[str]],
        urls: list[str],
        duration: Optional[float],
        info: Any,
    ) -> None:
        """Insert / refresh a manifest entry.

        Preserves any pre-fetched bytes from a prior prefetch for the
        same key — re-resolving the manifest doesn't wipe warmed
        segments, so a hover-prefetch followed by a real play stays
        warm end-to-end.
        """
        with self._lock:
            existing = self._entries.get(key)
            existing_bytes = existing[4] if existing is not None else {}
            self._entries[key] = (
                urls, duration, info, time.monotonic(), existing_bytes,
            )
            # Sweep expired siblings opportunistically while we have
            # the lock — keeps memory bounded without a janitor
            # thread.
            if len(self._entries) > _ENTRY_SWEEP_THRESHOLD:
                cutoff = time.monotonic() - self._ttl
                stale = [
                    k for k, v in self._entries.items() if v[3] < cutoff
                ]
                for k in stale:
                    dropped = self._entries.pop(k, None)
                    if dropped is not None:
                        self._bytes -= sum(
                            len(v) for v in dropped[4].values()
                        )

    def update_bytes(
        self,
        key: tuple[str, Optional[str]],
        new_bytes: dict[int, bytes],
    ) -> None:
        """Merge pre-fetched segment bytes into an existing entry.

        Only stores bytes for entries we already have URLs for — a
        no-op if the URLs got evicted mid-prefetch. Runs the FIFO
        eviction pass afterwards so a burst of prefetch can't blow
        past the memory cap.
        """
        if not new_bytes:
            return
        with self._lock:
            entry = self._entries.get(key)
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
            self._entries[key] = (urls, duration, info, cached_at, merged)
            self._bytes += added
            self._evict_bytes_over_cap_locked()

    def stats(self) -> dict:
        """Snapshot for the /api/player/cache-stats endpoint.

        Read while testing the prefetch path to confirm hovers /
        album-mount prefetches are landing.
        """
        with self._lock:
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
                    self._entries.items()
                )
            ]
            return {
                "hits": self._hits,
                "misses": self._misses,
                "ttl_seconds": int(self._ttl),
                "size": len(self._entries),
                "bytes_cached": self._bytes,
                "bytes_cap": self._bytes_cap,
                "entries": entries,
            }

    def _evict_bytes_over_cap_locked(self) -> None:
        """FIFO-evict byte-level entries until we're under the cap.

        Drops bytes only, preserves the URL/manifest metadata so
        subsequent plays still skip the Tidal round-trips. Caller
        holds the lock.
        """
        if self._bytes <= self._bytes_cap:
            return
        # Python 3.7+ dict preserves insertion order — pop in
        # insertion order until we're back under the cap.
        for key in list(self._entries.keys()):
            urls, duration, info, cached_at, bytes_map = self._entries[key]
            if not bytes_map:
                continue
            freed = sum(len(v) for v in bytes_map.values())
            self._entries[key] = (urls, duration, info, cached_at, {})
            self._bytes -= freed
            if self._bytes <= self._bytes_cap:
                break
