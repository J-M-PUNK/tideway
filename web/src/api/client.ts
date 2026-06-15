import type {
  AlbumDetail,
  ArtistDetail,
  ArtistExtras,
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
  AotyAlbum,
  AotyGenre,
  LocalFile,
  LocalVideo,
  Lyrics,
  MixDetail,
  ManualEqConfig,
  ParametricBand,
  PlayerSnapshot,
  Playlist,
  PlaylistFolder,
  PlaylistDetail,
  QualityOption,
  SearchResponse,
  Settings,
  SignalPath,
  SubscriptionStatus,
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
// Known-slow endpoints (server-side fan-outs that take ~18s+ cold)
// pass an opts.timeoutMs override; see `chartTopTracksResolved`.
const DEFAULT_TIMEOUT_MS = 15_000;

/** Optional per-call overrides for `req()`. */
interface ReqOptions {
  /** Override the default 15-second request timeout. Use for
   *  endpoints documented to take longer than 15s on a cold call
   *  (currently `chart-top-tracks-resolved` at ~18s). */
  timeoutMs?: number;
}

async function req<T>(
  path: string,
  init?: RequestInit,
  opts?: ReqOptions,
): Promise<T> {
  // Compose the caller's AbortSignal (if any) with our timeout so we
  // respect both. AbortSignal.any is available in every modern browser
  // we target; falling back to the timeout alone keeps behavior sane
  // on the rare UA where it isn't.
  const timeoutMs = opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
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
    loginStart: () =>
      req<{ url: string; user_code: string }>("/api/auth/login/start", {
        method: "POST",
      }),
    loginPoll: () =>
      req<{ status: "idle" | "pending" | "ok" | "failed"; username?: string }>(
        "/api/auth/login/poll",
      ),
    logout: () => req<{ ok: true }>("/api/auth/logout", { method: "POST" }),
    pkceUrl: () => req<{ url: string }>("/api/auth/pkce/url"),
    pkceComplete: (redirect_url: string) =>
      req<{ status: "ok"; username: string | null }>(
        "/api/auth/pkce/complete",
        {
          method: "POST",
          body: JSON.stringify({ redirect_url }),
        },
      ),
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
      req<{
        phase: "idle" | "active" | "aborted_sso" | "closed" | "unauthorized";
      }>("/api/auth/login/inapp/state").catch(() => ({
        phase: "idle" as const,
      })),
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
  /** Force a full app shutdown from the in-app Quit menu. Equivalent
   *  to clicking the native red-X. Returns `{ok: false, reason}` when
   *  running outside the desktop launcher (e.g. plain browser dev
   *  mode) — the UI should hide the Quit entry in that case. */
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
  /** Integrated window chrome — drives the React-rendered titlebar on
   *  Windows where the native min/max/close buttons are suppressed.
   *  `info` is fetched once on mount; the action endpoints reach the
   *  pywebview window via callbacks registered by desktop.py. */
  window: {
    info: () =>
      req<{
        ok: boolean;
        platform?: "win32" | "darwin" | "linux" | string;
        frameless?: boolean;
        maximized?: boolean;
        launcher?: boolean;
        reason?: string;
      }>("/api/_internal/window/info").catch(() => ({ ok: false as const })),
    minimize: () =>
      req<{ ok: boolean }>("/api/_internal/window/minimize", {
        method: "POST",
      }),
    maximize: () =>
      req<{ ok: boolean; maximized?: boolean }>(
        "/api/_internal/window/maximize",
        { method: "POST" },
      ),
    close: () =>
      req<{ ok: boolean }>("/api/_internal/window/close", { method: "POST" }),
    /** Start a native drag from the cursor's current position. The
     *  React titlebar calls this on mousedown — WebView2 ignores
     *  `app-region: drag`, so we route through Win32's move loop
     *  via PostMessage(WM_NCLBUTTONDOWN, HTCAPTION) on the backend. */
    startDrag: () =>
      req<{ ok: boolean }>("/api/_internal/window/start_drag", {
        method: "POST",
      }),
    /** Start a native resize loop in the given direction. Invisible
     *  edge / corner hit strips on the React shell fire this on
     *  mousedown — WS_THICKFRAME alone isn't enough because the
     *  WebView2 child covers the NC resize zones. */
    startResize: (
      direction:
        | "left"
        | "right"
        | "top"
        | "bottom"
        | "topleft"
        | "topright"
        | "bottomleft"
        | "bottomright",
    ) =>
      req<{ ok: boolean }>("/api/_internal/window/start_resize", {
        method: "POST",
        body: JSON.stringify({ direction }),
      }),
  },
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
    userInfo: () => req<LastFmUserInfo | null>("/api/lastfm/user-info"),
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
      req<LastFmChartArtist[]>(`/api/lastfm/chart/top-artists?limit=${limit}`),
    chartTopTracks: (limit = 50) =>
      req<LastFmChartTrack[]>(`/api/lastfm/chart/top-tracks?limit=${limit}`),
    /** Last.fm top tracks pre-resolved to Tidal Track objects. One
     *  round-trip instead of the N+1 resolve-on-client pattern. */
    chartTopTracksResolved: (limit = 50) =>
      // Cold-load takes ~18s server-side (Last.fm chart fetch + N
      // parallel Tidal search resolves for each entry). The default
      // 15s timeout aborts before the server finishes, so the
      // Popular Tracks tab always errored on first load. The
      // resolved list is cached for an hour after that first
      // resolve, so the 60s budget is paid at most once per hour.
      req<Track[]>(
        `/api/lastfm/chart/top-tracks-resolved?limit=${limit}`,
        undefined,
        { timeoutMs: 60_000 },
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
  aoty: {
    /** Top-rated albums of the given year per AlbumOfTheYear, with
     *  each entry decorated with a Tidal album dict when one exists.
     *  Year defaults server-side to the current year. */
    topOfYear: (opts?: { year?: number; limit?: number; genre?: string }) => {
      const params = new URLSearchParams();
      if (opts?.year !== undefined) params.set("year", String(opts.year));
      if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
      if (opts?.genre) params.set("genre", opts.genre);
      const qs = params.toString();
      return req<AotyAlbum[]>(`/api/aoty/top-of-year${qs ? `?${qs}` : ""}`);
    },
    /** Recently-released albums per AOTY's /releases/ grid, with each
     *  entry decorated with a Tidal album dict when one exists. */
    recentReleases: (limit = 30) =>
      req<AotyAlbum[]>(`/api/aoty/recent-releases?limit=${limit}`),
    /** AOTY's genre list ({slug, name}) for the New-releases genre
     *  picker. */
    genres: () => req<AotyGenre[]>(`/api/aoty/genres`),
    /** Recent albums for one AOTY genre (the "Recent {Genre}
     *  Albums" section of /genre/{slug}/), each decorated with a
     *  Tidal album dict when one exists. */
    genreReleases: (slug: string, limit = 60) =>
      req<AotyAlbum[]>(
        `/api/aoty/genre-releases?genre=${encodeURIComponent(slug)}&limit=${limit}`,
      ),
    /** Scraper health. `blocked` is true when AOTY has recently
     *  served us a Cloudflare challenge instead of HTML — the
     *  Home page reads this to render a "report on GitHub" notice
     *  rather than silently hiding the AOTY rows. */
    status: () =>
      req<{ blocked: boolean; issues_url: string }>(`/api/aoty/status`),
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
    /** Batched playcount lookup with optional fuzzy fallback. Pass
     *  the tracks in full so the server can fall back to a
     *  title + primary-artist search when Spotify's ISRC index
     *  doesn't have a given recording (common for feature-version
     *  ISRCs and brand-new releases).
     *
     *  `refresh: true` drops stale null/zero cache entries first,
     *  so pages where completeness matters (like Popular) self-heal
     *  from an earlier throttle hit at the cost of extra Spotify
     *  round-trips on this one call.
     *
     *  Returns `{playcounts: {ISRC: number | null}}`. */
    trackPlaycounts: (
      tracks: { isrc: string; title?: string; artist?: string }[],
      opts?: { refresh?: boolean },
    ) =>
      req<{ playcounts: Record<string, number | null> }>(
        `/api/spotify/track-playcounts`,
        {
          method: "POST",
          body: JSON.stringify({ tracks, refresh: !!opts?.refresh }),
        },
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
     *  world rank, top cities. Pass the Tidal artist's name plus a
     *  list of ISRCs from their top tracks; the resolver picks the
     *  ISRC whose Spotify primary artist matches the Tidal name, so
     *  feature-credits don't pivot us onto the host's stats. */
    artistStats: (
      tidalArtistId: string,
      tidalArtistName: string,
      sampleIsrcs: string[],
    ) => {
      const params = new URLSearchParams({
        tidal_artist_id: tidalArtistId,
      });
      if (tidalArtistName) {
        params.set("tidal_artist_name", tidalArtistName);
      }
      const cleaned = (sampleIsrcs || [])
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      if (cleaned.length > 0) {
        params.set("sample_isrcs", cleaned.join(","));
        // Keep `sample_isrc` for the very-old server case, harmless
        // to send when the new params are also set.
        params.set("sample_isrc", cleaned[0]);
      }
      return req<{
        spotify_artist_id?: string;
        name?: string;
        monthly_listeners: number | null;
        followers: number | null;
        world_rank: number | null;
        top_cities: { city: string; country: string; listeners: number }[];
      }>(`/api/spotify/artist-stats?${params.toString()}`);
    },
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
      // "flatpak" when the backend detects /.flatpak-info or
      // $FLATPAK_ID; "installer" everywhere else (macOS, Windows,
      // and the Linux AppImage path). The banner uses this to swap
      // the in-app "Install now" button for a "managed by Flatpak"
      // hint, since downloading and execing an AppImage from inside
      // a Flatpak sandbox can't update the installed app.
      kind?: "flatpak" | "installer";
      // Non-null when the GitHub fetch itself failed (offline, cert
      // verification problem, rate limit, repo private). The banner
      // doesn't display this, but it's useful for `/api/health`-style
      // diagnosis when a user reports "I'm not seeing update banners".
      error?: string | null;
    }>("/api/update-check"),
  /** Download the current-platform installer from the latest release
   *  and open it. Caller should quit the app shortly after so the old
   *  bundle is out of the way when the user runs the installer. */
  updateInstall: () =>
    req<{ ok: boolean; downloaded_to: string | null; reason?: string }>(
      "/api/update/install",
      { method: "POST" },
    ),
  /** Write a diagnostic snapshot to ~/Downloads. Backed by an
   *  unauthenticated endpoint so the user can capture state even
   *  while signed out. Returns the absolute path of the file the
   *  server wrote so the UI can toast it.
   */
  saveActivityReport: () =>
    req<{ path: string; size_bytes: number; report_schema: number }>(
      "/api/diagnostics/save-activity-report",
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
          /** Populated when token exchange succeeded but a follow-up
           *  Spotify API call (`/me`) was rejected. Most commonly
           *  fires when the Developer app's owner doesn't have
           *  active Spotify Premium. UI should show this verbatim
           *  if present. */
          auth_error?: string | null;
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
        filters?: {
          /** YYYY-MM-DD inclusive lower bound on Spotify added_at.
           *  Ignored for followed-artists (Spotify doesn't expose a
           *  follow timestamp). */
          since?: string;
          /** YYYY-MM-DD inclusive upper bound. */
          until?: string;
          /** Restrict saved albums to one Spotify release type.
           *  Ignored for liked-tracks and followed-artists. */
          albumType?: "album" | "single" | "compilation";
        },
      ) => {
        const params = new URLSearchParams();
        if (filters?.since) params.set("since", filters.since);
        if (filters?.until) params.set("until", filters.until);
        if (filters?.albumType && kind === "saved-albums") {
          params.set("album_type", filters.albumType);
        }
        const qs = params.toString();
        const path = qs
          ? `/api/import/spotify/${kind}/match?${qs}`
          : `/api/import/spotify/${kind}/match`;
        return req<{
          rows: {
            spotify: {
              name: string;
              artists: string[];
              duration_ms: number;
              isrc: string | null;
              added_at?: string | null;
              album_type?: string | null;
              total_tracks?: number;
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
          raw_total?: number;
        }>(path, { method: "POST" });
      },
    },
    favorite: (kind: "track" | "album" | "artist", ids: string[]) =>
      req<{ kind: string; added: number; failed: number }>(
        "/api/import/favorite",
        {
          method: "POST",
          body: JSON.stringify({ kind, ids }),
        },
      ),
  },
  user: {
    profile: (id: string) =>
      req<TidalUser>(`/api/user/${encodeURIComponent(id)}`),
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
    req<
      {
        kind: "mix";
        id: string;
        name: string;
        subtitle: string;
        cover: string | null;
      }[]
    >("/api/mixes"),
  search: (q: string, limit = 20) =>
    req<SearchResponse>(
      `/api/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
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
          {
            method: "POST",
            body: JSON.stringify({ playlist_ids: playlistIds }),
          },
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
  artistExtras: (id: string) => req<ArtistExtras>(`/api/artist/${id}/extras`),
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
  videoDownloadsList: () => req<VideoDownloadJob[]>(`/api/video/downloads`),
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
  lastfmTrackPlaycountsBatch: (items: { artist: string; track: string }[]) =>
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
  /** Subscription tier + download eligibility. UI uses this to gate
   *  the Download buttons — lossy-only accounts get a tooltip
   *  explaining why downloads aren't enabled. */
  subscription: () => req<SubscriptionStatus>("/api/subscription"),
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
    available: () => req<{ available: boolean }>("/api/player/available"),
    state: () => req<PlayerSnapshot>("/api/player/state"),
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
    /** Push display metadata for the currently-playing track into
     *  macOS Now Playing. Fired on track change so Control Center,
     *  the menu-bar widget, and the lock screen show title / artist
     *  / album / duration. No-ops on non-macOS server-side. */
    nowPlaying: (info: {
      title: string;
      artist: string;
      album?: string;
      duration_ms?: number;
      artwork_url?: string;
    }) =>
      req<{ ok: boolean }>("/api/now-playing", {
        method: "POST",
        body: JSON.stringify(info),
      }).catch(() => ({ ok: false })),
    play: () => req<PlayerSnapshot>("/api/player/play", { method: "POST" }),
    pause: () => req<PlayerSnapshot>("/api/player/pause", { method: "POST" }),
    /** Clear the cross-device pause banner without resuming
     *  playback. Called when the user dismisses the "Paused —
     *  playing on iOS" alert with its X button. */
    dismissPauseReason: () =>
      req<PlayerSnapshot>("/api/player/dismiss-pause-reason", {
        method: "POST",
      }),
    resume: () => req<PlayerSnapshot>("/api/player/resume", { method: "POST" }),
    stop: () => req<PlayerSnapshot>("/api/player/stop", { method: "POST" }),
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
        bands: ParametricBand[];
        preamp: number | null;
        config: ManualEqConfig;
        default_bands: ParametricBand[];
        presets: { index: number; name: string; bands: ParametricBand[] }[];
      }>("/api/player/eq"),
    setEq: (bands: ParametricBand[], preamp: number | null) =>
      req<{
        ok: boolean;
        enabled: boolean;
        bands: ParametricBand[];
        preamp: number | null;
      }>("/api/player/eq", {
        method: "POST",
        body: JSON.stringify({ bands, preamp }),
      }),
    setEqPreset: (preset: number) =>
      req<{ ok: boolean; enabled: boolean; bands: ParametricBand[] }>(
        "/api/player/eq/preset",
        { method: "POST", body: JSON.stringify({ preset }) },
      ),
    setEqEnabled: (enabled: boolean) =>
      req<{ ok: boolean; enabled: boolean }>("/api/player/eq/enabled", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      }),
    /** AutoEQ headphone-profile endpoints (see
     *  docs/autoeq-headphone-profiles-scope.md). The profile
     *  catalog is bundled in the desktop app; the picker fetches
     *  matches from the index built at server startup. */
    autoEqList: (q: string, limit = 50) =>
      req<{
        total: number;
        profiles: {
          id: string;
          brand: string;
          model: string;
          source: string;
          preamp_db: number;
          band_count: number;
        }[];
      }>(`/api/eq/profiles?q=${encodeURIComponent(q)}&limit=${limit}`),
    autoEqState: () =>
      req<{
        mode: "off" | "manual" | "profile";
        enabled: boolean;
        bypass: boolean;
        active_profile_id: string;
        active_profile: {
          id: string;
          brand: string;
          model: string;
          source: string;
          preamp_db: number;
          band_count: number;
        } | null;
        manual_bands: ParametricBand[];
        manual_preamp_db: number | null;
        profile_catalog_size: number;
        tilt: {
          preamp_offset_db: number;
          bass_db: number;
          treble_db: number;
        };
      }>("/api/eq/state"),
    autoEqLoadProfile: (profileId: string) =>
      req<{
        ok: boolean;
        mode: string;
        active_profile_id: string;
        active_profile: {
          id: string;
          brand: string;
          model: string;
          source: string;
          preamp_db: number;
          band_count: number;
          bands: {
            filter_type: string;
            freq_hz: number;
            gain_db: number;
            q: number;
          }[];
        };
      }>("/api/eq/load-profile", {
        method: "POST",
        body: JSON.stringify({ profile_id: profileId }),
      }),
    autoEqSetMode: (mode: "off" | "manual" | "profile") =>
      req<{ ok: boolean; mode: string; enabled: boolean }>("/api/eq/mode", {
        method: "POST",
        body: JSON.stringify({ mode }),
      }),
    /** Phase 4 A/B bypass — momentary disable that preserves the
     *  active profile / bands. Toggling back is instant. */
    autoEqSetBypass: (bypass: boolean) =>
      req<{ ok: boolean; bypass: boolean }>("/api/eq/bypass", {
        method: "POST",
        body: JSON.stringify({ bypass }),
      }),
    /** Import a PEQ.txt file from the user (or generated on
     *  autoeq.app with a non-default target). Validates against the
     *  same parser bundled / catalog profiles go through; surfaces
     *  per-line parse errors so the user knows which line is wrong.
     *  Lands under `User imported/<headphone>/...` in the cache. */
    autoEqImportProfile: (
      headphone_name: string,
      content: string,
      overwrite = false,
    ) =>
      req<{ ok: boolean; profile_id: string; headphone: string }>(
        "/api/eq/import-profile",
        {
          method: "POST",
          body: JSON.stringify({ headphone_name, content, overwrite }),
        },
      ),
    /** Delete a user-imported profile. Refuses to delete bundled
     *  profiles (those ship with the app and come back on reinstall).
     *  If the deleted profile was active, the server clears the
     *  active selection too. */
    autoEqDeleteProfile: (profile_id: string) =>
      req<{ ok: boolean; profile_id: string; cleared_active: boolean }>(
        "/api/eq/delete-profile",
        {
          method: "POST",
          body: JSON.stringify({ profile_id }),
        },
      ),
    /** Phase 6 frequency-response data for the FR graph. Returns
     *  three parallel arrays — raw measured curve, target curve,
     *  predicted post-EQ — at log-spaced frequencies. Raw + target
     *  may be null when the headphone's measurement CSV isn't
     *  bundled. Computed against the player's current sample rate. */
    autoEqResponse: (points = 512) =>
      req<{
        frequencies_hz: number[];
        raw_db: number[] | null;
        target_db: number[] | null;
        post_eq_db: number[];
        sample_rate_hz: number;
        has_measurement: boolean;
      }>(`/api/eq/response?points=${points}`),
    /** Phase 5 user-tilt — bass / treble shelves + preamp offset
     *  stacked on top of the profile. Each field is optional;
     *  omitting one leaves it unchanged on the server. */
    autoEqSetTilt: (tilt: {
      preamp_offset_db?: number;
      bass_db?: number;
      treble_db?: number;
    }) =>
      req<{
        ok: boolean;
        tilt: {
          preamp_offset_db: number;
          bass_db: number;
          treble_db: number;
        };
      }>("/api/eq/tilt", {
        method: "POST",
        body: JSON.stringify(tilt),
      }),
    /** AutoEQ per-device profile mapping (Phase 3). Returns the
     *  list of seen output devices, each tagged with its mapped
     *  profile id (or null = "EQ off for this device", or
     *  unmapped = "use the fallback rule"). */
    autoEqDevices: () =>
      req<{
        devices: {
          fingerprint: string;
          display_name: string;
          kind: string;
          first_seen: number;
          last_seen: number;
          mapped_profile_id: string | null;
          unmapped?: boolean;
        }[];
        current_fingerprint: string;
        fallback_when_unmapped: "bypass" | "use_last_profile";
      }>("/api/eq/devices"),
    autoEqSetDeviceMapping: (fingerprint: string, profileId: string | null) =>
      req<{
        ok: boolean;
        fingerprint: string;
        profile_id: string | null;
      }>("/api/eq/device-mappings", {
        method: "POST",
        body: JSON.stringify({ fingerprint, profile_id: profileId }),
      }),
    autoEqForgetDevice: (fingerprint: string) =>
      req<{ ok: boolean; removed: boolean }>("/api/eq/forget-device", {
        method: "POST",
        body: JSON.stringify({ fingerprint }),
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
    /** Snapshot of the audio DSP chain — feeds the "Signal path"
     *  panel users open from the now-playing pill. Re-fetched on
     *  every panel-open so the readout reflects the current track
     *  + the latest user toggles. */
    signalPath: () => req<SignalPath>("/api/player/signal-path"),
  },
  /** Backend backstop for "what was playing when the user quit."
   *  Pairs with the frontend's localStorage persistence — see
   *  usePlayer.ts. The server keeps the most-recently-pushed
   *  snapshot in user_data_dir/now_playing.json so a quit that
   *  loses localStorage (WKWebView quirks on macOS) still restores. */
  nowPlayingState: {
    get: () =>
      req<{ state: Record<string, unknown> | null }>(
        "/api/now-playing/state",
      ).catch(() => ({ state: null })),
    put: (state: Record<string, unknown> | null) =>
      req<{ ok: boolean }>("/api/now-playing/state", {
        method: "PUT",
        body: JSON.stringify(state ?? {}),
      }).catch(() => ({ ok: false })),
  },
  cast: {
    devices: () =>
      req<{
        status: {
          available: boolean;
          running: boolean;
          device_count: number;
          last_event_age_s: number | null;
          connected_id?: string | null;
          connected_name?: string | null;
          bytes_encoded?: number;
          media_loaded?: boolean;
        };
        devices: {
          id: string;
          friendly_name: string;
          model_name: string;
          manufacturer: string;
          cast_type: string;
        }[];
      }>("/api/cast/devices"),
    connect: (deviceId: string) =>
      req<{
        ok: boolean;
        device: {
          id: string;
          friendly_name: string;
          model_name: string;
          cast_type: string;
        };
      }>("/api/cast/connect", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      }),
    disconnect: () =>
      req<{ ok: boolean }>("/api/cast/disconnect", { method: "POST" }),
  },
  tidalConnect: {
    devices: () =>
      req<{
        status: {
          available: boolean;
          device_count: number;
          last_scan_age_s?: number | null;
          connected_id?: string | null;
          connected_name?: string | null;
          control_plane_ready?: boolean;
        };
        devices: {
          id: string;
          friendly_name: string;
          manufacturer: string;
          model: string;
          is_openhome: boolean;
          has_credentials_service: boolean;
        }[];
      }>("/api/tidal-connect/devices"),
    connect: (deviceId: string) =>
      req<{
        ok: boolean;
        device: {
          id: string;
          friendly_name: string;
          manufacturer: string;
          model: string;
        };
      }>("/api/tidal-connect/connect", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      }),
    disconnect: () =>
      req<{ ok: boolean }>("/api/tidal-connect/disconnect", {
        method: "POST",
      }),
  },
  // Real Tidal Connect. Talks the actual WSS protocol via
  // app/audio/tidal_connect_real.py. Parallel to tidalConnect (which
  // is the OpenHome-mimic path). Discovery is continuous (mDNS), so
  // devices() just returns the cached snapshot without an active
  // refresh.
  tidalConnectReal: {
    devices: () =>
      req<{
        devices: {
          id: string;
          name: string;
          address: string;
          port: number;
        }[];
        active_device_id: string | null;
      }>("/api/tidal-connect-real/devices"),
    connect: (deviceId: string) =>
      req<{
        ok: boolean;
        device: { id: string; name: string; address: string; port: number };
      }>("/api/tidal-connect-real/connect", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      }),
    disconnect: () =>
      req<{ ok: boolean }>("/api/tidal-connect-real/disconnect", {
        method: "POST",
      }),
  },
  dlna: {
    // Returns the cached device list without triggering a fresh
    // SSDP scan. Cheap and safe to poll. Use refresh() when the
    // picker dropdown opens so a stale cache doesn't hide a
    // device that just powered on.
    devices: () =>
      req<{
        status: {
          available: boolean;
          device_count: number;
          last_scan_age_s?: number | null;
          connected_id?: string | null;
          connected_name?: string | null;
          bytes_encoded?: number;
          media_loaded?: boolean;
          stream_url?: string;
        };
        devices: {
          id: string;
          name: string;
          manufacturer: string;
          model: string;
          has_avtransport: boolean;
        }[];
      }>("/api/dlna/devices"),
    refresh: (timeoutS: number = 5) =>
      req<{
        status: {
          available: boolean;
          device_count: number;
          last_scan_age_s?: number | null;
          connected_id?: string | null;
          connected_name?: string | null;
        };
        devices: {
          id: string;
          name: string;
          manufacturer: string;
          model: string;
          has_avtransport: boolean;
        }[];
      }>("/api/dlna/refresh", {
        method: "POST",
        body: JSON.stringify({ timeout_s: timeoutS }),
      }),
    connect: (deviceId: string) =>
      req<{
        ok: boolean;
        device: {
          id: string;
          name: string;
          manufacturer: string;
          model: string;
        };
      }>("/api/dlna/connect", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      }),
    disconnect: () =>
      req<{ ok: boolean }>("/api/dlna/disconnect", { method: "POST" }),
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
    clearCompleted: () =>
      req<{ ok: true }>("/api/downloads/completed", { method: "DELETE" }),
    cancel: (id: string) =>
      req<{ ok: true }>(`/api/downloads/${id}`, { method: "DELETE" }),
    cancelAll: () =>
      req<{ cancelled: number }>("/api/downloads/active", { method: "DELETE" }),
    reveal: (path: string) =>
      req<{ ok: true }>("/api/reveal", {
        method: "POST",
        body: JSON.stringify({ path }),
      }),
    stats: () =>
      req<{ output_dir: string; total_bytes: number; file_count: number }>(
        "/api/downloads/stats",
      ),
    state: () => req<{ paused: boolean }>("/api/downloads/state"),
    pause: () =>
      req<{ paused: true }>("/api/downloads/pause", { method: "POST" }),
    resume: () =>
      req<{ paused: false }>("/api/downloads/resume", { method: "POST" }),
  },
  settings: {
    get: () => req<Settings>("/api/settings"),
    put: (patch: Partial<Settings>) =>
      req<Settings>("/api/settings", {
        method: "PUT",
        body: JSON.stringify(patch),
      }),
  },
  favorites: {
    snapshot: () => req<FavoritesSnapshot>("/api/favorites"),
    add: (kind: FavoriteKind, id: string) =>
      req<{ ok: true }>(`/api/favorites/${kind}/${encodeURIComponent(id)}`, {
        method: "POST",
      }),
    remove: (kind: FavoriteKind, id: string) =>
      req<{ ok: true }>(`/api/favorites/${kind}/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
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
      req<{ ok: true }>(`/api/playlists/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
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
      req<{ ok: true }>(
        `/api/playlists/${encodeURIComponent(id)}/tracks/${index}`,
        {
          method: "DELETE",
        },
      ),
    moveTrack: (id: string, mediaId: string, position: number) =>
      req<{ ok: true }>(
        `/api/playlists/${encodeURIComponent(id)}/tracks/move`,
        {
          method: "POST",
          body: JSON.stringify({ media_id: mediaId, position }),
        },
      ),
  },
  /** Current Tidal request-gate cooldown. Populated after an HTTP
   *  429 or an abuse-detected 403; the UI uses it to render a
   *  banner so users know why nothing's loading. */
  tidalBackoff: () =>
    req<{
      active: boolean;
      seconds_remaining: number;
      reason: string;
      until_epoch: number;
    }>("/api/tidal/backoff"),
};
