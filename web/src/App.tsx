import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { Sidebar } from "@/components/Sidebar";
import { NavBar } from "@/components/NavBar";
import { NowPlaying } from "@/components/NowPlaying";
import { QueuePanel } from "@/components/QueuePanel";
import { LyricsPanel } from "@/components/LyricsPanel";
import { FullScreenPlayer } from "@/components/FullScreenPlayer";
import { CommandPalette } from "@/components/CommandPalette";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { UrlDropTarget } from "@/components/UrlDropTarget";
import { ToastProvider, useToast } from "@/components/toast";
import { DownloadedProvider } from "@/hooks/useDownloadedSet";
import { DownloadStreamProvider } from "@/hooks/useDownloadStream";
import { FavoritesProvider } from "@/hooks/useFavorites";
import { MyPlaylistsProvider } from "@/hooks/useMyPlaylists";
import { RecentsProvider } from "@/hooks/useRecentlyPlayed";
import { TrackSelectionProvider } from "@/hooks/useTrackSelection";
import { UiPreferencesProvider } from "@/hooks/useUiPreferences";
import { PlayerProvider } from "@/hooks/PlayerContext";
import { SelectionBar } from "@/components/SelectionBar";
import { useAuth } from "@/hooks/useAuth";
import { useDownloads } from "@/hooks/useDownloads";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";
import { useMediaSession } from "@/hooks/useMediaSession";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { Login } from "@/pages/Login";
import { Home } from "@/pages/Home";
import { Search } from "@/pages/Search";
import { Library } from "@/pages/Library";
import { Explore } from "@/pages/Explore";
import { BrowsePage } from "@/pages/BrowsePage";
import { ChartsPage } from "@/pages/ChartsPage";
import { FeedPage } from "@/pages/FeedPage";
import { HistoryPage } from "@/pages/HistoryPage";
import { AlbumDetail } from "@/pages/AlbumDetail";
import { ArtistDetail } from "@/pages/ArtistDetail";
import { MixDetail } from "@/pages/MixDetail";
import { PlaylistDetail } from "@/pages/PlaylistDetail";
import { Downloads } from "@/pages/Downloads";
import { SettingsPage } from "@/pages/SettingsPage";

export default function App() {
  return (
    <ToastProvider>
      <UiPreferencesProvider>
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
      </UiPreferencesProvider>
    </ToastProvider>
  );
}

function AppInner() {
  const auth = useAuth();

  if (auth.loading) {
    return (
      <div className="flex h-screen items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }

  if (!auth.logged_in) {
    return <Login onLoggedIn={auth.refresh} />;
  }

  return (
    <BrowserRouter>
      {/* PlayerProvider and TrackSelectionProvider both mount BELOW
          BrowserRouter — PlayerProvider doesn't strictly need it, but
          TrackSelectionProvider uses useLocation() to clear selection on
          route change. Keeping them colocated here for clarity. */}
      <PlayerProvider>
        <TrackSelectionProvider>
          <Shell
            username={auth.username}
            avatar={auth.avatar}
            onLogout={auth.logout}
          />
        </TrackSelectionProvider>
      </PlayerProvider>
    </BrowserRouter>
  );
}

function Shell({
  username,
  avatar,
  onLogout,
}: {
  username: string | null;
  avatar: string | null;
  onLogout: () => void;
}) {
  const downloads = useDownloads();
  const toast = useToast();
  const location = useLocation();
  const [queueOpen, setQueueOpen] = useState(false);
  const [lyricsOpen, setLyricsOpen] = useState(false);
  const [fullOpen, setFullOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [createPlaylistOpen, setCreatePlaylistOpen] = useState(false);

  useKeyboardShortcuts({ onOpenPalette: () => setPaletteOpen(true) });
  useMediaSession();

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
    <div className="flex h-screen flex-col bg-black">
      <div className="flex min-h-0 flex-1 gap-2 p-2 pb-0">
        <Sidebar
          activeDownloads={downloads.active.length}
          newDownloads={newCompletedCount}
        />
        <main
          data-scroll-container
          className="min-w-0 flex-1 overflow-y-auto rounded-lg bg-gradient-to-b from-[#1a1a1a] to-background scrollbar-thin"
        >
          {/* The scroll container itself carries no padding — otherwise
              `sticky top-0` on NavBar would anchor at the padding edge
              and the NavBar would visually drop 24px when the user
              scrolls. Padding lives on the route wrapper below NavBar;
              DetailHero still uses `-mx-8 -mt-6` to bleed against it. */}
          <NavBar username={username} avatar={avatar} onLogout={onLogout} />
          <div ref={fadeRef} className="animate-route px-8 py-6">
          <Routes>
            <Route path="/" element={<Home onDownload={enqueue} />} />
            <Route path="/search" element={<Search onDownload={enqueue} />} />
            <Route path="/explore" element={<Explore onDownload={enqueue} />} />
            <Route path="/charts" element={<Navigate to="/charts/new" replace />} />
            <Route path="/charts/:chart" element={<ChartsPage onDownload={enqueue} />} />
            <Route path="/browse/:path" element={<BrowsePage onDownload={enqueue} />} />
            <Route path="/library" element={<Navigate to="/library/albums" replace />} />
            <Route path="/library/:section" element={<Library onDownload={enqueue} />} />
            <Route path="/album/:id" element={<AlbumDetail onDownload={enqueue} />} />
            <Route path="/artist/:id" element={<ArtistDetail onDownload={enqueue} />} />
            <Route path="/playlist/:id" element={<PlaylistDetail onDownload={enqueue} />} />
            <Route path="/mix/:id" element={<MixDetail onDownload={enqueue} />} />
            <Route path="/feed" element={<FeedPage onDownload={enqueue} />} />
            <Route path="/history" element={<HistoryPage onDownload={enqueue} />} />
            <Route path="/downloads" element={<Downloads items={downloads.items} />} />
            <Route path="/settings" element={<SettingsPage onLogout={onLogout} />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
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
