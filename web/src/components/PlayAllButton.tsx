import { Pause, Play } from "lucide-react";
import type { Track } from "@/api/types";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import type { PlaySource } from "@/hooks/usePlayer";
import { cn } from "@/lib/utils";

interface Props {
  tracks: Track[];
  size?: "lg" | "md";
  /** The container the tracks came from. Passing this makes plays
   *  attribute to the album/playlist/mix for Tidal Recently Played
   *  aggregation; without it they get reported as sourceType=TRACK
   *  which Tidal files under aggregate stats but not Recently Played. */
  source?: PlaySource;
  /** Pre-selection from the page's ShuffleButton. When true and
   *  the user starts a new queue by pressing Play, the first
   *  track is a random pick from the list and the global shuffle
   *  flag gets turned on so subsequent Next picks stay random.
   *  When false, the global shuffle flag is cleared so the queue
   *  plays in track order. A no-op when the button is in "pause
   *  currently playing" mode — switching shuffle mid-playback is
   *  the bottom bar's job. */
  shuffleIntent?: boolean;
}

/**
 * Big round Play button that queues a list of tracks into the player.
 * If the current track is already in this queue, the button becomes
 * Pause. Default size is `md` — large enough to be the clear primary
 * CTA on a detail page, small enough to pair visually with the other
 * labeled action buttons in the actions row.
 */
export function PlayAllButton({
  tracks,
  size = "md",
  source,
  shuffleIntent = false,
}: Props) {
  const { track, playing, shuffle } = usePlayerMeta();
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
    // Apply the shuffle pre-selection to the global player state
    // before the queue starts, so the bottom bar reflects the new
    // state immediately and subsequent Next picks respect it.
    if (shuffleIntent !== shuffle) {
      actions.toggleShuffle();
    }
    const seed = shuffleIntent
      ? tracks[Math.floor(Math.random() * tracks.length)]
      : tracks[0];
    actions.play(seed, tracks, source);
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
