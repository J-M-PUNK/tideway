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
  search: (query: string) => `search:${query.toLowerCase()}`,
  libraryAlbums: "library:albums",
  libraryArtists: "library:artists",
  libraryPlaylists: "library:playlists",
  libraryTracks: "library:tracks",
  libraryFolders: "library:folders",
  statsTopArtists: (period: string, limit: number) =>
    `stats:top-artists:${period}:${limit}`,
  statsTopTracks: (period: string, limit: number) =>
    `stats:top-tracks:${period}:${limit}`,
  statsTopAlbums: (period: string, limit: number) =>
    `stats:top-albums:${period}:${limit}`,
  statsLoved: (limit: number) => `stats:loved:${limit}`,
  album: (id: string) => `album:${id}`,
  artist: (id: string) => `artist:${id}`,
  mix: (id: string) => `mix:${id}`,
  profile: (id: string) => `profile:${id}`,
} as const;

/**
 * Hover / nav-anticipation prefetch helpers — only the ones actually
 * wired into hover handlers. Each is a no-op when the cache is fresh,
 * so callers can fire on every mouseenter without worrying about
 * flooding the backend.
 */
// Library entries get a 30s TTL just like the page-side useApi calls
// so the prefetch and the subsequent useApi mount agree on what
// counts as fresh.
const LIBRARY_PREFETCH_TTL_MS = 30 * 1000;

export const prefetch = {
  pageHome: () => prefetchApi(queryKeys.pageHome, () => api.page("home")),
  feed: () => prefetchApi(queryKeys.feed, () => api.feed()),
  popularArtists: () =>
    prefetchApi(queryKeys.popularArtists, () => api.lastfm.chartTopArtists(50)),
  popularTracks: () =>
    prefetchApi(queryKeys.popularTracks, () =>
      api.lastfm.chartTopTracksResolved(50),
    ),
  libraryAlbums: () =>
    prefetchApi(
      queryKeys.libraryAlbums,
      () => api.library.albums(),
      LIBRARY_PREFETCH_TTL_MS,
    ),
  libraryArtists: () =>
    prefetchApi(
      queryKeys.libraryArtists,
      () => api.library.artists(),
      LIBRARY_PREFETCH_TTL_MS,
    ),
  libraryPlaylists: () =>
    prefetchApi(
      queryKeys.libraryPlaylists,
      () => api.library.playlists(),
      LIBRARY_PREFETCH_TTL_MS,
    ),
  libraryTracks: () =>
    prefetchApi(
      queryKeys.libraryTracks,
      () => api.library.tracks(),
      LIBRARY_PREFETCH_TTL_MS,
    ),
  album: (id: string) => prefetchApi(queryKeys.album(id), () => api.album(id)),
  artist: (id: string) =>
    prefetchApi(queryKeys.artist(id), () => api.artist(id)),
  // Playlist detail is mutation-heavy (rename, remove tracks); skipping
  // the cache there until invalidation is wired into the edit flow.
};
