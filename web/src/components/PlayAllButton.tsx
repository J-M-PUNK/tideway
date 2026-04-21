import { Pause, Play } from "lucide-react";
import type { Track } from "@/api/types";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { cn } from "@/lib/utils";

interface Props {
  tracks: Track[];
  size?: "lg" | "md";
}

/**
 * Big round Play button that queues a list of tracks into the player.
 * If the current track is already in this queue, the button becomes
 * Pause. Default size is `md` — large enough to be the clear primary
 * CTA on a detail page, small enough to pair visually with the other
 * labeled action buttons in the actions row.
 */
export function PlayAllButton({ tracks, size = "md" }: Props) {
  const { track, playing } = usePlayerMeta();
  const actions = usePlayerActions();

  const dim = size === "lg" ? "h-14 w-14" : "h-12 w-12";
  const icon = size === "lg" ? "h-6 w-6" : "h-5 w-5";
  const isOurQueue = !!track && tracks.some((t) => t.id === track.id);

  const onClick = () => {
    if (isOurQueue) {
      actions.toggle();
      return;
    }
    if (tracks.length === 0) return;
    actions.play(tracks[0], tracks);
  };

  const isPlaying = isOurQueue && playing;

  return (
    <button
      onClick={onClick}
      disabled={tracks.length === 0}
      className={cn(
        dim,
        "flex flex-shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg transition-all hover:scale-105 active:scale-95 disabled:opacity-40",
      )}
      aria-label={isPlaying ? "Pause" : "Play"}
    >
      {isPlaying ? (
        <Pause className={cn(icon)} fill="currentColor" />
      ) : (
        <Play className={cn(icon, "ml-0.5")} fill="currentColor" />
      )}
    </button>
  );
}
