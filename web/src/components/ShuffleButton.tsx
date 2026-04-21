import { Shuffle } from "lucide-react";
import type { Track } from "@/api/types";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { cn } from "@/lib/utils";

/**
 * Shuffle-play a list of tracks — same visual footprint as
 * `PlayAllButton` (big round primary-colored button) so a row of
 * [Play] [Shuffle] reads as two paired CTAs. Enables shuffle mode if
 * it isn't already on and plays a random seed track with the full list
 * as the queue.
 */
export function ShuffleButton({
  tracks,
  size = "md",
}: {
  tracks: Track[];
  size?: "lg" | "md";
}) {
  const { shuffle } = usePlayerMeta();
  const actions = usePlayerActions();

  const dim = size === "lg" ? "h-14 w-14" : "h-12 w-12";
  const icon = size === "lg" ? "h-6 w-6" : "h-5 w-5";

  const onClick = () => {
    if (tracks.length === 0) return;
    if (!shuffle) actions.toggleShuffle();
    const seed = tracks[Math.floor(Math.random() * tracks.length)];
    actions.play(seed, tracks);
  };

  return (
    <button
      onClick={onClick}
      disabled={tracks.length === 0}
      className={cn(
        dim,
        "flex flex-shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg transition-all hover:scale-105 active:scale-95 disabled:opacity-40",
      )}
      aria-label="Shuffle play"
      title="Shuffle play"
    >
      <Shuffle className={icon} />
    </button>
  );
}
