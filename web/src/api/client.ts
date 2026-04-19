import type {
  AlbumDetail,
  ArtistDetail,
  AuthStatus,
  Album,
  Artist,
  ContentKind,
  DownloadItem,
  FavoriteKind,
  FavoritesSnapshot,
  Lyrics,
  MixDetail,
  Playlist,
  PlaylistDetail,
  QualityOption,
  SearchResponse,
  Settings,
  TidalPage,
  Track,
} from "./types";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`${resp.status}: ${body || resp.statusText}`);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

export const api = {
  auth: {
    status: () => req<AuthStatus>("/api/auth/status"),
    loginStart: () => req<{ url: string; user_code: string }>("/api/auth/login/start", { method: "POST" }),
    loginPoll: () =>
      req<{ status: "idle" | "pending" | "ok" | "failed"; username?: string }>("/api/auth/login/poll"),
    logout: () => req<{ ok: true }>("/api/auth/logout", { method: "POST" }),
  },
  me: () => req<{ username: string }>("/api/me"),
  search: (q: string, limit = 20) =>
    req<SearchResponse>(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  library: {
    tracks: () => req<Track[]>("/api/library/tracks"),
    albums: () => req<Album[]>("/api/library/albums"),
    artists: () => req<Artist[]>("/api/library/artists"),
    playlists: () => req<Playlist[]>("/api/library/playlists"),
  },
  album: (id: string) => req<AlbumDetail>(`/api/album/${id}`),
  artist: (id: string) => req<ArtistDetail>(`/api/artist/${id}`),
  playlist: (id: string) => req<PlaylistDetail>(`/api/playlist/${id}`),
  mix: (id: string) => req<MixDetail>(`/api/mix/${encodeURIComponent(id)}`),
  trackLyrics: (id: string) => req<Lyrics>(`/api/track/${id}/lyrics`),
  trackRadio: (id: string) => req<Track[]>(`/api/track/${id}/radio`),
  qualities: () => req<QualityOption[]>("/api/qualities"),
  page: (name: "home" | "explore" | "genres" | "moods" | "hires") =>
    req<TidalPage>(`/api/page/${name}`),
  pagePath: (path: string) =>
    req<TidalPage>(`/api/page/resolve`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  downloaded: () => req<{ ids: string[] }>("/api/downloaded"),
  downloads: {
    list: () => req<DownloadItem[]>("/api/downloads"),
    enqueue: (kind: ContentKind, id: string, quality?: string) =>
      req<{ ok: true }>("/api/downloads", {
        method: "POST",
        body: JSON.stringify({ kind, id, quality }),
      }),
    enqueueUrl: (url: string, quality?: string) =>
      req<{ ok: true }>("/api/downloads/url", {
        method: "POST",
        body: JSON.stringify({ url, quality }),
      }),
    retry: (id: string) => req<{ ok: true }>(`/api/downloads/${id}/retry`, { method: "POST" }),
    clearCompleted: () => req<{ ok: true }>("/api/downloads/completed", { method: "DELETE" }),
    reveal: (path: string) =>
      req<{ ok: true }>("/api/reveal", { method: "POST", body: JSON.stringify({ path }) }),
  },
  settings: {
    get: () => req<Settings>("/api/settings"),
    put: (patch: Partial<Settings>) =>
      req<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),
  },
  favorites: {
    snapshot: () => req<FavoritesSnapshot>("/api/favorites"),
    add: (kind: FavoriteKind, id: string) =>
      req<{ ok: true }>(`/api/favorites/${kind}/${encodeURIComponent(id)}`, { method: "POST" }),
    remove: (kind: FavoriteKind, id: string) =>
      req<{ ok: true }>(`/api/favorites/${kind}/${encodeURIComponent(id)}`, { method: "DELETE" }),
  },
  playlists: {
    mine: () => req<Playlist[]>("/api/playlists/mine"),
    create: (title: string, description = "") =>
      req<Playlist>("/api/playlists", {
        method: "POST",
        body: JSON.stringify({ title, description }),
      }),
    delete: (id: string) =>
      req<{ ok: true }>(`/api/playlists/${encodeURIComponent(id)}`, { method: "DELETE" }),
    edit: (id: string, patch: { title?: string; description?: string }) =>
      req<Playlist>(`/api/playlists/${encodeURIComponent(id)}`, {
        method: "PUT",
        body: JSON.stringify(patch),
      }),
    addTracks: (id: string, trackIds: string[]) =>
      req<{ ok: true }>(`/api/playlists/${encodeURIComponent(id)}/tracks`, {
        method: "POST",
        body: JSON.stringify({ track_ids: trackIds }),
      }),
    removeTrack: (id: string, index: number) =>
      req<{ ok: true }>(`/api/playlists/${encodeURIComponent(id)}/tracks/${index}`, {
        method: "DELETE",
      }),
    moveTrack: (id: string, mediaId: string, position: number) =>
      req<{ ok: true }>(`/api/playlists/${encodeURIComponent(id)}/tracks/move`, {
        method: "POST",
        body: JSON.stringify({ media_id: mediaId, position }),
      }),
  },
};
