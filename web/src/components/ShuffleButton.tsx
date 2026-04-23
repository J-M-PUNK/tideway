import { Shuffle } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Controlled shuffle toggle. Sits on collection pages (album,
 * artist, playlist, mix, radio) next to the Play button and
 * expresses a pre-selection: do you want playback to be shuffled
 * when you press Play on this page?
 *
 * Click only flips the value the parent passed in. It does not
 * touch the global player state and does not affect whatever is
 * currently playing. The parent is responsible for persisting
 * that state and handing it to the Play button, which actually
 * applies the shuffle when starting a new queue.
 *
 * This mirrors how Spotify's Shuffle button on a collection page
 * works. The live shuffle toggle for the currently playing queue
 * lives on the bottom player bar, which binds to the global
 * player state directly.
 */
export function ShuffleButton({
  value,
  onChange,
  size = "md",
}: {
  value: boolean;
  onChange: (next: boolean) => void;
  size?: "lg" | "md";
}) {
  const dim = size === "lg" ? "h-14 w-14" : "h-12 w-12";
  const icon = size === "lg" ? "h-6 w-6" : "h-5 w-5";

  return (
    <button
      onClick={() => onChange(!value)}
      className={cn(
        dim,
        "flex flex-shrink-0 items-center justify-center rounded-full bg-transparent transition-colors",
        value
          ? "text-primary hover:text-primary/80"
          : "text-muted-foreground hover:text-foreground",
      )}
      aria-label={value ? "Turn off shuffle" : "Turn on shuffle"}
      aria-pressed={value}
      title={
        value
          ? "Shuffle will start when you press Play"
          : "Press to shuffle on next Play"
      }
    >
      <Shuffle className={icon} />
    </button>
  );
}
