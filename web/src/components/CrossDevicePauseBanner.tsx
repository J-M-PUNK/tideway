import { Pause, X } from "lucide-react";
import { api } from "@/api/client";
import { usePlayerMeta } from "@/hooks/PlayerContext";

/**
 * Strip above the play bar that explains why playback stopped when
 * another device on the user's Tidal account took over. Renders
 * only when the backend's `paused_by_device` field is non-null;
 * otherwise nothing.
 *
 * Cleared automatically when the user resumes playback locally
 * (the lifespan's player-state listener nulls the server-side
 * pause reason on the next play). The X button explicitly hits
 * `/api/player/dismiss-pause-reason` so users can dismiss without
 * having to play something.
 */
export function CrossDevicePauseBanner() {
  const { pausedByDevice } = usePlayerMeta();

  if (!pausedByDevice) return null;

  const onDismiss = () => {
    // Best-effort. The server clears `_cross_device_pause_device`
    // and returns the fresh snapshot, which the next SSE tick
    // will reflect. If the call fails, the banner sticks around
    // until the next play; not great, not catastrophic.
    api.player.dismissPauseReason().catch(() => {
      /* ignore */
    });
  };

  return (
    <div
      role="status"
      className="flex items-center gap-3 border-t border-muted bg-muted/40 px-6 py-2 text-sm"
    >
      <Pause className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <span className="font-medium">Paused</span>
        <span className="text-muted-foreground">
          {" "}
          — you're playing on {pausedByDevice}.
        </span>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        title="Dismiss"
        aria-label="Dismiss"
        className="flex h-7 w-7 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
