import { History, Music, Trash2 } from "lucide-react";
import type { OnDownload } from "@/api/download";
import { TrackList } from "@/components/TrackList";
import { EmptyState } from "@/components/EmptyState";
import { Button } from "@/components/ui/button";
import { useRecentlyPlayed } from "@/hooks/useRecentlyPlayed";
import { useToast } from "@/components/toast";

/**
 * Listening history — purely local (we record plays into localStorage from
 * NowPlaying's useRecordPlays). Capped at 30 entries, newest-first.
 */
export function HistoryPage({ onDownload }: { onDownload: OnDownload }) {
  const { tracks, clear } = useRecentlyPlayed();
  const toast = useToast();

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
            <History className="h-7 w-7" /> Listening history
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Tracks you've played in this app, newest first. Stored locally on this device.
          </p>
        </div>
        {tracks.length > 0 && (
          <Button
            variant="secondary"
            onClick={() => {
              clear();
              toast.show({ kind: "info", title: "Listening history cleared" });
            }}
          >
            <Trash2 className="h-4 w-4" /> Clear history
          </Button>
        )}
      </div>

      {tracks.length === 0 ? (
        <EmptyState
          icon={Music}
          title="Nothing here yet"
          description="Play a track for at least 10 seconds and it'll show up here."
        />
      ) : (
        <TrackList tracks={tracks} onDownload={onDownload} numbered={false} />
      )}
    </div>
  );
}
