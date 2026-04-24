import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { api } from "@/api/client";

/**
 * Shown when the backend's Tidal request gate has engaged a
 * cooldown — either after an HTTP 429 (soft rate-limit, 1 min) or
 * an `abuse_detected` 403 (anti-abuse flag, 30 min). Explains to
 * the user why navigation / search / play are failing during the
 * window so they don't retry into a harder strike.
 */
export function TidalBackoffBanner() {
  const [state, setState] = useState<{
    active: boolean;
    seconds_remaining: number;
    reason: string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const r = await api.tidalBackoff();
        if (cancelled) return;
        // Only update when something actually changed — otherwise
        // every 15s poll re-renders the app shell even though the
        // banner stays inactive.
        setState((prev) => {
          if (
            prev
            && prev.active === r.active
            && prev.reason === r.reason
            && Math.abs(prev.seconds_remaining - r.seconds_remaining) < 2
          ) {
            return prev;
          }
          return r;
        });
      } catch {
        /* transient — keep the previous state */
      }
    };
    tick();
    const id = window.setInterval(tick, 15_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (!state?.active) return null;
  const minutes = Math.max(1, Math.ceil(state.seconds_remaining / 60));
  return (
    <div className="flex items-center justify-center gap-2 bg-rose-500/20 px-4 py-1.5 text-xs font-semibold text-rose-200">
      <ShieldAlert className="h-3.5 w-3.5" />
      Tidal paused us for ~{minutes}m after detecting heavy API use. Playback,
      search, and navigation will resume automatically — retrying now makes
      the window longer.
    </div>
  );
}
