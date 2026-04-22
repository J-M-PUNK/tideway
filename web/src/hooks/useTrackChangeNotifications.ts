import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import type { Track } from "@/api/types";

/**
 * Fires an OS notification when the currently-playing track changes,
 * IF the window is not focused and the user opted in. Window-focused
 * check is the whole point — the in-app now-playing bar already tells
 * the user what's playing when they're looking at it, so firing a
 * bezel in that case would just be noise.
 *
 * Triggers:
 *   - `track.id` changes to a new non-null value
 *   - !document.hasFocus()
 *   - `enabled` preference is true
 *
 * The first track after enabling still fires, because the ref starts
 * null and any non-null track is "new". That's the right behavior —
 * the user just enabled it, they want feedback.
 */
export function useTrackChangeNotifications(
  enabled: boolean,
  track: Track | null,
) {
  const lastNotifiedId = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      // Forget last-notified state so a re-enable doesn't suppress
      // the next genuinely-new track.
      lastNotifiedId.current = null;
      return;
    }
    if (!track) return;
    if (track.id === lastNotifiedId.current) return;
    lastNotifiedId.current = track.id;
    if (typeof document !== "undefined" && document.hasFocus()) return;
    const artists =
      track.artists.map((a) => a.name).join(", ") || "Unknown artist";
    api.notify(track.name, artists, track.album?.name ?? undefined);
  }, [enabled, track]);
}
