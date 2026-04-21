import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { Sidebar } from "@/components/Sidebar";
import { NavBar } from "@/components/NavBar";
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
import { useNetworkStatus } from "@/hooks/useNetworkStatus";
import { PlayerProvider } from "@/hooks/PlayerContext";
import { VideoPlayerProvider } from "@/hooks/useVideoPlayer";
import { VideoPlayerModal } from "@/components/VideoPlayerModal";
import { SelectionBar } from "@/components/SelectionBar";
import { useAuth } from "@/hooks/useAuth";
import { useDownloads } from "@/hooks/useDownloads";
import { useDownloadNotifications } from "@/hooks/useDownloadNotifications";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";
import { setLastfmEnabled } from "@/hooks/useLastfmPlaycount";
import { useLastfmScrobbler } from "@/hooks/useLastfmScrobbler";
import { useMediaSession } from "@/hooks/useMediaSession";
import { useTidalPlayReporter } from "@/hooks/useTidalPlayReporter";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { Login } from "@/pages/Login";
import { Home } from "@/pages/Home";
import { Search } from "@/pages/Search";
import { Library } from "@/pages/Library";
import { LocalLibrary } from "@/pages/LocalLibrary";
import { Explore } from "@/pages/Explore";
import { FolderDetail } from "@/pages/FolderDetail";
import { FollowListPage } from "@/pages/FollowListPage";
import { GenresPage } from "@/pages/GenresPage";
import { MixesPage } from "@/pages/MixesPage";
import { MoodsPage } from "@/pages/MoodsPage";
import { ProfilePage } from "@/pages/ProfilePage";
import { BrowsePage } from "@/pages/BrowsePage";
import { ChartsPage } from "@/pages/ChartsPage";
import { FeedPage } from "@/pages/FeedPage";
import { HistoryPage } from "@/pages/HistoryPage";
import { PopularPage } from "@/pages/PopularPage";
import { StatsPage } from "@/pages/StatsPage";
import { AlbumDetail } from "@/pages/AlbumDetail";
import { ArtistDetail } from "@/pages/ArtistDetail";
import { MixDetail } from "@/pages/MixDetail";
import { RadioPage } from "@/pages/RadioPage";
import { ImportPage } from "@/pages/ImportPage";
import { PlaylistDetail } from "@/pages/PlaylistDetail";
import { Downloads } from "@/pages/Downloads";
import { SettingsPage } from "@/pages/SettingsPage";

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
          <OfflineProvider>
            <DownloadStreamProvider>
              <DownloadedProvider>
                <FavoritesProvider>
                  <MyPlaylistsProvider>
                    <RecentsProvider>
                      <AppInner />
                    </RecentsProvider>
                  </MyPlaylistsProvider>
                </FavoritesProvider>
              </DownloadedProvider>
            </DownloadStreamProvider>
          </OfflineProvider>
        </UiPreferencesProvider>
      </ErrorBoundary>
    </ToastProvider>
  );
}

function AppInner() {
  const auth = useAuth();
  const { offline } = useOfflineMode();
  const online = useNetworkStatus();

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

  // A dead network is treated like the offline preference — even if
  // the user never flipped the toggle, it would be strange to render
  // Login with no way to actually reach Tidal. The Login page still
  // shows when the user is genuinely signed out on an online network,
  // which is the case a first-time user lands in.
  if (!auth.logged_in && !offline && online) {
    return <Login onLoggedIn={auth.refresh} />;
  }

  // The toggle is the master switch — when it's on, we treat the
  // session as offline even if a Tidal token is still valid. That's
  // what users expect ("Work offline" should immediately hide Search,
  // Explore, etc.) and it also lets someone browse their own files
  // without the app chattering at Tidal in the background.
  //
  // OR with `!online` so a browser-level drop temporarily flips the
  // shell into offline mode without touching the persisted preference
  // — when connectivity returns, the OR clears and we're back online.
  const isOffline = offline || !online;

  return (
    <BrowserRouter>
      {/* PlayerProvider and TrackSelectionProvider both mount BELOW
          BrowserRouter — PlayerProvider doesn't strictly need it, but
          TrackSelectionProvider uses useLocation() to clear selection on
          route change. Keeping them colocated here for clarity. */}
      <PlayerProvider>
        <VideoPlayerProvider>
          <TrackSelectionProvider>
            <Shell
              username={auth.username}
              avatar={auth.avatar}
              userId={auth.user_id}
              onLogout={auth.logout}
              offline={isOffline}
              onSignInRequested={auth.refresh}
            />
            <VideoPlayerModal />
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
  const toast = useToast();
  const location = useLocation();
  // Opt-in desktop notifications when a burst finishes. We pull the
  // pref lazily from settings — Settings is the source of truth and
  // the GET is cheap enough to refetch on mount without plumbing the
  // whole settings object into context just for one boolean.
  const [notifyEnabled, setNotifyEnabled] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api.settings
      .get()
      .then((s) => {
        if (!cancelled) setNotifyEnabled(!!s.notify_on_complete);
      })
      .catch(() => {
        /* ignore — default stays false */
      });
    // Settings page dispatches this event after every successful save,
    // so toggling the pref there updates the shell in real time.
    const onUpdate = (e: Event) => {
      const detail = (e as CustomEvent<{ notify_on_complete?: boolean }>).detail;
      if (detail && typeof detail.notify_on_complete === "boolean") {
        setNotifyEnabled(detail.notify_on_complete);
      }
    };
    window.addEventListener("tidal-settings-updated", onUpdate);
    return () => {
      cancelled = true;
      window.removeEventListener("tidal-settings-updated", onUpdate);
    };
  }, []);
  useDownloadNotifications(notifyEnabled, downloads.active, downloads.completed);
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
      const raw = localStorage.getItem("tidal-downloader:seen-downloads");
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
        "tidal-downloader:seen-downloads",
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
          activeDownloads={downloads.active.length}
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
          <UpdateBanner />
          <div ref={fadeRef} className="animate-route px-8 py-6">
          <ErrorBoundary resetKey={location.pathname}>
          <Routes>
            {offline ? (
              <>
                {/* Offline mode: only show pages that don't need a live
                    Tidal session. Everything else redirects to the local
                    library so a stale bookmark / typed URL doesn't land
                    the user on a page that'll immediately 401. */}
                <Route path="/" element={<Navigate to="/library/local" replace />} />
                <Route path="/library" element={<Navigate to="/library/local" replace />} />
                <Route path="/library/local" element={<LocalLibrary onDownload={enqueue} />} />
                <Route path="/downloads" element={<Downloads items={downloads.items} offline={offline} />} />
                <Route path="/settings" element={<SettingsPage onLogout={onLogout} />} />
                <Route path="*" element={<Navigate to="/library/local" replace />} />
              </>
            ) : (
              <>
                <Route path="/" element={<Home onDownload={enqueue} />} />
                <Route path="/search" element={<Search onDownload={enqueue} />} />
                <Route path="/explore" element={<Explore onDownload={enqueue} />} />
                <Route path="/genres" element={<GenresPage onDownload={enqueue} />} />
                <Route path="/moods" element={<MoodsPage onDownload={enqueue} />} />
                <Route path="/mixes" element={<MixesPage />} />
                <Route path="/charts" element={<Navigate to="/charts/new" replace />} />
                <Route path="/charts/:chart" element={<ChartsPage onDownload={enqueue} />} />
                <Route path="/browse/:path" element={<BrowsePage onDownload={enqueue} />} />
                <Route path="/library" element={<Navigate to="/library/albums" replace />} />
                <Route path="/library/local" element={<LocalLibrary onDownload={enqueue} />} />
                <Route path="/library/folder/:id" element={<FolderDetail onDownload={enqueue} />} />
                <Route path="/library/:section" element={<Library onDownload={enqueue} />} />
                <Route path="/album/:id" element={<AlbumDetail onDownload={enqueue} />} />
                <Route path="/artist/:id" element={<ArtistDetail onDownload={enqueue} />} />
                <Route path="/playlist/:id" element={<PlaylistDetail onDownload={enqueue} />} />
                <Route path="/mix/:id" element={<MixDetail onDownload={enqueue} />} />
                <Route
                  path="/radio/artist/:id"
                  element={<RadioPage kind="artist" onDownload={enqueue} />}
                />
                <Route
                  path="/radio/track/:id"
                  element={<RadioPage kind="track" onDownload={enqueue} />}
                />
                <Route path="/feed" element={<FeedPage onDownload={enqueue} />} />
                <Route path="/history" element={<HistoryPage onDownload={enqueue} />} />
                <Route path="/stats" element={<StatsPage />} />
                <Route path="/import" element={<ImportPage />} />
                <Route path="/user/:id" element={<ProfilePage onDownload={enqueue} />} />
                <Route path="/user/:id/followers" element={<FollowListPage kind="followers" />} />
                <Route path="/user/:id/following" element={<FollowListPage kind="following" />} />
                <Route path="/popular" element={<PopularPage onDownload={enqueue} />} />
                <Route path="/downloads" element={<Downloads items={downloads.items} offline={offline} />} />
                <Route path="/settings" element={<SettingsPage onLogout={onLogout} />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </>
            )}
          </Routes>
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
