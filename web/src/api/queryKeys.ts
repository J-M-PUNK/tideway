/**
 * Canonical cache keys for `useApi` / `prefetchApi` calls.
 *
 * Anywhere a page component opts into the SWR cache, the same key
 * has to appear in the matching sidebar / hover prefetch handler.
 * Centralising the keys here means we can't drift apart silently —
 * a typo on either side would just miss the cache instead of
 * pretending two different queries are the same.
 */

import { api } from "@/api/client";
import { prefetchApi } from "@/hooks/useApi";

export const queryKeys = {
  pageHome: "page:home",
  pageGenres: "page:genres",
  pageMoods: "page:moods",
  feed: "page:feed",
  mixes: "page:mixes",
  charts: (path: string) => `page:charts:${path}`,
  pagePath: (path: string) => `page:path:${path}`,
  popularArtists: "page:popular:artists",
  popularTracks: "page:popular:tracks",
  album: (id: string) => `album:${id}`,
  artist: (id: string) => `artist:${id}`,
  mix: (id: string) => `mix:${id}`,
} as const;

/**
 * Hover / nav-anticipation prefetch helpers — only the ones actually
 * wired into hover handlers. Each is a no-op when the cache is fresh,
 * so callers can fire on every mouseenter without worrying about
 * flooding the backend.
 */
export const prefetch = {
  pageHome: () => prefetchApi(queryKeys.pageHome, () => api.page("home")),
  feed: () => prefetchApi(queryKeys.feed, () => api.feed()),
  popularArtists: () =>
    prefetchApi(queryKeys.popularArtists, () => api.lastfm.chartTopArtists(50)),
  popularTracks: () =>
    prefetchApi(queryKeys.popularTracks, () =>
      api.lastfm.chartTopTracksResolved(50),
    ),
  album: (id: string) => prefetchApi(queryKeys.album(id), () => api.album(id)),
  artist: (id: string) =>
    prefetchApi(queryKeys.artist(id), () => api.artist(id)),
  // Playlist detail is mutation-heavy (rename, remove tracks); skipping
  // the cache there until invalidation is wired into the edit flow.
};
