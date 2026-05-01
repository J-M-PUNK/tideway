import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MutableRefObject,
} from "react";
import { api } from "@/api/client";

/**
 * Sleep-timer state machine, lifted out of `usePlayer` so the
 * timeout / tick / end-of-track flag triple isn't tangled into the
 * hook's already-busy effect graph.
 *
 * Modes:
 *
 *   - `setSleepTimer(N)` (where N is a positive integer) — pause
 *     after N minutes. Ticks every second so the UI can render a
 *     countdown.
 *
 *   - `setSleepTimer("end-of-track")` — flip the
 *     `endOfTrackPendingRef` flag the player's `advanceRef` reads.
 *     The next natural track-end advance is suppressed and the
 *     flag is auto-cleared. No countdown — the UI just shows the
 *     "stop at end of track" badge until the track ends.
 *
 *   - `setSleepTimer(null)` — cancel any pending timer.
 *
 *   - `clearSleepTimer()` — same as setting to null. Also called on
 *     unmount to make sure timers don't outlive the component.
 *
 * Returns `sleepRemaining`:
 *   - `null` when no timer is active.
 *   - `-1` when the "stop at end of track" mode is armed (sentinel
 *     so the UI can render the badge differently).
 *   - A positive number of milliseconds remaining otherwise; ticks
 *     down once per second.
 *
 * The `endOfTrackPendingRef` is owned by `usePlayer` (because
 * `advanceRef` reads it on the SSE "ended" path); we just toggle it.
 * That coupling is intentional — the alternative would be a
 * subscription bus from this hook back into the player effect, and
 * a single shared ref is simpler than the wiring.
 */
export function useSleepTimer(endOfTrackPendingRef: MutableRefObject<boolean>) {
  const sleepTimeoutRef = useRef<number | null>(null);
  const sleepTickRef = useRef<number | null>(null);
  const [sleepRemaining, setSleepRemaining] = useState<number | null>(null);

  const clearSleepTimer = useCallback(() => {
    if (sleepTimeoutRef.current !== null) {
      window.clearTimeout(sleepTimeoutRef.current);
      sleepTimeoutRef.current = null;
    }
    if (sleepTickRef.current !== null) {
      window.clearTimeout(sleepTickRef.current);
      sleepTickRef.current = null;
    }
    endOfTrackPendingRef.current = false;
    setSleepRemaining(null);
  }, [endOfTrackPendingRef]);

  // Auto-cancel on unmount so a timer doesn't fire pause() against
  // a dead component / unmounted player.
  useEffect(() => clearSleepTimer, [clearSleepTimer]);

  const setSleepTimer = useCallback(
    (minutes: number | "end-of-track" | null) => {
      clearSleepTimer();
      if (minutes === null) return;
      if (minutes === "end-of-track") {
        endOfTrackPendingRef.current = true;
        setSleepRemaining(-1);
        return;
      }
      const ms = minutes * 60 * 1000;
      const fireAt = Date.now() + ms;
      setSleepRemaining(ms);
      sleepTimeoutRef.current = window.setTimeout(() => {
        void api.player.pause().catch(() => {});
        clearSleepTimer();
      }, ms);
      const tick = () => {
        const left = fireAt - Date.now();
        if (left <= 0) {
          sleepTickRef.current = null;
          return;
        }
        setSleepRemaining(left);
        sleepTickRef.current = window.setTimeout(tick, 1000);
      };
      sleepTickRef.current = window.setTimeout(tick, 1000);
    },
    [clearSleepTimer, endOfTrackPendingRef],
  );

  return { sleepRemaining, setSleepTimer, clearSleepTimer };
}
