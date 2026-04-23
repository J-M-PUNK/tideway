import { useCallback, useEffect, useRef } from "react";
import { api } from "@/api/client";
import { useUiPreferences } from "@/hooks/useUiPreferences";

/**
 * Client-side cooperative cache for stream-manifest prefetches.
 *
 * Backend (app/audio/player.py) keeps its own 3-minute TTL on the
 * resolved manifest. This set just tracks which (track id, quality)
 * pairs we've already warmed once in this session so the frontend
 * doesn't flood the backend with identical hover / album-mount
 * requests. The backend TTL still decides when the data actually
 * expires — we're only de-duping the requests themselves.
 */
const warmed = new Set<string>();

function key(id: string, quality: string): string {
  return `${quality}:${id}`;
}

/**
 * Hook that returns two functions:
 *
 *   prefetchOne(id)   — fire a single-track warm, debounced by the
 *                       caller's own hover delay (see useHoverPrefetch
 *                       below). Safe to call repeatedly; duplicate
 *                       calls for the same id are no-ops this session.
 *
 *   prefetchMany(ids) — batch warm used by album / playlist / mix
 *                       detail pages on mount. Drops ids we've already
 *                       warmed so a re-mount doesn't re-request.
 *
 * Both variants are fire-and-forget.
 */
export function useTrackPrefetch() {
  const { streamingQuality } = useUiPreferences();
  const qualityRef = useRef<string>(streamingQuality);
  useEffect(() => {
    qualityRef.current = streamingQuality;
  }, [streamingQuality]);

  const prefetchOne = useCallback((id: string) => {
    if (!id) return;
    const k = key(id, qualityRef.current);
    if (warmed.has(k)) return;
    warmed.add(k);
    void api.player.prefetch([id], qualityRef.current);
  }, []);

  const prefetchMany = useCallback((ids: string[]) => {
    if (!ids?.length) return;
    const q = qualityRef.current;
    const fresh = ids.filter((id) => {
      if (!id) return false;
      const k = key(id, q);
      if (warmed.has(k)) return false;
      warmed.add(k);
      return true;
    });
    if (fresh.length === 0) return;
    void api.player.prefetch(fresh, q);
  }, []);

  return { prefetchOne, prefetchMany };
}

/**
 * Hover-triggered single-track prefetch. The 200ms delay filters out
 * casual mouse-overs (scrolling past a row) so we don't spam the
 * backend on every row the cursor briefly crosses.
 */
export function useHoverPrefetch() {
  const { prefetchOne } = useTrackPrefetch();
  const timerRef = useRef<number | null>(null);
  const cancel = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);
  const schedule = useCallback(
    (id: string) => {
      cancel();
      if (!id) return;
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        prefetchOne(id);
      }, 200);
    },
    [cancel, prefetchOne],
  );
  useEffect(() => cancel, [cancel]);
  return { schedule, cancel };
}
