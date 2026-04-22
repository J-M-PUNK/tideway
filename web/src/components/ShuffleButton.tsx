import { Shuffle } from "lucide-react";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { cn } from "@/lib/utils";

/**
 * Shuffle toggle. This used to be a one tap shuffle-play button
 * that both flipped the shuffle flag and started playback in the
 * same click. Now it just flips the flag. The user still has to
 * press Play, which is how Spotify and Apple Music both handle
 * this. When shuffle is on the glyph turns brand purple so the
 * paired Play and Shuffle buttons make the current mode obvious.
 *
 * The button has no filled background. If it did, it would look
 * like another primary action sitting next to Play and would
 * compete with the real Play button for attention.
 */
export function ShuffleButton({
  size = "md",
}: {
  size?: "lg" | "md";
}) {
  const { shuffle } = usePlayerMeta();
  const actions = usePlayerActions();

  const dim = size === "lg" ? "h-14 w-14" : "h-12 w-12";
  const icon = size === "lg" ? "h-6 w-6" : "h-5 w-5";

  return (
    <button
      onClick={actions.toggleShuffle}
      className={cn(
        dim,
        "flex flex-shrink-0 items-center justify-center rounded-full bg-transparent transition-colors",
        shuffle
          ? "text-primary hover:text-primary/80"
          : "text-muted-foreground hover:text-foreground",
      )}
      aria-label={shuffle ? "Turn off shuffle" : "Turn on shuffle"}
      aria-pressed={shuffle}
      title={shuffle ? "Shuffle on — click to turn off" : "Turn on shuffle"}
    >
      <Shuffle className={icon} />
    </button>
  );
}
