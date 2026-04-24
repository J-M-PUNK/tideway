import type {
  AlbumDetail,
  ArtistDetail,
  AuthStatus,
  Album,
  Artist,
  ContentKind,
  CreditEntry,
  DownloadItem,
  FavoriteKind,
  FavoritesSnapshot,
  LastFmChartArtist,
  LastFmChartTag,
  LastFmChartTrack,
  LastFmLovedTrack,
  LastFmPeriod,
  LastFmPlaycount,
  LastFmRecentTrack,
  LastFmStatus,
  LastFmTopAlbum,
  LastFmTopArtist,
  LastFmTopTrack,
  LastFmUserInfo,
  LastFmWeeklyScrobble,
  LocalFile,
  LocalVideo,
  Lyrics,
  MixDetail,
  PlayerSnapshot,
  Playlist,
  PlaylistFolder,
  PlaylistDetail,
  QualityOption,
  SearchResponse,
  Settings,
  TidalPage,
  TidalUser,
  Track,
  Video,
  VideoDownloadJob,
} from "./types";

// Upper bound on how long any JSON request is allowed to hang before
// we give up. Without this, a request against a dead network waits for
// the OS's TCP timeout (~30s–2min on Windows), which makes the UI look
// frozen after a drop. 15s is generous enough that slow-but-alive
// Tidal endpoints still succeed while dropped connections fail fast.
const DEFAULT_TIMEOUT_MS = 15_000;

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // Compose the caller's AbortSignal (if any) with our timeout so we
  // respect both. AbortSignal.any is available in every modern browser
  // we target; falling back to the timeout alone keeps behavior sane
  // on the rare UA where it isn't.
  const timeoutSignal = AbortSignal.timeout(DEFAULT_TIMEOUT_MS);
  const signal =
    init?.signal && typeof AbortSignal.any === "function"
      ? AbortSignal.any([init.signal, timeoutSignal])
      : (init?.signal ?? timeoutSignal);

  let resp: Response;
  try {
    resp = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      ...init,
      signal,
    });
  } catch (err) {
    // Surface timeouts as a recognizable error string so callers /
    // toasts can format them nicely instead of showing "AbortError".
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new Error(`Request timed out: ${path}`);
    }
    throw err;
  }
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`${resp.status}: ${body || resp.statusText}`);
  }
  if (resp.status === 204) return undefined as T;
  // Read as text first so an empty-but-200 response (or a non-JSON
  // payload) doesn't reject every caller with a cryptic "Unexpected end
  // of JSON input". Callers of { ok: true }-style endpoints still work.
  const text = await resp.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`Invalid JSON from ${path}: ${text.slice(0, 120)}`);
  }
}

export const api = {
  auth: {
    status: () => req<AuthStatus>("/api/auth/status"),
    loginStart: () => req<{ url: string; user_code: string }>("/api/auth/login/start", { method: "POST" }),
    loginPoll: () =>
      req<{ status: "idle" | "pending" | "ok" | "failed"; username?: string }>("/api/auth/login/poll"),
    logout: () => req<{ ok: true }>("/api/auth/logout", { method: "POST" }),
    pkceUrl: () => req<{ url: string }>("/api/auth/pkce/url"),
    pkceComplete: (redirect_url: string) =>
      req<{ status: "ok"; username: string | null }>("/api/auth/pkce/complete", {
        method: "POST",
        body: JSON.stringify({ redirect_url }),
      }),
    /** Ask the desktop shell to open a pywebview child window at
     *  Tidal's PKCE login URL and auto-capture the post-signin
     *  redirect. Returns `supported: false` when running in plain
     *  browser dev mode (no shell to call back into); frontend
     *  falls back to the copy-and-paste flow in that case. */
    inappLoginStart: () =>
      req<{ supported: boolean }>("/api/auth/login/inapp/start", {
        method: "POST",
      }),
    /** Poll this to notice when the in-app login window closes
     *  early (SSO provider detected, user closed manually). The
     *  frontend reads it alongside /api/auth/status so it can
     *  bail out of the waiting spinner and show the paste fallback
     *  without waiting for the 10-minute timeout. */
    inappLoginState: () =>
      req<{ phase: "idle" | "active" | "aborted_sso" | "closed" | "unauthorized" }>(
        "/api/auth/login/inapp/state",
      ).catch(() => ({ phase: "idle" as const })),
  },
  /** Open a Tidal URL in the user's default system browser. Exists
   *  because `window.open` for external URLs is silently dropped in
   *  pywebview's embedded WebView on every platform; the server opens
   *  it via Python's `webbrowser` module instead. */
  openExternal: (url: string) =>
    req<{ ok: true }>("/api/open-external", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  /** Force a full app shutdown. Needed because close-to-tray swallows
   *  the red-X / Cmd+Q, so the user needs an explicit exit path.
   *  Returns `{ok: false, reason}` when running outside the desktop
   *  launcher (e.g. plain browser dev mode) — the UI should hide the
   *  Quit entry in that case. */
  quitApp: () =>
    req<{ ok: boolean; reason?: string }>("/api/_internal/quit", {
      method: "POST",
    }),
  /** Spawn the compact always-on-top mini-player window. Returns
   *  {ok: false} in plain-browser dev mode. */
  openMiniPlayer: () =>
    req<{ ok: boolean; reason?: string }>("/api/_internal/mini_player", {
      method: "POST",
    }),
  /** Fire an OS-native notification. The frontend owns the decision
   *  of when to call this (only when window unfocused + pref enabled)
   *  because it has the full context. */
  notify: (title: string, body: string, subtitle?: string) =>
    req<{ ok: boolean }>("/api/notify", {
      method: "POST",
      body: JSON.stringify({ title, body, subtitle }),
    }).catch(() => ({ ok: false })),
  autostart: {
    /** Report whether the app is currently registered to launch at
     *  login. `available` is false in dev mode (the exe path isn't
     *  stable); the UI should disable the toggle in that case. */
    status: () =>
      req<{ available: boolean; enabled: boolean; path: string | null }>(
        "/api/autostart",
      ),
    set: (enabled: boolean) =>
      req<{ available: boolean; enabled: boolean; path: string | null }>(
        "/api/autostart",
        { method: "PUT", body: JSON.stringify({ enabled }) },
      ),
  },
  lastfm: {
    status: () => req<LastFmStatus>("/api/lastfm/status"),
    setCredentials: (api_key: string, api_secret: string) =>
      req<LastFmStatus>("/api/lastfm/credentials", {
        method: "PUT",
        body: JSON.stringify({ api_key, api_secret }),
      }),
    connectStart: () =>
      req<{ auth_url: string; token: string }>("/api/lastfm/connect/start", {
        method: "POST",
      }),
    connectComplete: (token: string) =>
      req<{ connected: true; username: string }>(
        "/api/lastfm/connect/complete",
        { method: "POST", body: JSON.stringify({ token }) },
      ),
    disconnect: () =>
      req<LastFmStatus>("/api/lastfm/disconnect", { method: "POST" }),
    recentTracks: (limit = 100) =>
      req<LastFmRecentTrack[]>(`/api/lastfm/recent-tracks?limit=${limit}`),
    userInfo: () => req<LastFmUserInfo>("/api/lastfm/user-info"),
    topArtists: (period: LastFmPeriod, limit = 50) =>
      req<LastFmTopArtist[]>(
        `/api/lastfm/top-artists?period=${period}&limit=${limit}`,
      ),
    topTracks: (period: LastFmPeriod, limit = 50) =>
      req<LastFmTopTrack[]>(
        `/api/lastfm/top-tracks?period=${period}&limit=${limit}`,
      ),
    topAlbums: (period: LastFmPeriod, limit = 50) =>
      req<LastFmTopAlbum[]>(
        `/api/lastfm/top-albums?period=${period}&limit=${limit}`,
      ),
    lovedTracks: (limit = 50) =>
      req<LastFmLovedTrack[]>(`/api/lastfm/loved-tracks?limit=${limit}`),
    artistPlaycount: (artist: string) =>
      req<LastFmPlaycount>(
        `/api/lastfm/artist-playcount?artist=${encodeURIComponent(artist)}`,
      ),
    albumPlaycount: (artist: string, album: string) =>
      req<LastFmPlaycount>(
        `/api/lastfm/album-playcount?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`,
      ),
    trackPlaycount: (artist: string, track: string) =>
      req<LastFmPlaycount>(
        `/api/lastfm/track-playcount?artist=${encodeURIComponent(artist)}&track=${encodeURIComponent(track)}`,
      ),
    weeklyScrobbles: (weeks = 52) =>
      req<LastFmWeeklyScrobble[]>(
        `/api/lastfm/weekly-scrobbles?weeks=${weeks}`,
      ),
    chartTopArtists: (limit = 50) =>
      req<LastFmChartArtist[]>(
        `/api/lastfm/chart/top-artists?limit=${limit}`,
      ),
    chartTopTracks: (limit = 50) =>
      req<LastFmChartTrack[]>(
        `/api/lastfm/chart/top-tracks?limit=${limit}`,
      ),
    chartTopTags: (limit = 50) =>
      req<LastFmChartTag[]>(`/api/lastfm/chart/top-tags?limit=${limit}`),
    nowPlaying: (track: {
      artist: string;
      title: string;
      album?: string;
      duration?: number;
    }) =>
      req<{ ok: boolean }>("/api/lastfm/now-playing", {
        method: "POST",
        body: JSON.stringify({
          artist: track.artist,
          track: track.title,
          album: track.album ?? "",
          duration: track.duration ?? 0,
        }),
      }),
    scrobble: (track: {
      artist: string;
      title: string;
      album?: string;
      duration?: number;
      timestamp?: number;
    }) =>
      req<{ ok: boolean }>("/api/lastfm/scrobble", {
        method: "POST",
        body: JSON.stringify({
          artist: track.artist,
          track: track.title,
          album: track.album ?? "",
          duration: track.duration ?? 0,
          timestamp: track.timestamp ?? null,
        }),
      }),
  },
  spotify: {
    /** Spotify's global play count for a recording identified by
     *  ISRC. Returns `{playcount: null}` when Spotify doesn't know
     *  the recording or the private API is unreachable — callers
     *  should degrade the badge silently rather than showing an
     *  error. */
    trackPlaycount: (isrc: string) =>
      req<{ playcount: number | null }>(
        `/api/spotify/track-playcount?isrc=${encodeURIComponent(isrc)}`,
      ),
    /** Sum of per-track Spotify play counts across an album,
     *  computed server-side. Pass every track ISRC on the album.
     *  `resolved < total` means Spotify couldn't find some tracks;
     *  the `total_plays` is still a (lower-bound) number worth
     *  rendering. */
    albumTotalPlays: (isrcs: string[]) =>
      req<{ total_plays: number; resolved: number; total: number }>(
        `/api/spotify/album-total-plays?isrcs=${encodeURIComponent(
          isrcs.join(","),
        )}`,
      ),
    /** Artist-level stats from Spotify: monthly listeners, followers,
     *  world rank, top cities. `sampleIsrc` is any ISRC from a track
     *  by the artist — used once to resolve Tidal artist → Spotify
     *  artist, then the mapping is cached indefinitely. */
    artistStats: (tidalArtistId: string, sampleIsrc: string) =>
      req<{
        spotify_artist_id?: string;
        name?: string;
        monthly_listeners: number | null;
        followers: number | null;
        world_rank: number | null;
        top_cities: { city: string; country: string; listeners: number }[];
      }>(
        `/api/spotify/artist-stats?tidal_artist_id=${encodeURIComponent(
          tidalArtistId,
        )}&sample_isrc=${encodeURIComponent(sampleIsrc)}`,
      ),
  },
  me: () => req<{ username: string }>("/api/me"),
  version: () => req<{ version: string }>("/api/version"),
  updateCheck: () =>
    req<{
      available: boolean;
      current: string;
      latest: string | null;
      url: string | null;
      notes: string | null;
    }>("/api/update-check"),
  /** Download the current-platform installer from the latest release
   *  and open it. Caller should quit the app shortly after so the old
   *  bundle is out of the way when the user runs the installer. */
  updateInstall: () =>
    req<{ ok: boolean; downloaded_to: string | null; reason?: string }>(
      "/api/update/install",
      { method: "POST" },
    ),
  playReportLog: () =>
    req<{
      entries: {
        ts_ms: number;
        phase: "sent" | "skipped";
        track_id: string;
        http_status: number | null;
        listened_s?: number;
        client_id?: string;
        note?: string;
      }[];
    }>("/api/play-report/log"),
  playReportDiagnose: (trackId?: number) =>
    req<{
      ok: boolean;
      reason?: string;
      entry?: {
        ts_ms: number;
        phase: string;
        track_id: string;
        http_status: number | null;
        note?: string;
      };
    }>("/api/play-report/diagnose", {
      method: "POST",
      body: JSON.stringify(trackId ? { track_id: trackId } : {}),
    }),
  import: {
    // Shared across every import source — Spotify / M3U / Deezer all
    // funnel into this after their own match step. The older
    // /api/import/spotify/create path still works as a legacy alias.
    create: (name: string, description: string, trackIds: string[]) =>
      req<{
        playlist_id: string;
        added: number;
        failed: number;
        name: string;
      }>("/api/import/create", {
        method: "POST",
        body: JSON.stringify({
          name,
          description,
          track_ids: trackIds,
        }),
      }),
    deezer: {
      match: (source: string) =>
        req<{
          rows: {
            spotify: {
              name: string;
              artists: string[];
              duration_ms: number;
              isrc: string | null;
            };
            match: {
              tidal_id: string;
              name: string;
              artists: string[];
              duration: number;
              cover: string | null;
              confidence: number;
              reason: string;
            } | null;
          }[];
          total: number;
          matched: number;
          playlist: { name: string; description: string };
        }>("/api/import/deezer/match", {
          method: "POST",
          body: JSON.stringify({ source }),
        }),
    },
    text: {
      parse: (text: string) =>
        req<{
          rows: {
            spotify: {
              name: string;
              artists: string[];
              duration_ms: number;
              isrc: string | null;
            };
            match: {
              tidal_id: string;
              name: string;
              artists: string[];
              duration: number;
              cover: string | null;
              confidence: number;
              reason: string;
            } | null;
          }[];
          total: number;
          matched: number;
        }>("/api/import/text/parse", {
          method: "POST",
          body: JSON.stringify({ text }),
        }),
    },
    spotify: {
      status: () =>
        req<{
          connected: boolean;
          username: string | null;
          client_id_set: boolean;
          redirect_uri: string;
        }>("/api/import/spotify/status"),
      connect: (clientId: string) =>
        req<{ auth_url: string }>("/api/import/spotify/connect", {
          method: "POST",
          body: JSON.stringify({ client_id: clientId }),
        }),
      disconnect: () =>
        req<{ ok: boolean }>("/api/import/spotify/disconnect", {
          method: "POST",
        }),
      playlists: () =>
        req<
          {
            id: string;
            name: string;
            tracks: number;
            image: string | null;
            owner: string;
            description: string;
          }[]
        >("/api/import/spotify/playlists"),
      match: (playlistId: string) =>
        req<{
          rows: {
            spotify: {
              name: string;
              artists: string[];
              duration_ms: number;
              isrc: string | null;
            };
            match: {
              tidal_id: string;
              name: string;
              artists: string[];
              duration: number;
              cover: string | null;
              confidence: number;
              reason: string;
            } | null;
          }[];
          total: number;
          matched: number;
        }>("/api/import/spotify/match", {
          method: "POST",
          body: JSON.stringify({ playlist_id: playlistId }),
        }),
      create: (name: string, description: string, trackIds: string[]) =>
        req<{
          playlist_id: string;
          added: number;
          failed: number;
          name: string;
        }>("/api/import/spotify/create", {
          method: "POST",
          body: JSON.stringify({
            name,
            description,
            track_ids: trackIds,
          }),
        }),
      matchLibrary: (
        kind: "liked-tracks" | "saved-albums" | "followed-artists",
      ) =>
        req<{
          rows: {
            spotify: {
              name: string;
              artists: string[];
              duration_ms: number;
              isrc: string | null;
            };
            match: {
              tidal_id: string;
              name: string;
              artists: string[];
              duration: number;
              cover: string | null;
              confidence: number;
              reason: string;
            } | null;
          }[];
          total: number;
          matched: number;
        }>(`/api/import/spotify/${kind}/match`, { method: "POST" }),
    },
    favorite: (
      kind: "track" | "album" | "artist",
      ids: string[],
    ) =>
      req<{ kind: string; added: number; failed: number }>(
        "/api/import/favorite",
        {
          method: "POST",
          body: JSON.stringify({ kind, ids }),
        },
      ),
  },
  user: {
    profile: (id: string) => req<TidalUser>(`/api/user/${encodeURIComponent(id)}`),
    playlists: (id: string) =>
      req<Playlist[]>(`/api/user/${encodeURIComponent(id)}/playlists`),
    followers: (id: string) =>
      req<TidalUser[]>(`/api/user/${encodeURIComponent(id)}/followers`),
    following: (id: string) =>
      req<TidalUser[]>(`/api/user/${encodeURIComponent(id)}/following`),
    follow: (id: string) =>
      req<{ ok: boolean; error?: string }>(
        `/api/user/${encodeURIComponent(id)}/follow`,
        { method: "POST" },
      ),
    unfollow: (id: string) =>
      req<{ ok: boolean; error?: string }>(
        `/api/user/${encodeURIComponent(id)}/follow`,
        { method: "DELETE" },
      ),
    isFollowing: (id: string) =>
      req<{ following: boolean }>(
        `/api/me/following/status/${encodeURIComponent(id)}`,
      ),
    counts: (id: string) =>
      req<{ followers: number; following: number }>(
        `/api/user/${encodeURIComponent(id)}/counts`,
      ),
  },
  // Tidal's "event producer" — play reporting so tracks count for
  // Recently Played, recommendations, and artist royalties. Fire-and-
  // forget from the caller's perspective; the server queues and sends.
  playReport: {
    start: () =>
      req<{ session_id: string; ts_ms: number }>("/api/play-report/start", {
        method: "POST",
        body: JSON.stringify({}),
      }),
    stop: (body: {
      session_id: string;
      track_id: string;
      quality: string;
      source_type?: string | null;
      source_id?: string | null;
      start_ts_ms: number;
      end_ts_ms: number;
      start_position_s: number;
      end_position_s: number;
    }) =>
      req<{ ok: boolean }>("/api/play-report/stop", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },
  mixes: () =>
    req<{ kind: "mix"; id: string; name: string; subtitle: string; cover: string | null }[]>(
      "/api/mixes",
    ),
  search: (q: string, limit = 20) =>
    req<SearchResponse>(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  library: {
    tracks: () => req<Track[]>("/api/library/tracks"),
    albums: () => req<Album[]>("/api/library/albums"),
    artists: () => req<Artist[]>("/api/library/artists"),
    playlists: () => req<Playlist[]>("/api/library/playlists"),
    local: () =>
      req<{
        output_dir: string;
        videos_dir: string;
        files: LocalFile[];
        videos: LocalVideo[];
      }>("/api/library/local"),
    folders: {
      list: (parentId: string = "root") =>
        req<PlaylistFolder[]>(
          `/api/library/folders?parent_id=${encodeURIComponent(parentId)}`,
        ),
      playlists: (folderId: string) =>
        req<Playlist[]>(
          `/api/library/folders/${encodeURIComponent(folderId)}/playlists`,
        ),
      create: (name: string, parentId: string = "root") =>
        req<PlaylistFolder>("/api/library/folders", {
          method: "POST",
          body: JSON.stringify({ name, parent_id: parentId }),
        }),
      rename: (folderId: string, name: string) =>
        req<{ ok: boolean }>(
          `/api/library/folders/${encodeURIComponent(folderId)}`,
          { method: "PATCH", body: JSON.stringify({ name }) },
        ),
      delete: (folderId: string) =>
        req<{ ok: boolean }>(
          `/api/library/folders/${encodeURIComponent(folderId)}`,
          { method: "DELETE" },
        ),
      movePlaylists: (folderId: string, playlistIds: string[]) =>
        req<{ ok: boolean }>(
          `/api/library/folders/${encodeURIComponent(folderId)}/playlists`,
          { method: "POST", body: JSON.stringify({ playlist_ids: playlistIds }) },
        ),
    },
  },
  album: (id: string) => req<AlbumDetail>(`/api/album/${id}`),
  albumCredits: (id: string) =>
    req<
      {
        track_id: string;
        track_num: number;
        title: string;
        artists: { id: string | null; name: string }[];
        credits: CreditEntry[];
      }[]
    >(`/api/album/${id}/credits`),
  artist: (id: string) => req<ArtistDetail>(`/api/artist/${id}`),
  artistRadio: (id: string) => req<Track[]>(`/api/artist/${id}/radio`),
  artistCredits: (id: string) =>
    req<(Track & { role: string })[]>(`/api/artist/${id}/credits`),
  artistVideos: (id: string) => req<Video[]>(`/api/artist/${id}/videos`),
  video: (id: string) => req<Video>(`/api/video/${id}`),
  videoStream: (id: string, quality?: string) =>
    req<{ url: string }>(
      quality
        ? `/api/video/${id}/stream?quality=${encodeURIComponent(quality)}`
        : `/api/video/${id}/stream`,
    ),
  videoCredits: (id: string) => req<CreditEntry[]>(`/api/video/${id}/credits`),
  videoSimilar: (id: string) => req<Video[]>(`/api/video/${id}/similar`),
  videoDownloadStart: (id: string, quality?: string) =>
    req<VideoDownloadJob>(`/api/video/${id}/download`, {
      method: "POST",
      body: JSON.stringify(quality ? { quality } : {}),
    }),
  videoDownloadStatus: (id: string) =>
    req<VideoDownloadJob>(`/api/video/${id}/download`),
  videoDownloadsList: () =>
    req<VideoDownloadJob[]>(`/api/video/downloads`),
  /** Open the OS file manager with the given path highlighted.
   *  No-ops silently in plain browser mode. */
  revealInFinder: (path: string) =>
    req<{ ok: boolean }>(`/api/reveal`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }).catch(() => ({ ok: false })),
  playlist: (id: string) => req<PlaylistDetail>(`/api/playlist/${id}`),
  mix: (id: string) => req<MixDetail>(`/api/mix/${encodeURIComponent(id)}`),
  track: (id: string) => req<Track>(`/api/track/${id}`),
  lastfmTrackPlaycountsBatch: (
    items: { artist: string; track: string }[],
  ) =>
    req<{
      results: Record<string, LastFmPlaycount>;
    }>("/api/lastfm/track-playcounts", {
      method: "POST",
      body: JSON.stringify({ items }),
    }),
  trackLyrics: (id: string) => req<Lyrics>(`/api/track/${id}/lyrics`),
  trackRadio: (id: string) => req<Track[]>(`/api/track/${id}/radio`),
  trackCredits: (id: string) => req<CreditEntry[]>(`/api/track/${id}/credits`),
  qualities: () => req<QualityOption[]>("/api/qualities"),
  page: (name: "home" | "explore" | "genres" | "moods" | "hires") =>
    req<TidalPage>(`/api/page/${name}`),
  pagePath: (path: string) =>
    req<TidalPage>(`/api/page/resolve`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  downloaded: () => req<{ ids: string[] }>("/api/downloaded"),
  feed: () =>
    req<{
      items: (Album & { released_at: string })[];
      editorial: TidalPage | null;
    }>("/api/feed"),
  player: {
    available: () =>
      req<{ available: boolean }>("/api/player/available"),
    state: () =>
      req<PlayerSnapshot>("/api/player/state"),
    load: (trackId: string, quality?: string) =>
      req<PlayerSnapshot>("/api/player/load", {
        method: "POST",
        body: JSON.stringify({ track_id: trackId, quality }),
      }),
    /** Combined load + play in one call. Saves a network round-trip
     *  for auto-advance and keeps set_media + play back-to-back
     *  under the same backend lock so the decoder starts priming
     *  as soon as possible. */
    playTrack: (trackId: string, quality?: string) =>
      req<PlayerSnapshot>("/api/player/play_track", {
        method: "POST",
        body: JSON.stringify({ track_id: trackId, quality }),
      }),
    /** Pre-resolve the next track's manifest so auto-advance skips
     *  the network fetch. Fire-and-forget — a failed preload just
     *  means the subsequent load() pays the full cost as before. */
    preload: (trackId: string, quality?: string) =>
      req<{ ok: boolean; cached: boolean; hit?: boolean }>(
        "/api/player/preload",
        {
          method: "POST",
          body: JSON.stringify({ track_id: trackId, quality }),
        },
      ).catch(() => ({ ok: false, cached: false })),
    preloadClear: () =>
      req<{ ok: boolean }>("/api/player/preload/clear", {
        method: "POST",
      }).catch(() => ({ ok: false })),
    /** Warm the stream-manifest cache for a set of tracks so the
     *  next click skips the Tidal track→stream→manifest round-trips.
     *  Fire-and-forget: errors are swallowed. Called from hover on
     *  track rows (single id) and on album / playlist mount
     *  (batched). */
    prefetch: (trackIds: string[], quality?: string) =>
      req<{ prefetched: number; total: number }>("/api/player/prefetch", {
        method: "POST",
        body: JSON.stringify({ track_ids: trackIds, quality }),
      }).catch(() => ({ prefetched: 0, total: trackIds.length })),
    play: () =>
      req<PlayerSnapshot>("/api/player/play", { method: "POST" }),
    pause: () =>
      req<PlayerSnapshot>("/api/player/pause", { method: "POST" }),
    resume: () =>
      req<PlayerSnapshot>("/api/player/resume", { method: "POST" }),
    stop: () =>
      req<PlayerSnapshot>("/api/player/stop", { method: "POST" }),
    seek: (fraction: number) =>
      req<PlayerSnapshot>("/api/player/seek", {
        method: "POST",
        body: JSON.stringify({ fraction }),
      }),
    volume: (volume: number) =>
      req<PlayerSnapshot>("/api/player/volume", {
        method: "POST",
        body: JSON.stringify({ volume }),
      }),
    muted: (muted: boolean) =>
      req<PlayerSnapshot>("/api/player/muted", {
        method: "POST",
        body: JSON.stringify({ muted }),
      }),
    eq: () =>
      req<{
        enabled: boolean;
        bands: number[];
        preamp: number | null;
        band_count: number;
        frequencies: number[];
        presets: { index: number; name: string }[];
      }>("/api/player/eq"),
    setEq: (bands: number[], preamp: number | null) =>
      req<{
        ok: boolean;
        enabled: boolean;
        bands: number[];
        preamp: number | null;
      }>("/api/player/eq", {
        method: "POST",
        body: JSON.stringify({ bands, preamp }),
      }),
    setEqPreset: (preset: number) =>
      req<{ ok: boolean; enabled: boolean; bands: number[] }>(
        "/api/player/eq/preset",
        { method: "POST", body: JSON.stringify({ preset }) },
      ),
    setEqEnabled: (enabled: boolean) =>
      req<{ ok: boolean; enabled: boolean }>("/api/player/eq/enabled", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      }),
    outputDevices: () =>
      req<{
        devices: { id: string; name: string }[];
        current: string;
      }>("/api/player/output-devices"),
    setOutputDevice: (deviceId: string) =>
      req<{ ok: boolean; device_id: string }>("/api/player/output-device", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      }),
  },
  airplay: {
    devices: () =>
      req<{
        available: boolean;
        reason?: string | null;
        devices: {
          id: string;
          name: string;
          address: string;
          has_raop: boolean;
          paired: boolean;
        }[];
        connected_id: string | null;
      }>("/api/airplay/devices"),
    pairStart: (deviceId: string) =>
      req<{ ok: boolean }>("/api/airplay/pair/start", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      }),
    pairPin: (pin: string) =>
      req<{ ok: boolean }>("/api/airplay/pair/pin", {
        method: "POST",
        body: JSON.stringify({ pin }),
      }),
    pairCancel: () =>
      req<{ ok: boolean }>("/api/airplay/pair/cancel", { method: "POST" }),
    connect: (deviceId: string) =>
      req<{ ok: boolean; connected_id: string }>("/api/airplay/connect", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      }),
    disconnect: () =>
      req<{ ok: boolean }>("/api/airplay/disconnect", { method: "POST" }),
  },
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
    enqueueBulk: (
      items: { kind: "track" | "album" | "playlist"; id: string }[],
      quality?: string,
    ) =>
      req<{ submitted: number }>("/api/downloads/bulk", {
        method: "POST",
        body: JSON.stringify({ items, quality }),
      }),
    retry: (id: string, quality?: string) =>
      req<{ ok: true }>(`/api/downloads/${id}/retry`, {
        method: "POST",
        body: JSON.stringify({ quality }),
      }),
    clearCompleted: () => req<{ ok: true }>("/api/downloads/completed", { method: "DELETE" }),
    cancel: (id: string) =>
      req<{ ok: true }>(`/api/downloads/${id}`, { method: "DELETE" }),
    cancelAll: () =>
      req<{ cancelled: number }>("/api/downloads/active", { method: "DELETE" }),
    reveal: (path: string) =>
      req<{ ok: true }>("/api/reveal", { method: "POST", body: JSON.stringify({ path }) }),
    stats: () =>
      req<{ output_dir: string; total_bytes: number; file_count: number }>(
        "/api/downloads/stats",
      ),
    state: () => req<{ paused: boolean }>("/api/downloads/state"),
    pause: () => req<{ paused: true }>("/api/downloads/pause", { method: "POST" }),
    resume: () => req<{ paused: false }>("/api/downloads/resume", { method: "POST" }),
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
    bulk: (kind: FavoriteKind, ids: string[], add = true) =>
      req<{ submitted: number }>("/api/favorites/bulk", {
        method: "POST",
        body: JSON.stringify({ kind, ids, add }),
      }),
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
