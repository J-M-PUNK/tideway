import { useEffect, useState } from "react";
import { FastAverageColor } from "fast-average-color";

const fac = new FastAverageColor();

// LRU-bounded module cache. A long browsing session can visit hundreds
// of albums; without a cap the cache grows unboundedly. Map preserves
// insertion order, so "re-set on hit" gives us true LRU semantics.
const MAX_ENTRIES = 200;
const cache = new Map<string, string>();

function cacheGet(key: string): string | undefined {
  const v = cache.get(key);
  if (v === undefined) return undefined;
  // Touch: move to the end so repeated hits stay warm.
  cache.delete(key);
  cache.set(key, v);
  return v;
}

function cacheSet(key: string, value: string): void {
  if (cache.has(key)) cache.delete(key);
  cache.set(key, value);
  if (cache.size > MAX_ENTRIES) {
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
}

/**
 * Return a CSS color (e.g. `rgb(44, 88, 120)`) sampled from the given
 * image URL. Null while loading or on failure. Results are cached per URL.
 */
export function useCoverColor(url: string | null | undefined): string | null {
  const [color, setColor] = useState<string | null>(url ? cacheGet(url) ?? null : null);

  useEffect(() => {
    if (!url) {
      setColor(null);
      return;
    }
    const cached = cacheGet(url);
    if (cached) {
      setColor(cached);
      return;
    }
    let cancelled = false;
    fac
      .getColorAsync(url, { algorithm: "dominant", ignoredColor: [[0, 0, 0, 255, 20]] })
      .then((result) => {
        if (cancelled) return;
        cacheSet(url, result.rgb);
        setColor(result.rgb);
      })
      .catch(() => {
        /* leave null */
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  return color;
}
