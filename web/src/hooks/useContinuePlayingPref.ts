import { useEffect, useRef, type MutableRefObject } from "react";
import { api } from "@/api/client";

/**
 * Mirror the backend's `continue_playing_after_queue_ends` setting
 * as a live-updating ref. Returns the ref directly so callers can
 * read `.current` from inside event handlers without taking the
 * value as a dep (which would force the handler to re-install on
 * every settings change).
 *
 * Two update paths:
 *
 *   - Mount: GET /api/settings, set the ref from the response.
 *     Failure (no auth / offline) leaves the ref at the default,
 *     which mirrors the backend default for fresh installs so
 *     behaviour is consistent on first launch.
 *
 *   - Live: a `tidal-settings-updated` window event dispatched by
 *     SettingsPage after a successful PUT carries the new value
 *     in `detail.continue_playing_after_queue_ends`. The advance-
 *     queue path inside `usePlayer.advanceRef` reads the ref
 *     synchronously when the queue runs out, so the user gets the
 *     new behaviour immediately without needing to restart playback.
 *
 * The default-on (`true`) mirrors the backend default, so a user
 * who never opens Settings ends up with the same auto-radio
 * experience as if the backend's default had loaded.
 */
export function useContinuePlayingPref(): MutableRefObject<boolean> {
  const ref = useRef(true);
  useEffect(() => {
    let cancelled = false;
    api.settings
      .get()
      .then((s) => {
        if (!cancelled) {
          ref.current = !!s.continue_playing_after_queue_ends;
        }
      })
      .catch(() => {
        /* default: true (set above) */
      });
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail as {
        continue_playing_after_queue_ends?: boolean;
      } | null;
      if (
        detail &&
        typeof detail.continue_playing_after_queue_ends === "boolean"
      ) {
        ref.current = detail.continue_playing_after_queue_ends;
      }
    };
    window.addEventListener("tidal-settings-updated", handler);
    return () => {
      cancelled = true;
      window.removeEventListener("tidal-settings-updated", handler);
    };
  }, []);
  return ref;
}
