import { Link } from "react-router-dom";
import { Music } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import type { Track } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { useRecentlyPlayed } from "@/hooks/useRecentlyPlayed";
import { usePlayerActions } from "@/hooks/PlayerContext";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";
import { imageProxy } from "@/lib/utils";

export function Home({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading, error } = useApi(() => api.page("home"), []);
  const recents = useRecentlyPlayed();

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  if (loading) {
    return (
      <div>
        <h1 className="mb-6 text-4xl font-bold tracking-tight">{greeting}</h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load home"} />;

  return (
    <div>
      <h1 className="mb-8 text-4xl font-bold tracking-tight">{greeting}</h1>
      {recents.tracks.length > 0 && (
        <JumpBackIn tracks={recents.tracks.slice(0, 6)} />
      )}
      <PageView page={data} onDownload={onDownload} />
    </div>
  );
}

function JumpBackIn({ tracks }: { tracks: Track[] }) {
  const actions = usePlayerActions();
  return (
    <div className="mb-8">
      <h2 className="mb-4 text-xl font-bold tracking-tight">Jump back in</h2>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
        {tracks.map((t) => (
          <button
            key={t.id}
            onClick={() => actions.play(t, tracks)}
            className="group flex items-center gap-3 rounded-md bg-card/60 p-2 text-left transition-colors hover:bg-accent"
          >
            <div className="h-14 w-14 flex-shrink-0 overflow-hidden rounded bg-secondary">
              {t.album?.cover ? (
                <img
                  src={imageProxy(t.album.cover)}
                  alt=""
                  className="h-full w-full object-cover"
                  loading="lazy"
                />
              ) : (
                <Music className="m-auto h-5 w-5 text-muted-foreground" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-semibold">{t.name}</div>
              <div className="truncate text-xs text-muted-foreground">
                {t.artists.map((a, i) => (
                  <span key={a.id}>
                    {i > 0 && ", "}
                    <Link
                      to={`/artist/${a.id}`}
                      onClick={(e) => e.stopPropagation()}
                      className="hover:underline"
                    >
                      {a.name}
                    </Link>
                  </span>
                ))}
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
