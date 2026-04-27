import { useEffect, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { SubscriptionTier } from "@/api/types";

/**
 * Module-level cache for subscription tier. Fetched once after login
 * (cheap on the backend — `get_max_quality` is already cached for 5
 * minutes), shared across every component that wants to know whether
 * to enable Download. Pattern matches `useAudioOptions`.
 */

export interface SubscriptionInfo {
  tier: SubscriptionTier;
  canDownload: boolean;
  reason: string | null;
  loaded: boolean;
}

/** Default tooltip surfaced on disabled Download buttons when the
 *  backend didn't supply a more specific reason string. */
export const DOWNLOAD_GATE_TOOLTIP =
  "A Tidal HiFi or HiFi Plus subscription is required to download.";

const initial: SubscriptionInfo = {
  tier: "unknown",
  // Optimistic default before the first fetch — we don't want to
  // briefly grey out every Download button on app start while the
  // request is in flight.
  canDownload: true,
  reason: null,
  loaded: false,
};

let state: SubscriptionInfo = initial;
const listeners = new Set<() => void>();
let inflight: Promise<void> | null = null;

function setState(next: SubscriptionInfo): void {
  if (
    next.tier === state.tier &&
    next.canDownload === state.canDownload &&
    next.reason === state.reason &&
    next.loaded === state.loaded
  ) {
    return;
  }
  state = next;
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function getSnapshot(): SubscriptionInfo {
  return state;
}

async function loadOnce(): Promise<void> {
  if (state.loaded || inflight) return inflight ?? Promise.resolve();
  inflight = (async () => {
    try {
      const r = await api.subscription();
      setState({
        tier: r.tier,
        canDownload: r.can_download,
        reason: r.reason,
        loaded: true,
      });
    } catch {
      // Network blip on first load — assume the user CAN download so
      // a transient error doesn't lock the UI. The next request that
      // actually hits Tidal will surface the real error.
      setState({ ...initial, loaded: true });
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

export function useSubscription(): SubscriptionInfo {
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  useEffect(() => {
    loadOnce();
  }, []);
  return snap;
}

/**
 * Force a refetch — call after login or logout so the gate reflects
 * the new account state without waiting for the backend's 5-minute
 * cache TTL. Drops any in-flight fetch first so a stale signed-out
 * response can't overwrite the new login state.
 */
export function refreshSubscription(): void {
  inflight = null;
  state = { ...state, loaded: false };
  loadOnce();
}
