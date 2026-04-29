import { useEffect, useState } from "react";
import { api } from "@/api/client";

/**
 * Spotify public-data enrichment hooks — complement Last.fm with
 * global popularity signals (total track plays, artist monthly
 * listeners, top cities).
 *
 * Both hooks follow the same module-level-cache pattern as
 * useLastfmPlaycount: any surface that asks for the same key
 * shares a single in-flight request and a late-arriving result
 * re-renders every subscriber. Unlike the Last.fm hooks, Spotify
 * lookups can take ~3s on first call (ISRC → Spotify track search
 * → getTrack / queryArtistOverview GraphQL) so the shared cache
 * matters more here.
 */

interface ArtistStats {
  monthly_listeners: number | null;
  followers: number | null;
  world_rank: number | null;
  top_cities: { city: string; country: string; listeners: number }[];
}

const playcountCache = new Map<string, number | null>();
const playcountInflight = new Map<string, Promise<number | null>>();
const playcountSubs = new Map<string, Set<() => void>>();

const statsCache = new Map<string, ArtistStats>();
const statsInflight = new Map<string, Promise<ArtistStats>>();
const statsSubs = new Map<string, Set<() => void>>();

function subscribe(
  map: Map<string, Set<() => void>>,
  key: string,
  fn: () => void,
): () => void {
  let set = map.get(key);
  if (!set) {
    set = new Set();
    map.set(key, set);
  }
  set.add(fn);
  return () => {
    const s = map.get(key);
    if (!s) return;
    s.delete(fn);
    if (s.size === 0) map.delete(key);
  };
}

function notify(map: Map<string, Set<() => void>>, key: string): void {
  map.get(key)?.forEach((fn) => {
    try {
      fn();
    } catch {
      /* ignore */
    }
  });
}

export function useSpotifyTrackPlaycount(
  isrc: string | null | undefined,
): number | null {
  const [, force] = useState(0);
  const key = isrc ? isrc.toUpperCase() : null;
  useEffect(() => {
    if (!key || !spotifyEnabled) return;
    const off = subscribe(playcountSubs, key, () => force((n) => n + 1));
    if (!playcountCache.has(key) && !playcountInflight.has(key)) {
      const p = api.spotify
        .trackPlaycount(key)
        .then((r) => r.playcount)
        .catch(() => null)
        .then((val) => {
          playcountCache.set(key, val);
          notify(playcountSubs, key);
          return val;
        })
        .finally(() => playcountInflight.delete(key));
      playcountInflight.set(key, p);
    }
    return off;
  }, [key]);
  return key ? (playcountCache.get(key) ?? null) : null;
}

export function useSpotifyArtistStats(
  tidalArtistId: string | null | undefined,
  tidalArtistName: string | null | undefined,
  sampleIsrcs: string[] | null | undefined,
): ArtistStats | null {
  const [, force] = useState(0);
  // Cache key uses the artist id, the (lowercased) artist name, and
  // the sorted+normalised ISRC list. The name belongs in the key
  // because the resolver's pick depends on it; without it, two
  // artists with overlapping ISRC samples would alias each other.
  const cleanedIsrcs = (sampleIsrcs || [])
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  const sortedIsrcs = [...cleanedIsrcs].sort();
  const key =
    tidalArtistId && cleanedIsrcs.length > 0
      ? `${tidalArtistId}:${(tidalArtistName || "").toLowerCase()}:${sortedIsrcs.join(",")}`
      : null;
  useEffect(() => {
    if (!key || !tidalArtistId || cleanedIsrcs.length === 0 || !spotifyEnabled)
      return;
    const off = subscribe(statsSubs, key, () => force((n) => n + 1));
    if (!statsCache.has(key) && !statsInflight.has(key)) {
      const p = api.spotify
        .artistStats(tidalArtistId, tidalArtistName || "", cleanedIsrcs)
        .then(
          (r): ArtistStats => ({
            monthly_listeners: r.monthly_listeners,
            followers: r.followers,
            world_rank: r.world_rank,
            top_cities: r.top_cities,
          }),
        )
        .catch(
          (): ArtistStats => ({
            monthly_listeners: null,
            followers: null,
            world_rank: null,
            top_cities: [],
          }),
        )
        .then((val) => {
          statsCache.set(key, val);
          notify(statsSubs, key);
          return val;
        })
        .finally(() => statsInflight.delete(key));
      statsInflight.set(key, p);
    }
    return off;
  }, [key, tidalArtistId, tidalArtistName, sortedIsrcs.join(",")]);
  return key ? (statsCache.get(key) ?? null) : null;
}

const albumPlaysCache = new Map<string, AlbumPlays>();
const albumPlaysInflight = new Map<string, Promise<AlbumPlays>>();
const albumPlaysSubs = new Map<string, Set<() => void>>();

interface AlbumPlays {
  total_plays: number;
  resolved: number;
  total: number;
}

/**
 * Sum Spotify play counts across an album, fetched server-side in a
 * single call. `isrcs` should include every track on the album that
 * has an ISRC — order doesn't matter (we sort into the cache key so
 * different call-sites share the cached result). Returns null until
 * the first fetch resolves; afterwards the summed object until the
 * user leaves the page.
 */
export function useSpotifyAlbumTotalPlays(
  isrcs: string[] | null,
): AlbumPlays | null {
  const [, force] = useState(0);
  const key =
    isrcs && isrcs.length > 0
      ? isrcs
          .map((i) => i.toUpperCase())
          .sort()
          .join(",")
      : null;
  useEffect(() => {
    if (!key || !spotifyEnabled) return;
    const off = subscribe(albumPlaysSubs, key, () => force((n) => n + 1));
    if (!albumPlaysCache.has(key) && !albumPlaysInflight.has(key)) {
      const p = api.spotify
        .albumTotalPlays(key.split(","))
        .catch(() => ({ total_plays: 0, resolved: 0, total: 0 }))
        .then((val) => {
          albumPlaysCache.set(key, val);
          notify(albumPlaysSubs, key);
          return val;
        })
        .finally(() => albumPlaysInflight.delete(key));
      albumPlaysInflight.set(key, p);
    }
    return off;
  }, [key]);
  return key ? (albumPlaysCache.get(key) ?? null) : null;
}

// Gate — flipped by a settings callback or an availability probe
// later. Default to on since the backend degrades gracefully
// (returns nulls) when Spotify is unreachable.
let spotifyEnabled = true;

export function setSpotifyEnrichmentEnabled(enabled: boolean): void {
  spotifyEnabled = enabled;
  if (!enabled) {
    playcountCache.clear();
    statsCache.clear();
    albumPlaysCache.clear();
  }
}

/**
 * Seed the playcount cache with a batch of pre-fetched results. Used
 * by `useSpotifyTrackPlaycountBatch` (and the Popular page directly
 * for its `refresh: true` case) so the per-row
 * `useSpotifyTrackPlaycount` hook renders from cache on first paint.
 */
export function preseedSpotifyPlaycounts(
  playcounts: Record<string, number | null>,
): void {
  if (!spotifyEnabled) return;
  // Write every entry first, then notify — React's automatic batching
  // coalesces the 50 subscriber setState calls into a single render
  // pass instead of triggering a re-render after each key.
  const keys: string[] = [];
  for (const [rawIsrc, value] of Object.entries(playcounts)) {
    const key = rawIsrc.toUpperCase();
    playcountCache.set(key, value);
    keys.push(key);
  }
  for (const key of keys) {
    notify(playcountSubs, key);
  }
}

interface PreseedTrack {
  id?: string;
  isrc?: string | null;
  name?: string;
  artists?: { name: string }[];
}

/**
 * One bulk request for all tracks' playcounts when a page mounts a
 * list of them. Without this, every TrackList row's
 * `useSpotifyTrackPlaycount` fires its own browser→backend round
 * trip, the browser throttles to ~6 parallel, and a 12-track album
 * cold-cache takes 5-6 seconds to fill in numbers (two waves of
 * 500-1000ms each). The batch endpoint runs the per-track lookups
 * through a 5-worker pool server-side so the wall time is one
 * round-trip plus parallel work — typically 1-3 seconds for a
 * full album.
 *
 * Idempotent: the same set of ISRCs hits the same server-side cache
 * key, so calling this from AlbumDetail, ArtistDetail, and the
 * tracklist's own per-row hooks won't double-up the upstream cost.
 *
 * Skips the request entirely when none of the tracks carry an ISRC
 * (rare; obscure catalog entries) or when the cache already has every
 * key — no point paying for a no-op.
 */
export function useSpotifyTrackPlaycountBatch(
  tracks: PreseedTrack[] | null | undefined,
): void {
  // Stable comma-joined ISRC key so the effect only refires when the
  // set of tracks actually changes — page mounts and re-renders that
  // produce the same list don't re-batch.
  const lookup =
    tracks
      ?.map((t) => ({
        isrc: (t.isrc ?? "").toUpperCase(),
        title: t.name ?? "",
        artist: t.artists?.[0]?.name ?? "",
      }))
      .filter((x) => x.isrc) ?? [];
  const cacheKey = lookup
    .map((x) => x.isrc)
    .sort()
    .join(",");

  useEffect(() => {
    if (!cacheKey || !spotifyEnabled) return;
    // Already cached for every ISRC? Skip the round trip.
    const allCached = lookup.every((x) => playcountCache.has(x.isrc));
    if (allCached) return;
    let cancelled = false;
    api.spotify
      .trackPlaycounts(lookup)
      .then((r) => {
        if (cancelled) return;
        preseedSpotifyPlaycounts(r.playcounts);
      })
      .catch(() => {
        /* per-row hooks fall back to their own fetch */
      });
    return () => {
      cancelled = true;
    };
    // `cacheKey` covers the contents of `lookup`. Re-deriving lookup
    // from `tracks` on every render would tempt a deps-array thrash;
    // we capture it once per cacheKey and the effect doesn't read it
    // again after the call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey]);
}
