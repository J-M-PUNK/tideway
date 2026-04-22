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
  return key ? playcountCache.get(key) ?? null : null;
}

export function useSpotifyArtistStats(
  tidalArtistId: string | null | undefined,
  sampleIsrc: string | null | undefined,
): ArtistStats | null {
  const [, force] = useState(0);
  const key =
    tidalArtistId && sampleIsrc
      ? `${tidalArtistId}:${sampleIsrc.toUpperCase()}`
      : null;
  useEffect(() => {
    if (!key || !tidalArtistId || !sampleIsrc || !spotifyEnabled) return;
    const off = subscribe(statsSubs, key, () => force((n) => n + 1));
    if (!statsCache.has(key) && !statsInflight.has(key)) {
      const p = api.spotify
        .artistStats(tidalArtistId, sampleIsrc)
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
  }, [key, tidalArtistId, sampleIsrc]);
  return key ? statsCache.get(key) ?? null : null;
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
  }
}
