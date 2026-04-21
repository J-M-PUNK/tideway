import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { LastFmPlaycount } from "@/api/types";

/**
 * Module-level cache keyed by "type:artist:name" so the same artist
 * being asked for from multiple surfaces (Now Playing, artist hero,
 * album hero, track row) only fires one Last.fm request. Entries are
 * small — just playcount numbers — so we don't bother evicting.
 */
const cache = new Map<string, LastFmPlaycount>();
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
