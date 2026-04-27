import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { Lyrics } from "@/api/types";

// Module-level cache — shared across LyricsPanel and FullScreenPlayer so
// opening one doesn't re-fetch if the other already did.
const cache = new Map<string, Lyrics>();
const inflight = new Map<string, Promise<Lyrics>>();
// Per-trackId subscribers. Every hook instance for the same track registers
// here so a fetch resolution notifies EVERY listener, not just the one that
// started the request.
const subscribers = new Map<string, Set<() => void>>();

function notify(trackId: string) {
  subscribers.get(trackId)?.forEach((fn) => {
    try {
      fn();
    } catch {
      /* one listener's bug shouldn't take down the others */
    }
  });
}

function subscribeTrack(trackId: string, fn: () => void): () => void {
  let set = subscribers.get(trackId);
  if (!set) {
    set = new Set();
    subscribers.set(trackId, set);
  }
  set.add(fn);
  return () => {
    const s = subscribers.get(trackId);
    if (!s) return;
    s.delete(fn);
    if (s.size === 0) subscribers.delete(trackId);
  };
}

function fetchOnce(trackId: string): Promise<Lyrics> {
  const existing = inflight.get(trackId);
  if (existing) return existing;
  const promise = api
    .trackLyrics(trackId)
    .catch(() => ({ synced: null, text: null }) as Lyrics)
    .then((lyrics) => {
      cache.set(trackId, lyrics);
      notify(trackId);
      return lyrics;
    })
    .finally(() => inflight.delete(trackId));
  inflight.set(trackId, promise);
  return promise;
}

/**
 * Fetch lyrics for a track, process-wide cache + in-flight dedup.
 *
 * Any number of hook instances can subscribe to the same track; when the
 * fetch resolves, all of them re-render (via the subscriber map). A hook
 * that subscribes after the fetch already resolved reads straight from
 * cache with no refetch.
 */
export function useLyrics(trackId: string | null | undefined): {
  lyrics: Lyrics | null;
  loading: boolean;
} {
  // Version counter doubles as "cache changed, please re-read".
  const [, forceRender] = useState(0);

  useEffect(() => {
    if (!trackId) return;
    const cached = cache.has(trackId);
    // Subscribe first so we don't miss a notification if the fetch resolves
    // between our cache check and our subscription.
    const unsubscribe = subscribeTrack(trackId, () =>
      forceRender((n) => n + 1),
    );
    if (!cached) fetchOnce(trackId);
    return unsubscribe;
  }, [trackId]);

  const lyrics = trackId ? (cache.get(trackId) ?? null) : null;
  // Treat as loading whenever we have a trackId and no cache entry yet,
  // regardless of whether the in-flight request has been registered by
  // our effect. Otherwise the very first render (before the effect
  // fires) flashes the "No lyrics available" empty state between the
  // track change and the fetch kicking off.
  const loading = !!trackId && !cache.has(trackId);
  return { lyrics, loading };
}
