import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { Track } from "@/api/types";
import { findBestMatch } from "@/lib/match";

/**
 * Lazy "Last.fm entry → Tidal object" resolvers. Both hooks share the
 * same pattern as `useTidalArt`: module-level cache with subscriber
 * notify so one lookup feeds every component asking for the same key.
 *
 * - `useTidalArtistId(name)` returns the Tidal artist ID for a Last.fm
 *   artist name, so Popular's artist cards can route to the real
 *   artist page instead of falling into the play-on-click trap.
 * - `useTidalTracksFor(queries)` resolves a whole list of Last.fm
 *   `{title, artist}` entries to Tidal Track objects in parallel, so
 *   the Popular tracks tab can render via the standard `TrackList`
 *   and get duration / clickable album / clickable artist / menu for
 *   free.
 */

// -- artist id ----------------------------------------------------------
type ArtistEntry = { id: string | null; promise?: Promise<string | null> };
const artistCache = new Map<string, ArtistEntry>();
const artistSubs = new Map<string, Set<() => void>>();

function notifyArtist(key: string) {
  artistSubs.get(key)?.forEach((fn) => fn());
}

async function resolveArtistId(name: string): Promise<string | null> {
  try {
    const res = await api.search(name, 10);
    const exact = res.artists.find((a) => a.name.toLowerCase() === name.toLowerCase());
    return (exact ?? res.artists[0])?.id ?? null;
  } catch {
    return null;
  }
}

export function useTidalArtistId(name: string): string | null {
  const key = name.toLowerCase().trim();
  const [, tick] = useState(0);

  useEffect(() => {
    if (!key) return;
    let active = true;
    const sub = () => {
      if (active) tick((n) => n + 1);
    };
    let set = artistSubs.get(key);
    if (!set) {
      set = new Set();
      artistSubs.set(key, set);
    }
    set.add(sub);

    if (!artistCache.get(key)) {
      const promise = resolveArtistId(name).then((id) => {
        artistCache.set(key, { id });
        notifyArtist(key);
        return id;
      });
      artistCache.set(key, { id: null, promise });
    }

    return () => {
      active = false;
      set?.delete(sub);
    };
  }, [key, name]);

  return artistCache.get(key)?.id ?? null;
}

// -- tracks (batched) ---------------------------------------------------
type TrackEntry = { track: Track | null; promise?: Promise<Track | null> };
const trackCache = new Map<string, TrackEntry>();
const trackSubs = new Map<string, Set<() => void>>();

function trackKey(title: string, artist: string): string {
  return `${artist.toLowerCase().trim()}::${title.toLowerCase().trim()}`;
}

function notifyTrack(key: string) {
  trackSubs.get(key)?.forEach((fn) => fn());
}

async function resolveTrack(title: string, artist: string): Promise<Track | null> {
  try {
    const res = await api.search(`${artist} ${title}`, 5);
    return findBestMatch(res.tracks, { track: title, artist });
  } catch {
    return null;
  }
}

/**
 * Resolve a list of `{title, artist}` queries to Tidal Track objects.
 * Returns the current resolved tracks in the same order as the queries,
 * with unresolved entries filtered out. Re-renders as each search
 * completes so the list visibly fills in.
 */
export function useTidalTracksFor(
  queries: { title: string; artist: string }[],
): Track[] {
  const [, tick] = useState(0);

  useEffect(() => {
    const subsForThisEffect: { key: string; fn: () => void }[] = [];
    let active = true;
    const sub = () => {
      if (active) tick((n) => n + 1);
    };
    for (const q of queries) {
      const key = trackKey(q.title, q.artist);
      let set = trackSubs.get(key);
      if (!set) {
        set = new Set();
        trackSubs.set(key, set);
      }
      set.add(sub);
      subsForThisEffect.push({ key, fn: sub });

      if (!trackCache.get(key)) {
        const promise = resolveTrack(q.title, q.artist).then((track) => {
          trackCache.set(key, { track });
          notifyTrack(key);
          return track;
        });
        trackCache.set(key, { track: null, promise });
      }
    }

    return () => {
      active = false;
      for (const { key, fn } of subsForThisEffect) {
        trackSubs.get(key)?.delete(fn);
      }
    };
    // Depend only on the list of keys, not the array identity — parent
    // components re-create the queries array on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queries.map((q) => trackKey(q.title, q.artist)).join("|")]);

  const out: Track[] = [];
  for (const q of queries) {
    const key = trackKey(q.title, q.artist);
    const entry = trackCache.get(key);
    if (entry?.track) out.push(entry.track);
  }
  return out;
}
