import { ListMusic, Music, Play, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/EmptyState";
import { formatDuration, imageProxy } from "@/lib/utils";
import { useIsDownloaded } from "@/hooks/useDownloadedSet";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { cn } from "@/lib/utils";

export function QueuePanel({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { track, queue, queueIndex } = usePlayerMeta();
  const actions = usePlayerActions();
  const current = queueIndex;
  const upcoming = queue.slice(current + 1);
  const history = current > 0 ? queue.slice(0, current) : [];

  return (
    <>
      {/* Backdrop */}
      <div
        className={cn(
          "fixed inset-0 z-40 bg-black/40 backdrop-blur-sm transition-opacity",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={onClose}
      />
      <aside
        className={cn(
          "fixed right-0 top-0 z-50 flex h-full w-96 flex-col border-l border-border bg-card shadow-2xl transition-transform",
          open ? "translate-x-0" : "translate-x-full",
        )}
        aria-hidden={!open}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <ListMusic className="h-4 w-4" />
            <h2 className="text-sm font-semibold">Queue</h2>
          </div>
          <div className="flex items-center gap-1">
            {queue.length > 1 && (
              <Button
                size="sm"
                variant="ghost"
                className="h-7 text-xs"
                onClick={actions.clearQueue}
                title="Clear everything except what's playing"
              >
                <Trash2 className="h-3 w-3" /> Clear
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={onClose}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto scrollbar-thin">
          {track ? (
            <>
              <SectionLabel>Now playing</SectionLabel>
              <QueueRow track={track} index={current} isCurrent />
              {upcoming.length > 0 && (
                <>
                  <SectionLabel>Up next · {upcoming.length}</SectionLabel>
                  {upcoming.map((t, i) => (
                    <QueueRow
                      key={`${t.id}-${current + 1 + i}`}
                      track={t}
                      index={current + 1 + i}
                    />
                  ))}
                </>
              )}
              {history.length > 0 && (
                <>
                  <SectionLabel>History</SectionLabel>
                  {history.map((t, i) => (
                    <QueueRow key={`${t.id}-${i}`} track={t} index={i} dimmed />
                  ))}
                </>
              )}
            </>
          ) : (
            <div className="p-6">
              <EmptyState
                icon={Music}
                title="Queue is empty"
                description="Play a track from anywhere to start building a queue."
              />
            </div>
          )}
        </div>
      </aside>
    </>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 pb-2 pt-4 text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
      {children}
    </div>
  );
}

function QueueRow({
  track,
  index,
  isCurrent,
  dimmed,
}: {
  track: import("@/api/types").Track;
  index: number;
  isCurrent?: boolean;
  dimmed?: boolean;
}) {
  const actions = usePlayerActions();
  const cover = imageProxy(track.album?.cover);
  const downloaded = useIsDownloaded(track.id);
  return (
    <div
      className={cn(
        "group flex items-center gap-3 px-4 py-2 hover:bg-accent",
        isCurrent && "bg-accent/60",
        dimmed && "opacity-50",
      )}
    >
      <button
        onClick={() => actions.jumpTo(index)}
        className="relative h-10 w-10 flex-shrink-0 overflow-hidden rounded bg-secondary"
        title="Jump to this track"
      >
        {cover ? (
          <img
            src={cover}
            alt=""
            className="h-full w-full object-cover"
            loading="lazy"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-4 w-4" />
          </div>
        )}
        <div
          className={cn(
            "absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity",
            !isCurrent && "group-hover:opacity-100",
          )}
        >
          <Play className="h-4 w-4 text-foreground" fill="currentColor" />
        </div>
      </button>
      <div className="min-w-0 flex-1">
        <div
          className={cn(
            "flex items-center gap-1.5 truncate text-sm",
            isCurrent ? "font-semibold text-primary" : "text-foreground",
          )}
        >
          <span className="truncate">{track.name}</span>
          {downloaded && (
            <span className="flex-shrink-0 rounded-sm bg-primary/15 px-1 py-0.5 text-[8px] font-bold uppercase text-primary">
              Saved
            </span>
          )}
        </div>
        <div className="truncate text-xs text-muted-foreground">
          {track.artists.map((a) => a.name).join(", ")}
        </div>
      </div>
      <span className="text-[11px] text-muted-foreground tabular-nums">
        {formatDuration(track.duration)}
      </span>
      {!isCurrent && (
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 opacity-0 group-hover:opacity-100"
          onClick={() => actions.removeFromQueue(index)}
          title="Remove from queue"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      )}
    </div>
  );
}
