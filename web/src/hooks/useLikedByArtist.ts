import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { Album, Track } from "@/api/types";

/**
 * Returns the user's liked tracks AND albums filtered to a specific
 * artist. Powers the artist page's "You Liked" summary card and the
 * drill-down "Liked from [Artist]" page that shows both hearted
 * tracks and hearted albums in one place.
 *
 * Implementation notes:
 *
 *   * Fetches `api.library.tracks()` and `api.library.albums()` once
 *     each, caches both at module scope. The set is bounded by the
 *     user's library size and lookup is O(N) per filter — fine even
 *     for collections in the thousands. Both fire in parallel on
 *     first call so the round trip is one wave, not two.
 *   * Listens for the `tideway:favorite-toggled` window event,
 *     dispatched from `useFavorites.toggle`, and invalidates both
 *     caches so the next render reflects the new state. Without
 *     this the section would freeze at the snapshot taken on first
 *     call.
 *   * Track inclusion is featured-credit-inclusive. A track where
 *     the artist appears anywhere in `artists[]` counts. Album
 *     inclusion uses the same rule against the album's `artists[]`.
 *   * Returns `null` while the first fetch is in flight so callers
 *     can render a skeleton. An empty `{tracks: [], albums: []}`
 *     means the user has nothing liked for this artist.
 */
const FAVORITE_TOGGLED_EVENT = "tideway:favorite-toggled";

interface LibraryCache {
  tracks: Track[] | null;
  albums: Album[] | null;
}

let cache: LibraryCache = { tracks: null, albums: null };
let inflight: Promise<LibraryCache> | null = null;
const subscribers = new Set<(c: LibraryCache) => void>();

async function loadLibrary(): Promise<LibraryCache> {
  if (cache.tracks !== null && cache.albums !== null) return cache;
  if (inflight) return inflight;
  inflight = Promise.all([api.library.tracks(), api.library.albums()])
    .then(([tracks, albums]) => {
      cache = { tracks, albums };
      subscribers.forEach((fn) => fn(cache));
      return cache;
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/**
 * Drop the in-memory cache so the next call to `useLikedByArtist`
 * triggers a fresh fetch. Called locally in response to the
 * `tideway:favorite-toggled` event. Safe to call any time; idempotent
 * when the cache is already empty.
 */
function invalidateCache(): void {
  cache = { tracks: null, albums: null };
}

export interface LikedByArtist {
  tracks: Track[];
  albums: Album[];
}

export function useLikedByArtist(
  artistId: string | null | undefined,
): LikedByArtist | null {
  const [snapshot, setSnapshot] = useState<LibraryCache>(cache);

  useEffect(() => {
    const handle = (c: LibraryCache) => setSnapshot({ ...c });
    subscribers.add(handle);
    void loadLibrary().then(handle);
    return () => {
      subscribers.delete(handle);
    };
  }, []);

  useEffect(() => {
    const listener = () => {
      invalidateCache();
      setSnapshot({ tracks: null, albums: null });
      void loadLibrary().then((c) => setSnapshot({ ...c }));
    };
    window.addEventListener(FAVORITE_TOGGLED_EVENT, listener);
    return () => {
      window.removeEventListener(FAVORITE_TOGGLED_EVENT, listener);
    };
  }, []);

  return useMemo(() => {
    if (snapshot.tracks === null || snapshot.albums === null || !artistId) {
      return null;
    }
    const wanted = String(artistId);
    const tracks = snapshot.tracks.filter((t) =>
      (t.artists ?? []).some((a) => String(a.id) === wanted),
    );
    const albums = snapshot.albums.filter((a) =>
      (a.artists ?? []).some((art) => String(art.id) === wanted),
    );
    return { tracks, albums };
  }, [snapshot.tracks, snapshot.albums, artistId]);
}
