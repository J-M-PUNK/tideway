import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { LastFmPlaycount } from "@/api/types";

/**
 * Module-level cache keyed by "type:artist:name" so the same artist
 * being asked for from multiple surfaces (Now Playing, artist hero,
 * album hero, track row) only fires one Last.fm request.
 *
 * Persisted to localStorage with a 7-day TTL so "you've played
 * them 342 times" doesn't disappear on every reload. Entries are
 * small (a few numeric fields) so total cache size stays well
 * under localStorage's per-origin quota even with thousands of
 * keys.
 */
const LS_KEY = "tidal-downloader:lastfm-playcount-cache";
const LS_TTL_MS = 7 * 24 * 3600 * 1000;

interface CacheEntry {
  val: LastFmPlaycount;
  t: number; // fetched_at (epoch ms)
}

function loadFromStorage(): Map<string, LastFmPlaycount> {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return new Map();
    const parsed: Record<string, CacheEntry> = JSON.parse(raw);
    const out = new Map<string, LastFmPlaycount>();
    const now = Date.now();
    for (const [k, entry] of Object.entries(parsed)) {
      if (entry && typeof entry.t === "number" && now - entry.t < LS_TTL_MS) {
        out.set(k, entry.val);
      }
    }
    return out;
  } catch {
    return new Map();
  }
}

function persistToStorage(): void {
  try {
    const now = Date.now();
    const payload: Record<string, CacheEntry> = {};
    cache.forEach((val, key) => {
      payload[key] = { val, t: now };
    });
    localStorage.setItem(LS_KEY, JSON.stringify(payload));
  } catch {
    // Quota hit or storage disabled — keep the in-memory cache;
    // just means next reload re-fetches.
  }
}

let persistScheduled = false;
function schedulePersist(): void {
  // Coalesce bursts of cache writes (a track-list render can easily
  // resolve 20 playcounts at once) into one localStorage.setItem.
  if (persistScheduled) return;
  persistScheduled = true;
  setTimeout(() => {
    persistScheduled = false;
    persistToStorage();
  }, 500);
}

const cache = loadFromStorage();
const inflight = new Map<string, Promise<LastFmPlaycount>>();
// Per-key subscriber list so a late-arriving result re-renders any
// component that asked before the fetch landed.
const subscribers = new Map<string, Set<() => void>>();

function subscribe(key: string, fn: () => void): () => void {
  let set = subscribers.get(key);
  if (!set) {
    set = new Set();
    subscribers.set(key, set);
  }
  set.add(fn);
  return () => {
    const s = subscribers.get(key);
    if (!s) return;
    s.delete(fn);
    if (s.size === 0) subscribers.delete(key);
  };
}

function notify(key: string): void {
  subscribers.get(key)?.forEach((fn) => {
    try {
      fn();
    } catch {
      /* ignore */
    }
  });
}

function fetchOnce(key: string, request: () => Promise<LastFmPlaycount>) {
  if (cache.has(key)) return;
  if (inflight.has(key)) return;
  const promise = request()
    .catch(() => ({} as LastFmPlaycount))
    .then((val) => {
      cache.set(key, val);
      schedulePersist();
      notify(key);
      return val;
    })
    .finally(() => {
      inflight.delete(key);
    });
  inflight.set(key, promise);
}

/** Clear every cached playcount. Called when the user disconnects or
 *  changes Last.fm account so stale "you've played this 342 times"
 *  numbers don't linger across sessions. */
export function clearLastfmPlaycountCache(): void {
  cache.clear();
  inflight.clear();
  try {
    localStorage.removeItem(LS_KEY);
  } catch {
    /* ignore */
  }
  subscribers.forEach((set) => set.forEach((fn) => fn()));
}

export function useLastfmArtistPlaycount(
  artist: string | null | undefined,
): LastFmPlaycount | null {
  const [, force] = useState(0);
  const key = artist ? `artist:${artist.toLowerCase()}` : null;
  useEffect(() => {
    if (!key || !artist || !lastfmEnabled) return;
    const off = subscribe(key, () => force((n) => n + 1));
    fetchOnce(key, () => api.lastfm.artistPlaycount(artist));
    return off;
  }, [key, artist]);
  return key ? cache.get(key) ?? null : null;
}

export function useLastfmAlbumPlaycount(
  artist: string | null | undefined,
  album: string | null | undefined,
): LastFmPlaycount | null {
  const [, force] = useState(0);
  const key = artist && album ? `album:${artist.toLowerCase()}:${album.toLowerCase()}` : null;
  useEffect(() => {
    if (!key || !artist || !album || !lastfmEnabled) return;
    const off = subscribe(key, () => force((n) => n + 1));
    fetchOnce(key, () => api.lastfm.albumPlaycount(artist, album));
    return off;
  }, [key, artist, album]);
  return key ? cache.get(key) ?? null : null;
}

export function useLastfmTrackPlaycount(
  artist: string | null | undefined,
  track: string | null | undefined,
): LastFmPlaycount | null {
  const [, force] = useState(0);
  const key = artist && track ? `track:${artist.toLowerCase()}:${track.toLowerCase()}` : null;
  useEffect(() => {
    if (!key || !artist || !track || !lastfmEnabled) return;
    const off = subscribe(key, () => force((n) => n + 1));
    fetchOnce(key, () => api.lastfm.trackPlaycount(artist, track));
    return off;
  }, [key, artist, track]);
  return key ? cache.get(key) ?? null : null;
}

/** Gates whether playcount hooks fire at all. Set to true as long as
 *  an API key is configured (baked-in default OR user-entered) — we
 *  don't require a connected session here because the global listener
 *  / playcount fields come through even without a Last.fm user. The
 *  per-user `userplaycount` field just won't be populated when the
 *  user isn't connected, which the UI handles gracefully.
 *
 *  App.tsx flips this on boot + on settings-updated events, and
 *  clears the cache on transitions so a disconnect doesn't leave
 *  stale per-user numbers behind. */
let lastfmEnabled = false;

export function setLastfmEnabled(enabled: boolean): void {
  const wasEnabled = lastfmEnabled;
  lastfmEnabled = enabled;
  // Clear the cache whenever the flag flips either way. Going from
  // disabled → enabled clears any prior empty responses so the hooks
  // re-fire now that calls can succeed. Going enabled → disabled
  // clears per-user numbers that no longer apply.
  if (wasEnabled !== enabled) clearLastfmPlaycountCache();
}

export function isLastfmEnabled(): boolean {
  return lastfmEnabled;
}
