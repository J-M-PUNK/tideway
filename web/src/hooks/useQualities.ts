import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { QualityOption } from "@/api/types";
import { publishDefaultQuality } from "@/components/DownloadButton";

// The quality catalog is static per server version; one fetch per app
// load is plenty. Shared cache + in-flight dedup so any number of rows
// mounting simultaneously (e.g. 20 failed Downloads rows) fire exactly
// one network call.
let cached: QualityOption[] | null = null;
let inflight: Promise<QualityOption[]> | null = null;
const subscribers = new Set<() => void>();

function subscribe(fn: () => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

/**
 * Drop the module-level cache so the next call refetches. Used after
 * auth state changes (e.g. switching between device-code and PKCE
 * sessions) since the filtered list depends on the logged-in client's
 * entitlements — a session that's newly capable of Max must re-pull.
 */
export function resetQualitiesCache(): void {
  cached = null;
  inflight = null;
  subscribers.forEach((fn) => {
    try {
      fn();
    } catch {
      /* ignore */
    }
  });
}

function fetchOnce(): Promise<QualityOption[]> {
  if (cached) return Promise.resolve(cached);
  if (inflight) return inflight;
  inflight = api
    .qualities()
    .catch(() => [] as QualityOption[])
    .then((qs) => {
      cached = qs;
      subscribers.forEach((fn) => {
        try {
          fn();
        } catch {
          /* ignore */
        }
      });
      // The server may have clamped the saved default quality to the
      // subscription ceiling while computing the filtered list. Pull
      // settings back so every mounted DownloadButton's "Use default
      // (X)" label updates without a reload.
      api.settings
        .get()
        .then((s) => publishDefaultQuality(s.quality))
        .catch(() => {
          /* non-critical */
        });
      return qs;
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/**
 * Returns the quality catalog, lazily fetched once per session. `null`
 * during the initial fetch; afterwards the same array reference on every
 * call. Many rows calling this concurrently dedupe to one request.
 */
export function useQualities(): QualityOption[] | null {
  const [, force] = useState(0);
  useEffect(() => {
    const off = subscribe(() => force((n) => n + 1));
    if (!cached) fetchOnce();
    return off;
  }, []);
  return cached;
}
