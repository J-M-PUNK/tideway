import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { Sidebar } from "@/components/Sidebar";
import { DownloadDock } from "@/components/DownloadDock";
import { NavBar } from "@/components/NavBar";
import { NowPlaying } from "@/components/NowPlaying";
import { QueuePanel } from "@/components/QueuePanel";
import { LyricsPanel } from "@/components/LyricsPanel";
import { FullScreenPlayer } from "@/components/FullScreenPlayer";
import { CommandPalette } from "@/components/CommandPalette";
import { CreatePlaylistDialog } from "@/components/CreatePlaylistDialog";
import { ToastProvider, useToast } from "@/components/toast";
import { DownloadedProvider } from "@/hooks/useDownloadedSet";
import { DownloadStreamProvider } from "@/hooks/useDownloadStream";
import { FavoritesProvider } from "@/hooks/useFavorites";
import { MyPlaylistsProvider } from "@/hooks/useMyPlaylists";
import { RecentsProvider } from "@/hooks/useRecentlyPlayed";
import { PlayerProvider } from "@/hooks/PlayerContext";
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
import { AlbumDetail } from "@/pages/AlbumDetail";
import { ArtistDetail } from "@/pages/ArtistDetail";
import { MixDetail } from "@/pages/MixDetail";
import { PlaylistDetail } from "@/pages/PlaylistDetail";
import { Downloads } from "@/pages/Downloads";
import { SettingsPage } from "@/pages/SettingsPage";

export default function App() {
  return (
    <ToastProvider>
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
      {/* PlayerProvider is mounted BELOW BrowserRouter so nav inside the
          player UI works, and ABOVE Shell so everything that uses the
          player can pull from context instead of prop drilling. */}
      <PlayerProvider>
        <Shell username={auth.username} onLogout={auth.logout} />
      </PlayerProvider>
    </BrowserRouter>
  );
}

function Shell({ username, onLogout }: { username: string | null; onLogout: () => void }) {
  const downloads = useDownloads();
  const toast = useToast();
  const [queueOpen, setQueueOpen] = useState(false);
  const [lyricsOpen, setLyricsOpen] = useState(false);
  const [fullOpen, setFullOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [createPlaylistOpen, setCreatePlaylistOpen] = useState(false);

  useKeyboardShortcuts({ onOpenPalette: () => setPaletteOpen(true) });
  useMediaSession();

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
        <Sidebar username={username} activeDownloads={downloads.active.length} />
        <main className="min-w-0 flex-1 overflow-y-auto rounded-lg bg-gradient-to-b from-[#1a1a1a] to-background px-8 py-6 scrollbar-thin">
          <NavBar />
          <Routes>
            <Route path="/" element={<Home onDownload={enqueue} />} />
            <Route path="/search" element={<Search onDownload={enqueue} />} />
            <Route path="/explore" element={<Explore onDownload={enqueue} />} />
            <Route path="/browse/:path" element={<BrowsePage onDownload={enqueue} />} />
            <Route path="/library" element={<Navigate to="/library/albums" replace />} />
            <Route path="/library/:section" element={<Library onDownload={enqueue} />} />
            <Route path="/album/:id" element={<AlbumDetail onDownload={enqueue} />} />
            <Route path="/artist/:id" element={<ArtistDetail onDownload={enqueue} />} />
            <Route path="/playlist/:id" element={<PlaylistDetail onDownload={enqueue} />} />
            <Route path="/mix/:id" element={<MixDetail onDownload={enqueue} />} />
            <Route path="/downloads" element={<Downloads items={downloads.items} />} />
            <Route path="/settings" element={<SettingsPage onLogout={onLogout} />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
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
      <DownloadDock items={downloads.items} activeCount={downloads.active.length} />
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
    </div>
  );
}
