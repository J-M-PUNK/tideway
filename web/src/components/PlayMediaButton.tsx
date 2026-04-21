import { useState } from "react";
import { Loader2, Play } from "lucide-react";
import { api } from "@/api/client";
import { usePlayerActions } from "@/hooks/PlayerContext";
import { useToast } from "@/components/toast";
import { cn } from "@/lib/utils";

/**
 * Icon-only play button for an album or playlist card / row. Clicking
 * fetches the detail payload, then plays the first track with the
 * full tracklist as context (so shuffle / next / prev work). Shows a
 * spinner while the fetch is in flight.
 *
 * Designed for the same overlay slot the DownloadButton uses: hover-
 * revealed corner control on grid cards, inline trailing action on
 * list rows. Artist kind is unsupported (playing "an artist" would
 * need a radio seed — we have a dedicated Artist Radio button for
 * that).
 */
export function PlayMediaButton({
  kind,
  id,
  className,
  onOpenChange,
}: {
  kind: "album" | "playlist";
  id: string;
  className?: string;
  /** Matches DownloadButton's prop so the hover-reveal CSS can latch
   *  onto the same busy state (prevents the button flashing back to
   *  opacity-0 mid-fetch when the cursor drifts off). */
  onOpenChange?: (open: boolean) => void;
}) {
  const actions = usePlayerActions();
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const onClick = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    onOpenChange?.(true);
    try {
      const detail =
        kind === "album" ? await api.album(id) : await api.playlist(id);
      const tracks = detail.tracks;
      if (!tracks?.length) {
        toast.show({
          kind: "info",
          title: "Nothing to play",
          description: `This ${kind} has no playable tracks.`,
        });
        return;
      }
      actions.play(tracks[0], tracks);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't start playback",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
      onOpenChange?.(false);
    }
  };

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      title={busy ? "Loading…" : "Play"}
      aria-label={busy ? "Loading" : "Play"}
      className={cn(
        "flex items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg transition-transform hover:scale-105 disabled:opacity-80",
        className,
      )}
    >
      {busy ? (
        <Loader2 className="h-5 w-5 animate-spin" />
      ) : (
        <Play className="h-5 w-5 fill-current" />
      )}
    </button>
  );
}
