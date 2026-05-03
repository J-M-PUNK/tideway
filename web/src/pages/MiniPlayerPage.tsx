import { Pause, Play, SkipBack, SkipForward, Music } from "lucide-react";
import {
  usePlayerActions,
  usePlayerMeta,
  usePlayerTime,
} from "@/hooks/PlayerContext";
import { formatDuration, imageProxy } from "@/lib/utils";
import { StreamQualityBadge } from "@/components/StreamQualityBadge";
import { cn } from "@/lib/utils";

/**
 * Compact floating mini-player. Lives in its own pywebview window
 * (opened from the user menu) with `on_top=True` so it floats above
 * whatever the user is working on.
 *
 * Both the main window and the mini-player subscribe to the same
 * /api/player/events SSE stream and POST to the same transport
 * endpoints, so playback is coherent across both surfaces without
 * any explicit sync. Stopping in one stops both, skipping in one
 * skips both, etc.
 *
 * No sidebar, no settings, no library browsing — the mini-player is
 * just transport + now-playing readout. Users who want more navigate
 * back to the main window.
 */
export function MiniPlayerPage() {
  const { track, playing, hasPrev, streamInfo } = usePlayerMeta();
  const { currentTime, duration } = usePlayerTime();
  const actions = usePlayerActions();

  if (!track) {
    return (
      <div className="flex h-full w-full flex-1 items-center justify-center bg-[hsl(var(--now-playing-bg))] text-xs text-muted-foreground">
        <Music className="mr-2 h-4 w-4" />
        Nothing playing
      </div>
    );
  }

  const cover = imageProxy(track.album?.cover);
  const pct = duration > 0 ? (currentTime / duration) * 100 : 0;
  const artists = track.artists.map((a) => a.name).join(", ");

  return (
    <div className="flex h-full w-full flex-1 select-none flex-col gap-2 bg-[hsl(var(--now-playing-bg))] px-3 py-2">
      <div className="flex items-center gap-3">
        <div className="h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary">
          {cover ? (
            <img src={cover} alt="" className="h-full w-full object-cover" />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-muted-foreground">
              <Music className="h-5 w-5" />
            </div>
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">{track.name}</div>
          <div className="flex items-center gap-2 truncate text-xs text-muted-foreground">
            <span className="truncate">{artists}</span>
            <StreamQualityBadge info={streamInfo} />
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={actions.prev}
            disabled={!hasPrev}
            title="Previous"
            aria-label="Previous"
            className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground hover:text-foreground disabled:opacity-30"
          >
            <SkipBack className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={actions.toggle}
            title={playing ? "Pause" : "Play"}
            aria-label={playing ? "Pause" : "Play"}
            className="flex h-9 w-9 items-center justify-center rounded-full bg-foreground text-background transition-transform hover:scale-105"
          >
            {playing ? (
              <Pause className="h-4 w-4" fill="currentColor" />
            ) : (
              <Play className="h-4 w-4 ml-0.5" fill="currentColor" />
            )}
          </button>
          <button
            onClick={actions.next}
            title="Next"
            aria-label="Next"
            className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground hover:text-foreground"
          >
            <SkipForward className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
      <div className="flex items-center gap-2 text-[10px] tabular-nums text-muted-foreground">
        <span className="w-8 text-right">{formatDuration(currentTime)}</span>
        <div className="relative h-1 flex-1 rounded-full bg-muted-foreground/20">
          <div
            className={cn(
              "absolute inset-y-0 left-0 rounded-full bg-foreground transition-[width]",
            )}
            style={{ width: `${pct}%` }}
          />
        </div>
        <span className="w-8">{formatDuration(duration)}</span>
      </div>
    </div>
  );
}
