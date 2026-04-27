import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { Loader2 } from "lucide-react";
import { Sidebar } from "@/components/Sidebar";
import { NavBar } from "@/components/NavBar";
import { OfflineBanner } from "@/components/OfflineBanner";
import { TidalBackoffBanner } from "@/components/TidalBackoffBanner";
import { UpdateBanner } from "@/components/UpdateBanner";
import { NowPlaying } from "@/components/NowPlaying";
import { QueuePanel } from "@/components/QueuePanel";
import { LyricsPanel } from "@/components/LyricsPanel";
import { FullScreenPlayer } from "@/components/FullScreenPlayer";
import { CommandPalette } from "@/components/CommandPalette";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { UrlDropTarget } from "@/components/UrlDropTarget";
import { ToastProvider, useToast } from "@/components/toast";
import { DownloadedProvider } from "@/hooks/useDownloadedSet";
import { DownloadStreamProvider } from "@/hooks/useDownloadStream";
import { FavoritesProvider } from "@/hooks/useFavorites";
import { MyPlaylistsProvider } from "@/hooks/useMyPlaylists";
import { RecentsProvider } from "@/hooks/useRecentlyPlayed";
import { TrackSelectionProvider } from "@/hooks/useTrackSelection";
import { UiPreferencesProvider } from "@/hooks/useUiPreferences";
import { OfflineProvider, useOfflineMode } from "@/hooks/useOfflineMode";
import { PlayerProvider, usePlayerMeta } from "@/hooks/PlayerContext";
import {
  VideoDownloadsProvider,
  useVideoDownloads,
} from "@/hooks/useVideoDownloads";
import { TooltipProvider } from "@/components/ui/tooltip";
import { VideoPlayerProvider } from "@/hooks/useVideoPlayer";
import { VideoPlayerModal } from "@/components/VideoPlayerModal";
import { SelectionBar } from "@/components/SelectionBar";
import { useAuth } from "@/hooks/useAuth";
import { useDownloads } from "@/hooks/useDownloads";
import { useDownloadNotifications } from "@/hooks/useDownloadNotifications";
import { useTrackChangeNotifications } from "@/hooks/useTrackChangeNotifications";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";
import { setLastfmEnabled } from "@/hooks/useLastfmPlaycount";
import { useLastfmScrobbler } from "@/hooks/useLastfmScrobbler";
import { useMediaSession } from "@/hooks/useMediaSession";
import { useTidalPlayReporter } from "@/hooks/useTidalPlayReporter";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
// Route components load on demand. Each entry becomes its own chunk
// in the Vite build — the initial bundle ships only the scaffolding
// (App, Shell, PlayerProvider, etc.) and the page for the URL the
// user lands on. Everything else streams in when navigated to. This
// is the single biggest initial-load win for a pywebview app where
// the WebView2 / WKWebView cold-start + the giant single-chunk parse
// used to dominate first paint.
//
// React.lazy expects a module with a `default` export; pages here
// are named exports, so we wrap each import to rename the chosen
// export into `default` for the lazy loader.
import { lazy, Suspense } from "react";
import { HeroSkeleton } from "@/components/Skeletons";

// Inline `lazy(() => import(...).then(m => ({ default: m.Name })))`
// calls preserve each component's prop types through TypeScript's
// inference. A generic wrapper around lazy would strip them to
// ComponentType<any>, which breaks <Home onDownload={...} />.
const Login = lazy(() =>
  import("@/pages/Login").then((m) => ({ default: m.Login })),
);
const Home = lazy(() =>
  import("@/pages/Home").then((m) => ({ default: m.Home })),
);
const Search = lazy(() =>
  import("@/pages/Search").then((m) => ({ default: m.Search })),
);
const Library = lazy(() =>
  import("@/pages/Library").then((m) => ({ default: m.Library })),
);
const LocalLibrary = lazy(() =>
  import("@/pages/LocalLibrary").then((m) => ({ default: m.LocalLibrary })),
);
const Explore = lazy(() =>
  import("@/pages/Explore").then((m) => ({ default: m.Explore })),
);
const FolderDetail = lazy(() =>
  import("@/pages/FolderDetail").then((m) => ({ default: m.FolderDetail })),
);
const FollowListPage = lazy(() =>
  import("@/pages/FollowListPage").then((m) => ({ default: m.FollowListPage })),
);
const GenresPage = lazy(() =>
  import("@/pages/GenresPage").then((m) => ({ default: m.GenresPage })),
);
const MixesPage = lazy(() =>
  import("@/pages/MixesPage").then((m) => ({ default: m.MixesPage })),
);
const MoodsPage = lazy(() =>
  import("@/pages/MoodsPage").then((m) => ({ default: m.MoodsPage })),
);
const ProfilePage = lazy(() =>
  import("@/pages/ProfilePage").then((m) => ({ default: m.ProfilePage })),
);
const BrowsePage = lazy(() =>
  import("@/pages/BrowsePage").then((m) => ({ default: m.BrowsePage })),
);
const ChartsPage = lazy(() =>
  import("@/pages/ChartsPage").then((m) => ({ default: m.ChartsPage })),
);
const FeedPage = lazy(() =>
  import("@/pages/FeedPage").then((m) => ({ default: m.FeedPage })),
);
const HistoryPage = lazy(() =>
  import("@/pages/HistoryPage").then((m) => ({ default: m.HistoryPage })),
);
const PopularPage = lazy(() =>
  import("@/pages/PopularPage").then((m) => ({ default: m.PopularPage })),
);
const StatsPage = lazy(() =>
  import("@/pages/StatsPage").then((m) => ({ default: m.StatsPage })),
);
const StatsDetail = lazy(() =>
  import("@/pages/StatsDetail").then((m) => ({ default: m.StatsDetail })),
);
const AlbumDetail = lazy(() =>
  import("@/pages/AlbumDetail").then((m) => ({ default: m.AlbumDetail })),
);
const ArtistDetail = lazy(() =>
  import("@/pages/ArtistDetail").then((m) => ({ default: m.ArtistDetail })),
);
const ArtistSection = lazy(() =>
  import("@/pages/ArtistSection").then((m) => ({ default: m.ArtistSection })),
);
const MixDetail = lazy(() =>
  import("@/pages/MixDetail").then((m) => ({ default: m.MixDetail })),
);
const RadioPage = lazy(() =>
  import("@/pages/RadioPage").then((m) => ({ default: m.RadioPage })),
);
const ImportPage = lazy(() =>
  import("@/pages/ImportPage").then((m) => ({ default: m.ImportPage })),
);
const PlaylistDetail = lazy(() =>
  import("@/pages/PlaylistDetail").then((m) => ({ default: m.PlaylistDetail })),
);
const Downloads = lazy(() =>
  import("@/pages/Downloads").then((m) => ({ default: m.Downloads })),
);
const MiniPlayerPage = lazy(() =>
  import("@/pages/MiniPlayerPage").then((m) => ({ default: m.MiniPlayerPage })),
);
const SettingsPage = lazy(() =>
  import("@/pages/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);

export default function App() {
  // Outer boundary renders a full-screen fallback for errors that
  // escape the per-route boundary (e.g. a provider blows up during
  // its initial render). ToastProvider sits above it so the fallback
  // doesn't try to emit a toast into nothing if some rarely-hit
  // cleanup path throws.
  return (
    <ToastProvider>
      <ErrorBoundary fullScreen>
        <UiPreferencesProvider>
          <TooltipProvider delayDuration={250}>
            <OfflineProvider>
              <DownloadStreamProvider>
                <DownloadedProvider>
                  <FavoritesProvider>
                    <MyPlaylistsProvider>
                      <RecentsProvider>
                        <VideoDownloadsProvider>
                          <AppInner />
                        </VideoDownloadsProvider>
                      </RecentsProvider>
                    </MyPlaylistsProvider>
                  </FavoritesProvider>
                </DownloadedProvider>
              </DownloadStreamProvider>
            </OfflineProvider>
          </TooltipProvider>
        </UiPreferencesProvider>
      </ErrorBoundary>
    </ToastProvider>
  );
}

function AppInner() {
  const auth = useAuth();
  const { offline } = useOfflineMode();

  // Hold the spinner until both probes resolve — rendering Login
  // prematurely would flash the sign-in screen for users who'd land
  // straight in offline mode.
  if (auth.loading || offline === null) {
    return (
      <div className="flex h-screen items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }

  // `offline` is already the OR of the user toggle and the auto
  // browser-level detection (see useOfflineMode). If it's false we
  // have a live network and no user preference for offline, which is
  // when a signed-out user should land on Login.
  if (!auth.logged_in && !offline) {
    return (
      <Suspense fallback={<HeroSkeleton />}>
        <Login onLoggedIn={auth.refresh} />
      </Suspense>
    );
  }

  // The toggle is the master switch — when it's on, we treat the
  // session as offline even if a Tidal token is still valid. That's
  // what users expect ("Work offline" should immediately hide Search,
  // Explore, etc.) and it also lets someone browse their own files
  // without the app chattering at Tidal in the background.
  const isOffline = offline;

  // The mini-player window loads /mini — a compact transport UI that
  // shares player state (via backend SSE) with the main window but
  // doesn't render the Shell. Detect via window.location so the route
  // is decided before Shell's hooks (and its large component tree)
  // pay render cost.
  const isMiniRoute =
    typeof window !== "undefined" &&
    window.location.pathname.startsWith("/mini");

  return (
    <BrowserRouter>
      {/* PlayerProvider and TrackSelectionProvider both mount BELOW
          BrowserRouter — PlayerProvider doesn't strictly need it, but
          TrackSelectionProvider uses useLocation() to clear selection on
          route change. Keeping them colocated here for clarity. */}
      <PlayerProvider>
        <VideoPlayerProvider>
          <TrackSelectionProvider>
            {isMiniRoute ? (
              <Suspense fallback={null}>
                <MiniPlayerPage />
              </Suspense>
            ) : (
              <>
                <Shell
                  username={auth.username}
                  avatar={auth.avatar}
                  userId={auth.user_id}
                  onLogout={auth.logout}
                  offline={isOffline}
                  onSignInRequested={auth.refresh}
                />
                <VideoPlayerModal />
              </>
            )}
          </TrackSelectionProvider>
        </VideoPlayerProvider>
      </PlayerProvider>
    </BrowserRouter>
  );
}

function Shell({
  username,
  avatar,
  userId,
  onLogout,
  offline,
  onSignInRequested,
}: {
  username: string | null;
  avatar: string | null;
  userId: string | null;
  onLogout: () => void;
  /** True when the user is signed out but offline mode is on. Flips
   *  the sidebar to local-only entries and redirects online-only
   *  routes to /library/local. */
  offline: boolean;
  /** Called when the user wants to exit offline mode and sign in.
   *  Re-runs the auth probe so the App re-evaluates. */
  onSignInRequested: () => void;
}) {
  const downloads = useDownloads();
  const videoDownloads = useVideoDownloads();
  const toast = useToast();
  const location = useLocation();
  const playerMeta = usePlayerMeta();
  // Opt-in desktop notifications when a burst finishes. We pull the
  // pref lazily from settings — Settings is the source of truth and
  // the GET is cheap enough to refetch on mount without plumbing the
  // whole settings object into context just for one boolean.
  const [notifyEnabled, setNotifyEnabled] = useState(false);
  const [notifyTrackEnabled, setNotifyTrackEnabled] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api.settings
      .get()
      .then((s) => {
        if (cancelled) return;
        setNotifyEnabled(!!s.notify_on_complete);
        setNotifyTrackEnabled(!!s.notify_on_track_change);
      })
      .catch(() => {
        /* ignore — default stays false */
      });
    // Settings page dispatches this event after every successful save,
    // so toggling the pref there updates the shell in real time.
    const onUpdate = (e: Event) => {
      const detail = (
        e as CustomEvent<{
          notify_on_complete?: boolean;
          notify_on_track_change?: boolean;
        }>
      ).detail;
      if (!detail) return;
      if (typeof detail.notify_on_complete === "boolean") {
        setNotifyEnabled(detail.notify_on_complete);
      }
      if (typeof detail.notify_on_track_change === "boolean") {
        setNotifyTrackEnabled(detail.notify_on_track_change);
      }
    };
    window.addEventListener("tidal-settings-updated", onUpdate);
    return () => {
      cancelled = true;
      window.removeEventListener("tidal-settings-updated", onUpdate);
    };
  }, []);
  useDownloadNotifications(
    notifyEnabled,
    downloads.active,
    downloads.completed,
  );
  useTrackChangeNotifications(notifyTrackEnabled, playerMeta.track);
  const [queueOpen, setQueueOpen] = useState(false);
  const [lyricsOpen, setLyricsOpen] = useState(false);
  const [fullOpen, setFullOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [createPlaylistOpen, setCreatePlaylistOpen] = useState(false);

  useKeyboardShortcuts({ onOpenPalette: () => setPaletteOpen(true) });
  useMediaSession();
  useLastfmScrobbler();
  useTidalPlayReporter();

  // Gate the playcount hooks on whether api credentials exist (not on
  // whether the user has completed the auth flow). Global listener /
  // playcount numbers from artist.getInfo / album.getInfo come back
  // with just an api_key; per-user playcount is optional. Settings
  // page emits "tidal-settings-updated" on save so switching from
  // baked-in → user creds or finishing Connect propagates instantly.
  useEffect(() => {
    let cancelled = false;
    const sync = async () => {
      try {
        const s = await api.lastfm.status();
        if (!cancelled) setLastfmEnabled(s.has_credentials);
      } catch {
        if (!cancelled) setLastfmEnabled(false);
      }
    };
    sync();
    const onUpdate = () => sync();
    window.addEventListener("tidal-settings-updated", onUpdate);
    return () => {
      cancelled = true;
      window.removeEventListener("tidal-settings-updated", onUpdate);
    };
  }, []);

  // Track unseen completed downloads so the sidebar can badge "X new
  // finished" when the user hasn't looked at /downloads yet. When the
  // user lands on /downloads, mark everything currently finished as
  // seen. Persisted so a reload doesn't re-surface old downloads.
  const [seenCompletedIds, setSeenCompletedIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem("tideway:seen-downloads");
      return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
    } catch {
      return new Set();
    }
  });
  useEffect(() => {
    if (location.pathname !== "/downloads") return;
    const ids = new Set(downloads.completed.map((c) => c.id));
    setSeenCompletedIds(ids);
    try {
      localStorage.setItem(
        "tideway:seen-downloads",
        JSON.stringify(Array.from(ids)),
      );
    } catch {
      /* ignore */
    }
  }, [location.pathname, downloads.completed]);
  const newCompletedCount = useMemo(
    () => downloads.completed.filter((c) => !seenCompletedIds.has(c.id)).length,
    [downloads.completed, seenCompletedIds],
  );

  // Replay the route-fade animation on navigation without remounting the
  // routed view. Keying on location.pathname would throw away in-page
  // state (scroll position, fetch cache, expanded dropdowns) whenever a
  // route param changed (e.g. /album/1 → /album/2), which is a real
  // regression. Instead, remove + re-add the animation class on a stable
  // wrapper so the CSS keyframes restart cleanly.
  const fadeRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = fadeRef.current;
    if (!el) return;
    el.classList.remove("animate-route");
    void el.offsetWidth;
    el.classList.add("animate-route");
  }, [location.pathname]);

  // Esc closes any open side panel. Lyrics wins over queue so the user can
  // stack them without ambiguity.
  useEffect(() => {
    if (!queueOpen && !lyricsOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (lyricsOpen) setLyricsOpen(false);
      else if (queueOpen) setQueueOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [queueOpen, lyricsOpen]);

  const enqueue = useCallback<OnDownload>(
    async (kind, id, quality) => {
      try {
        await api.downloads.enqueue(kind, id, quality);
        toast.show({
          kind: "success",
          title: `Added to downloads`,
          description:
            kind === "track"
              ? "Track queued"
              : kind === "album"
                ? "Album queued"
                : "Playlist queued",
        });
      } catch (err) {
        toast.show({
          kind: "error",
          title: "Download failed",
          description: err instanceof Error ? err.message : String(err),
        });
      }
    },
    [toast],
  );

  return (
    <div className="flex h-screen flex-col bg-background">
      <div className="flex min-h-0 flex-1 gap-2 p-2 pb-0">
        <Sidebar
          // Active count = music + video jobs currently downloading.
          // Both render as rows on the Downloads page now, so the
          // badge on the sidebar button should reflect the union.
          activeDownloads={
            downloads.active.length + videoDownloads.active.length
          }
          newDownloads={newCompletedCount}
          offline={offline}
        />
        <main
          data-scroll-container
          className="min-w-0 flex-1 overflow-y-auto rounded-lg bg-gradient-to-b from-secondary to-background scrollbar-thin"
        >
          {/* The scroll container itself carries no padding — otherwise
              `sticky top-0` on NavBar would anchor at the padding edge
              and the NavBar would visually drop 24px when the user
              scrolls. Padding lives on the route wrapper below NavBar;
              DetailHero still uses `-mx-8 -mt-6` to bleed against it. */}
          <NavBar
            username={username}
            avatar={avatar}
            userId={userId}
            onLogout={onLogout}
            offline={offline}
            onSignInRequested={onSignInRequested}
          />
          <OfflineBanner />
          <TidalBackoffBanner />
          <UpdateBanner />
          <div ref={fadeRef} className="animate-route px-8 py-6">
            <ErrorBoundary resetKey={location.pathname}>
              <Suspense fallback={<HeroSkeleton />}>
                <Routes>
                  {offline ? (
                    <>
                      {/* Offline mode: only show pages that don't need a live
                    Tidal session. Everything else redirects to the local
                    library so a stale bookmark / typed URL doesn't land
                    the user on a page that'll immediately 401. */}
                      <Route
                        path="/"
                        element={<Navigate to="/library/local" replace />}
                      />
                      <Route
                        path="/library"
                        element={<Navigate to="/library/local" replace />}
                      />
                      <Route
                        path="/library/local"
                        element={<LocalLibrary onDownload={enqueue} />}
                      />
                      <Route
                        path="/downloads"
                        element={
                          <Downloads
                            items={downloads.items}
                            offline={offline}
                          />
                        }
                      />
                      <Route
                        path="/settings"
                        element={<SettingsPage onLogout={onLogout} />}
                      />
                      <Route
                        path="*"
                        element={<Navigate to="/library/local" replace />}
                      />
                    </>
                  ) : (
                    <>
                      <Route path="/" element={<Home onDownload={enqueue} />} />
                      <Route
                        path="/search"
                        element={<Search onDownload={enqueue} />}
                      />
                      <Route
                        path="/explore"
                        element={<Explore onDownload={enqueue} />}
                      />
                      <Route
                        path="/genres"
                        element={<GenresPage onDownload={enqueue} />}
                      />
                      <Route
                        path="/moods"
                        element={<MoodsPage onDownload={enqueue} />}
                      />
                      <Route path="/mixes" element={<MixesPage />} />
                      <Route
                        path="/charts"
                        element={<Navigate to="/charts/new" replace />}
                      />
                      <Route
                        path="/charts/:chart"
                        element={<ChartsPage onDownload={enqueue} />}
                      />
                      <Route
                        path="/browse/:path"
                        element={<BrowsePage onDownload={enqueue} />}
                      />
                      <Route
                        path="/library"
                        element={<Navigate to="/library/albums" replace />}
                      />
                      <Route
                        path="/library/local"
                        element={<LocalLibrary onDownload={enqueue} />}
                      />
                      <Route
                        path="/library/folder/:id"
                        element={<FolderDetail onDownload={enqueue} />}
                      />
                      <Route
                        path="/library/:section"
                        element={<Library onDownload={enqueue} />}
                      />
                      <Route
                        path="/album/:id"
                        element={<AlbumDetail onDownload={enqueue} />}
                      />
                      <Route
                        path="/artist/:id"
                        element={<ArtistDetail onDownload={enqueue} />}
                      />
                      <Route
                        path="/artist/:id/all/:section"
                        element={<ArtistSection onDownload={enqueue} />}
                      />
                      <Route
                        path="/playlist/:id"
                        element={<PlaylistDetail onDownload={enqueue} />}
                      />
                      <Route
                        path="/mix/:id"
                        element={<MixDetail onDownload={enqueue} />}
                      />
                      <Route
                        path="/radio/artist/:id"
                        element={
                          <RadioPage kind="artist" onDownload={enqueue} />
                        }
                      />
                      <Route
                        path="/radio/track/:id"
                        element={
                          <RadioPage kind="track" onDownload={enqueue} />
                        }
                      />
                      <Route
                        path="/feed"
                        element={<FeedPage onDownload={enqueue} />}
                      />
                      <Route
                        path="/history"
                        element={<HistoryPage onDownload={enqueue} />}
                      />
                      <Route path="/stats" element={<StatsPage />} />
                      <Route path="/stats/:kind" element={<StatsDetail />} />
                      <Route path="/import" element={<ImportPage />} />
                      <Route
                        path="/user/:id"
                        element={<ProfilePage onDownload={enqueue} />}
                      />
                      <Route
                        path="/user/:id/followers"
                        element={<FollowListPage kind="followers" />}
                      />
                      <Route
                        path="/user/:id/following"
                        element={<FollowListPage kind="following" />}
                      />
                      <Route
                        path="/popular"
                        element={<PopularPage onDownload={enqueue} />}
                      />
                      <Route
                        path="/downloads"
                        element={
                          <Downloads
                            items={downloads.items}
                            offline={offline}
                          />
                        }
                      />
                      <Route
                        path="/settings"
                        element={<SettingsPage onLogout={onLogout} />}
                      />
                      <Route path="*" element={<Navigate to="/" replace />} />
                    </>
                  )}
                </Routes>
              </Suspense>
            </ErrorBoundary>
          </div>
        </main>
      </div>
      <SelectionBar />
      <NowPlaying
        onDownload={enqueue}
        onExpand={() => setFullOpen(true)}
        onToggleQueue={() => {
          setLyricsOpen(false);
          setQueueOpen((v) => !v);
        }}
        onToggleLyrics={() => {
          setQueueOpen(false);
          setLyricsOpen((v) => !v);
        }}
      />
      <QueuePanel open={queueOpen} onClose={() => setQueueOpen(false)} />
      <LyricsPanel open={lyricsOpen} onClose={() => setLyricsOpen(false)} />
      <FullScreenPlayer
        open={fullOpen}
        onClose={() => setFullOpen(false)}
        onDownload={enqueue}
      />
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onOpenCreatePlaylist={() => setCreatePlaylistOpen(true)}
      />
      <CreatePlaylistDialog
        open={createPlaylistOpen}
        onOpenChange={setCreatePlaylistOpen}
      />
      <UrlDropTarget />
    </div>
  );
}
