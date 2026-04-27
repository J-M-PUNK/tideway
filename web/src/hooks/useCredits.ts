import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { CreditEntry } from "@/api/types";

// Module-level cache + in-flight dedup, mirroring useLyrics. Closing and
// reopening the credits dialog for the same track returns instantly; two
// rows that both fetch the same track kick off only one request.
const cache = new Map<string, CreditEntry[]>();
const inflight = new Map<string, Promise<CreditEntry[]>>();
const subscribers = new Map<string, Set<() => void>>();

function notify(trackId: string) {
  subscribers.get(trackId)?.forEach((fn) => {
    try {
      fn();
    } catch {
      /* ignore */
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

function fetchOnce(trackId: string): Promise<CreditEntry[]> {
  const existing = inflight.get(trackId);
  if (existing) return existing;
  const promise = api
    .trackCredits(trackId)
    .catch(() => [] as CreditEntry[])
    .then((credits) => {
      cache.set(trackId, credits);
      notify(trackId);
      return credits;
    })
    .finally(() => inflight.delete(trackId));
  inflight.set(trackId, promise);
  return promise;
}

/**
 * Fetch track credits with caching. Returns null while loading so the UI
 * can render a spinner; cached results are synchronous on subsequent calls.
 */
export function useCredits(
  trackId: string | null | undefined,
  enabled = true,
): {
  credits: CreditEntry[] | null;
  loading: boolean;
} {
  const [, forceRender] = useState(0);

  useEffect(() => {
    if (!enabled || !trackId) return;
    const unsubscribe = subscribeTrack(trackId, () =>
      forceRender((n) => n + 1),
    );
    if (!cache.has(trackId)) fetchOnce(trackId);
    return unsubscribe;
  }, [trackId, enabled]);

  const credits = trackId ? (cache.get(trackId) ?? null) : null;
  const loading =
    !!enabled && !!trackId && !cache.has(trackId) && inflight.has(trackId);
  return { credits, loading };
}
