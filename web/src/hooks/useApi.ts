import { useEffect, useState } from "react";

interface State<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
}

interface CacheEntry<T> {
  data: T;
  timestamp: number;
}

const cache = new Map<string, CacheEntry<unknown>>();
const inflight = new Map<string, Promise<unknown>>();

// Default cap on how stale a cached value can be before we refuse to
// render it as the initial state. We always revalidate in the
// background regardless, so a fresh hit just means "render this
// instantly while the network confirms"; an expired hit means "show
// the spinner because we'd rather wait than show possibly-wrong
// data". 5 minutes is a generous default for editorial pages /
// detail views; pages with stricter freshness can pass `ttlMs`.
const DEFAULT_TTL_MS = 5 * 60 * 1000;

export interface UseApiOptions {
  /** Stable key that identifies this query for caching and request
   *  de-duplication. When omitted, the hook is uncached (legacy
   *  behavior — every mount fetches). */
  cacheKey?: string;
  /** Maximum age of a cached entry that's still fresh enough to use
   *  as the initial render. Older than this and we show the loading
   *  state until the revalidate completes. */
  ttlMs?: number;
  /** When true the hook skips the fetch entirely and returns
   *  `{ data: null, loading: false, error: null }`. Use for queries
   *  that are conditional on user input — e.g. Search before any
   *  query has been entered. The hook re-evaluates skip whenever
   *  `deps` change, so flipping this off triggers a fresh fetch. */
  skip?: boolean;
}

/**
 * Data-fetching hook with optional stale-while-revalidate caching.
 *
 * Without a `cacheKey`, behaves like the original tiny hook: fetch on
 * mount and on dep change, cancel stale results.
 *
 * With a `cacheKey`, on mount: if the cache has a fresh entry, render
 * it instantly (loading=false) and start a background revalidate; if
 * the cached entry is stale or absent, show loading and fetch. Two
 * components mounted with the same key share the in-flight request.
 *
 * Errors during revalidate keep the stale data visible; the `error`
 * field is populated so callers that care can surface a toast. Cold
 * fetches that fail clear `data` and surface the error normally.
 */
export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: React.DependencyList = [],
  options?: UseApiOptions,
): State<T> {
  const cacheKey = options?.cacheKey;
  const ttlMs = options?.ttlMs ?? DEFAULT_TTL_MS;
  const skip = options?.skip ?? false;

  // Initial state pulls from cache synchronously so the very first
  // render of a revisited page already has data — no spinner flash.
  const [state, setState] = useState<State<T>>(() => {
    if (skip) return { data: null, loading: false, error: null };
    if (cacheKey) {
      const entry = cache.get(cacheKey) as CacheEntry<T> | undefined;
      if (entry && Date.now() - entry.timestamp < ttlMs) {
        return { data: entry.data, loading: false, error: null };
      }
    }
    return { data: null, loading: true, error: null };
  });

  useEffect(() => {
    if (skip) {
      // Idle — caller hasn't supplied the input the fetcher needs
      // (e.g. an empty search query). Reset to a clean idle state
      // so a previous query's data doesn't linger after the input
      // is cleared.
      setState({ data: null, loading: false, error: null });
      return;
    }

    let cancelled = false;

    // Adjust visible loading state based on whether we have a fresh
    // cached value: stale-while-revalidate keeps loading=false while
    // the background fetch runs, full cold load shows the spinner.
    // On a cold key (no fresh cache hit), data is cleared too — the
    // previous render's data is for a different query, so showing it
    // beneath the spinner would flash mismatched content.
    if (cacheKey) {
      const entry = cache.get(cacheKey) as CacheEntry<T> | undefined;
      const fresh = entry && Date.now() - entry.timestamp < ttlMs;
      if (fresh) {
        setState({ data: entry.data, loading: false, error: null });
      } else {
        setState({ data: null, loading: true, error: null });
      }
    } else {
      setState({ data: null, loading: true, error: null });
    }

    const promise = (() => {
      if (!cacheKey) return fetcher();
      const existing = inflight.get(cacheKey) as Promise<T> | undefined;
      if (existing) return existing;
      const p = fetcher().finally(() => {
        // Only remove if we're still the registered in-flight promise —
        // a later prefetch may have overwritten us.
        if (inflight.get(cacheKey) === p) inflight.delete(cacheKey);
      });
      inflight.set(cacheKey, p);
      return p;
    })();

    promise
      .then((data) => {
        if (cancelled) return;
        if (cacheKey) {
          cache.set(cacheKey, { data, timestamp: Date.now() });
        }
        setState({ data, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const error = err instanceof Error ? err : new Error(String(err));
        // On error, fall back to whatever the cache has — even an
        // expired entry is more useful than a blank screen because
        // the backend hiccupped for a second.
        if (cacheKey) {
          const entry = cache.get(cacheKey) as CacheEntry<T> | undefined;
          if (entry) {
            setState({ data: entry.data, loading: false, error });
            return;
          }
        }
        setState((s) => ({ data: s.data, loading: false, error }));
      });

    return () => {
      cancelled = true;
    };
    // skip is included so toggling it on/off re-evaluates the effect
    // even if the caller didn't put their skip predicate in deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skip, ...deps]);

  return state;
}

/**
 * Fire-and-forget prefetch — populates the cache without rendering.
 *
 * Used by hover handlers on sidebar / nav links so the data is
 * already (or imminently) cached by the time the click lands. Skips
 * the request if the cache is already fresh or another prefetch is
 * in flight for the same key.
 */
export function prefetchApi<T>(
  cacheKey: string,
  fetcher: () => Promise<T>,
  ttlMs: number = DEFAULT_TTL_MS,
): void {
  if (!cacheKey) return;
  const entry = cache.get(cacheKey) as CacheEntry<T> | undefined;
  if (entry && Date.now() - entry.timestamp < ttlMs) return;
  if (inflight.has(cacheKey)) return;
  const p = fetcher()
    .then((data) => {
      cache.set(cacheKey, { data, timestamp: Date.now() });
      return data;
    })
    .finally(() => {
      if (inflight.get(cacheKey) === p) inflight.delete(cacheKey);
    });
  inflight.set(cacheKey, p);
}

/**
 * Surgically update one cache entry's data in place, if present.
 *
 * Used to keep an optimistic mutation consistent with the SWR cache —
 * e.g. when the user unfavorites an album, drop it from the cached
 * library list so a later remount reads the corrected list instead of
 * briefly re-rendering the stale one before the background revalidate
 * catches up. No-op when the key isn't cached; the next fetch produces
 * a correct entry anyway. Keeps the original timestamp: we corrected
 * the data, we didn't refetch it, so this must not extend freshness.
 *
 * Note this updates the cache only, not any mounted component's state
 * (that's a render-time snapshot) — callers that need the current view
 * to react should also drive their own state.
 */
export function mutateApiCache<T>(
  cacheKey: string,
  updater: (prev: T) => T,
): void {
  if (!cacheKey) return;
  const entry = cache.get(cacheKey) as CacheEntry<T> | undefined;
  if (!entry) return;
  cache.set(cacheKey, {
    data: updater(entry.data),
    timestamp: entry.timestamp,
  });
}

/** Wipe the whole cache. Used on logout / session reset. */
export function clearApiCache(): void {
  cache.clear();
  inflight.clear();
}

// Test seam — exposed for vitest only.
export const __cacheInternals = { cache, inflight };
