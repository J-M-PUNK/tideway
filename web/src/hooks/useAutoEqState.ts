import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";

/**
 * Fetches the current AutoEQ state (mode, active profile, bypass
 * flag, manual bands) once on mount and provides a `refresh` so
 * callers can re-pull after they mutate state via the API.
 *
 * Phase 4 intentionally doesn't add an SSE channel for EQ state —
 * the user only changes EQ from one place (Settings) plus the
 * Phase 4 A/B bypass button, and both call back to refresh
 * directly. SSE would be the right answer if a future phase
 * adds device-driven changes (auto-swap on output change) that
 * the user needs to see reflected in the now-playing UI live —
 * that's part of Phase 6 / 7.
 */
export type AutoEqMode = "off" | "manual" | "profile";

export interface AutoEqProfileSummary {
  id: string;
  brand: string;
  model: string;
  source: string;
  preamp_db: number;
  band_count: number;
}

export interface AutoEqTilt {
  preamp_offset_db: number;
  bass_db: number;
  treble_db: number;
}

export interface AutoEqState {
  mode: AutoEqMode;
  enabled: boolean;
  bypass: boolean;
  active_profile_id: string;
  active_profile: AutoEqProfileSummary | null;
  manual_bands: number[];
  manual_preamp_db: number | null;
  profile_catalog_size: number;
  tilt: AutoEqTilt;
}

export function useAutoEqState(enabled: boolean): {
  state: AutoEqState | null;
  refresh: () => Promise<void>;
  setBypass: (bypass: boolean) => Promise<void>;
} {
  const [state, setState] = useState<AutoEqState | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    try {
      const s = await api.player.autoEqState();
      setState(s);
    } catch {
      /* feature not available — keep null, callers hide */
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setBypass = useCallback(async (bypass: boolean) => {
    // Optimistic flip — the audio change is instant, no point
    // making the UI wait for the round-trip.
    setState((prev) => (prev ? { ...prev, bypass } : prev));
    try {
      await api.player.autoEqSetBypass(bypass);
    } catch {
      // Roll back on failure.
      setState((prev) => (prev ? { ...prev, bypass: !bypass } : prev));
    }
  }, []);

  return { state, refresh, setBypass };
}
