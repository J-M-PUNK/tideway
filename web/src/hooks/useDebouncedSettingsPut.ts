import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import type { Settings } from "@/api/types";

/**
 * Debounced PUT for self-contained settings fields.
 *
 * The main SettingsPage already debounces its global-state-driven
 * autosave at 600 ms, but self-contained Field components
 * (CrossfeedField, ReplayGainField) fetch their own initial state
 * via `api.settings.get()` and PUT directly — they bypass the
 * global autosave entirely, so without their own debounce a
 * slider drag would fire one PUT per pointer-move event.
 *
 * Returns a `send` function that accumulates partial Settings
 * patches and flushes them as one PUT after `delayMs` of quiet.
 * Multiple keys batched in the same window collapse into a single
 * PUT — useful for the ReplayGain section where toggling mode +
 * dragging preamp can fire in quick succession.
 *
 * Best-effort: any in-flight timer at unmount is cleared without
 * flushing. The audio engine reads from settings on each PUT, and
 * a lost trailing edge is recoverable on next mount (the field
 * re-fetches via `api.settings.get`).
 */
export function useDebouncedSettingsPut(
  delayMs = 200,
): (patch: Partial<Settings>) => void {
  const timerRef = useRef<number | null>(null);
  const pendingRef = useRef<Partial<Settings>>({});

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  return (patch: Partial<Settings>) => {
    pendingRef.current = { ...pendingRef.current, ...patch };
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      const payload = pendingRef.current;
      pendingRef.current = {};
      timerRef.current = null;
      void api.settings.put(payload).catch(() => {
        /* Self-contained fields surface errors via their own state
           on next mount; a failed background save is acceptable. */
      });
    }, delayMs);
  };
}
