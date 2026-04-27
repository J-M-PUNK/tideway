import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { Track } from "@/api/types";

/**
 * Returns the user's liked tracks filtered to those credited to a
 * specific artist. Matches Spotify's "Liked songs by [Artist]" surface
 * on the artist page.
 *
 * Implementation notes:
 *
 *   * Fetches `api.library.tracks()` once and caches the full list at
 *     module scope so navigating between artist pages reuses it. The
 *     filter is O(N) per call — fine even for libraries in the
 *     thousands.
 *   * Listens for the `tideway:favorite-toggled` window event,
 *     dispatched from `useFavorites.toggle`, and invalidates the
 *     cache so the next render reflects the new state. Without this
 *     the section would freeze at the snapshot taken on first call.
 *   * Includes featured-credit tracks. Spotify counts a track if the
 *     artist appears anywhere in `artists[]`; we match that.
 *   * Returns `null` while the first fetch is in flight so callers
 *     can render a skeleton. An empty array means the user has no
 *     liked tracks for this artist.
 */
const FAVORITE_TOGGLED_EVENT = "tideway:favorite-toggled";

let cache: Track[] | null = null;
let inflight: Promise<Track[]> | null = null;
const subscribers = new Set<(tracks: Track[]) => void>();

async function loadLibraryTracks(): Promise<Track[]> {
  if (cache !== null) return cache;
  if (inflight) return inflight;
  inflight = api.library
    .tracks()
    .then((list) => {
      cache = list;
      subscribers.forEach((fn) => fn(list));
      return list;
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/**
 * Drop the in-memory cache and notify any active subscribers so they
 * trigger a fresh fetch. Called from `useFavorites.toggle` so the
 * artist page's "Liked songs" section stays in sync with the heart
 * button. Safe to call from anywhere; idempotent when the cache is
 * already empty.
 */
export function invalidateLikedTracksCache(): void {
  cache = null;
  // Active subscribers refetch on the next effect pass when the
  // event handler below sets state to null and the dependent effect
  // re-runs.
}

export function useLikedTracksByArtist(
  artistId: string | null | undefined,
): Track[] | null {
  const [allLiked, setAllLiked] = useState<Track[] | null>(cache);

  useEffect(() => {
    // Subscriber fires when a fetch completes — covers the cold
    // mount case as well as post-invalidation refetches.
    const handle = (list: Track[]) => setAllLiked(list);
    subscribers.add(handle);
    void loadLibraryTracks().then((list) => setAllLiked(list));
    return () => {
      subscribers.delete(handle);
    };
  }, []);

  // Invalidate + refetch on heart toggles.
  useEffect(() => {
    const listener = () => {
      invalidateLikedTracksCache();
      setAllLiked(null);
      void loadLibraryTracks().then((list) => setAllLiked(list));
    };
    window.addEventListener(FAVORITE_TOGGLED_EVENT, listener);
    return () => {
      window.removeEventListener(FAVORITE_TOGGLED_EVENT, listener);
    };
  }, []);

  return useMemo(() => {
    if (allLiked === null || !artistId) return allLiked;
    const wanted = String(artistId);
    return allLiked.filter((t) =>
      (t.artists ?? []).some((a) => String(a.id) === wanted),
    );
  }, [allLiked, artistId]);
}
