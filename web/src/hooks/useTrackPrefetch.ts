import { useCallback, useEffect, useRef } from "react";
import { api } from "@/api/client";
import { useUiPreferences } from "@/hooks/useUiPreferences";

/**
 * Client-side de-dupe for stream-manifest prefetches, so repeat hovers
 * and album re-mounts don't flood the backend with identical requests.
 *
 * The backend decides what's actually warm: its manifest cache holds a
 * track for 3 minutes, because Tidal's signed segment URLs expire then
 * (app/audio/manifest_cache.py). So a warm can only be remembered for
 * less than that. This used to remember it for the whole session, and
 * a track warmed more than three minutes earlier — the album you've
 * been listening to since you opened it — never warmed again: its
 * hover sent nothing and its click paid the full resolve. Repeats
 * inside REWARM_AFTER_MS are still de-duped; after that a hover warms
 * again, which is a no-op on the backend while its entry is still live.
 */
const REWARM_AFTER_MS = 60_000;
const warmedAt = new Map<string, number>();

function key(id: string, quality: string): string {
  return `${quality}:${id}`;
}

/** Record a warm for `k` unless one was sent recently. Returns
 *  whether the caller should send it. */
function claimWarm(k: string, now: number): boolean {
  const last = warmedAt.get(k);
  if (last !== undefined && now - last < REWARM_AFTER_MS) return false;
  warmedAt.set(k, now);
  return true;
}

/**
 * Hook that returns two functions:
 *
 *   prefetchOne(id)   — fire a single-track warm, debounced by the
 *                       caller's own hover delay (see useHoverPrefetch
 *                       below). Safe to call repeatedly; repeats for
 *                       the same id within REWARM_AFTER_MS are no-ops.
 *
 *   prefetchMany(ids) — batch warm used by album / playlist / mix
 *                       detail pages on mount. Drops ids warmed within
 *                       REWARM_AFTER_MS so a re-mount doesn't re-request.
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
    if (!claimWarm(key(id, qualityRef.current), Date.now())) return;
    void api.player.prefetch([id], qualityRef.current);
  }, []);

  const prefetchMany = useCallback((ids: string[]) => {
    if (!ids?.length) return;
    const q = qualityRef.current;
    const now = Date.now();
    const fresh = ids.filter((id) => !!id && claimWarm(key(id, q), now));
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
