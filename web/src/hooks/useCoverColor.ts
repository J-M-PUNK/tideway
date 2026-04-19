import { useEffect, useState } from "react";
import { FastAverageColor } from "fast-average-color";

const fac = new FastAverageColor();

// Module-level cache so navigating to the same album twice skips the work.
const cache = new Map<string, string>();

/**
 * Return a CSS color (e.g. `rgb(44, 88, 120)`) sampled from the given
 * image URL. Null while loading or on failure. Results are cached per URL.
 */
export function useCoverColor(url: string | null | undefined): string | null {
  const [color, setColor] = useState<string | null>(url ? cache.get(url) ?? null : null);

  useEffect(() => {
    if (!url) {
      setColor(null);
      return;
    }
    const cached = cache.get(url);
    if (cached) {
      setColor(cached);
      return;
    }
    let cancelled = false;
    fac
      .getColorAsync(url, { algorithm: "dominant", ignoredColor: [[0, 0, 0, 255, 20]] })
      .then((result) => {
        if (cancelled) return;
        cache.set(url, result.rgb);
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
