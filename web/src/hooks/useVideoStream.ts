import { useEffect, useState } from "react";
import { api } from "@/api/client";

/**
 * Module-level cache of resolved HLS manifest URLs. Tidal's signed
 * URLs stay valid for well beyond a single user's browse-then-click
 * flow, so caching for up to 5 minutes makes hover-prefetch pay off —
 * opening a video the user hovered over feels instant because the
 * `/stream` round-trip is already done.
 *
 * Key includes the requested quality so swapping between qualities
 * doesn't collide; the quality picker asks for a specific quality
 * while the default hover-prefetch uses the user's session default.
 */
type Entry = { url: string; expiresAt: number };

const cache = new Map<string, Entry>();
const inflight = new Map<string, Promise<string | null>>();
const TTL_MS = 5 * 60 * 1000;

function cacheKey(id: string, quality?: string): string {
  return `${id}::${quality ?? "default"}`;
}

function getCached(key: string): string | null {
  const entry = cache.get(key);
  if (!entry) return null;
  if (Date.now() > entry.expiresAt) {
    cache.delete(key);
    return null;
  }
  return entry.url;
}

function setCached(key: string, url: string) {
  cache.set(key, { url, expiresAt: Date.now() + TTL_MS });
}

/**
 * Fire-and-forget prefetch at the default quality. Callers on a card's
 * mouseenter hook call this so by the time the user clicks Play, the
 * network round-trip is already done.
 */
export function prefetchVideoStream(id: string): void {
  const key = cacheKey(id);
  if (getCached(key) || inflight.has(key)) return;
  const promise = api
    .videoStream(id)
    .then((res) => {
      if (res?.url) setCached(key, res.url);
      return res?.url ?? null;
    })
    .catch(() => null)
    .finally(() => {
      inflight.delete(key);
    });
  inflight.set(key, promise);
}

/**
 * Returns the HLS URL for a video. Optional `quality` forces a
 * specific stream quality (used by the quality picker); omit to use
 * the session's default.
 */
export function useVideoStream(
  videoId: string | null,
  quality?: string,
): {
  url: string | null;
  error: string | null;
  loading: boolean;
} {
  const [state, setState] = useState<{
    url: string | null;
    error: string | null;
    loading: boolean;
  }>(() => ({
    url: videoId ? getCached(cacheKey(videoId, quality)) : null,
    error: null,
    loading: false,
  }));

  useEffect(() => {
    if (!videoId) {
      setState({ url: null, error: null, loading: false });
      return;
    }
    const key = cacheKey(videoId, quality);
    const cached = getCached(key);
    if (cached) {
      setState({ url: cached, error: null, loading: false });
      return;
    }
    let cancelled = false;
    setState({ url: null, error: null, loading: true });
    const promise =
      inflight.get(key) ??
      (() => {
        const p = api
          .videoStream(videoId, quality)
          .then((res) => {
            if (res?.url) setCached(key, res.url);
            return res?.url ?? null;
          })
          .finally(() => {
            inflight.delete(key);
          });
        inflight.set(key, p);
        return p;
      })();
    promise
      .then((url) => {
        if (cancelled) return;
        if (url) setState({ url, error: null, loading: false });
        else
          setState({
            url: null,
            error: "No stream URL returned",
            loading: false,
          });
      })
      .catch((err) => {
        if (cancelled) return;
        setState({
          url: null,
          error: err instanceof Error ? err.message : String(err),
          loading: false,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [videoId, quality]);

  return state;
}
