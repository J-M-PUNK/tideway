import { useCallback, useEffect, useSyncExternalStore } from "react";
import { api } from "@/api/client";

/**
 * Module-level cache for the bottom-bar output-device picker. The
 * picker mounts in both the active and empty player bar, so every
 * track start / track end cycle unmounts one and mounts the other;
 * a per-mount fetch would refetch devices + settings on every track
 * boundary. Keeping state in a module-level external store shares
 * one fetch across remounts.
 */

interface Device {
  id: string;
  name: string;
}

export interface AudioOptions {
  devices: Device[];
  current: string;
  exclusiveMode: boolean;
  forceVolume: boolean;
  loaded: boolean;
}

const initial: AudioOptions = {
  devices: [],
  current: "",
  exclusiveMode: false,
  forceVolume: false,
  loaded: false,
};

let state: AudioOptions = initial;
const listeners = new Set<() => void>();
let inflight: Promise<void> | null = null;

function setState(next: AudioOptions): void {
  // Skip same-value updates so clicking the already-selected device
  // or toggling an already-on option doesn't wake every subscriber
  // and re-render consumers.
  if (
    next.current === state.current &&
    next.exclusiveMode === state.exclusiveMode &&
    next.forceVolume === state.forceVolume &&
    next.loaded === state.loaded &&
    next.devices === state.devices
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

function getSnapshot(): AudioOptions {
  return state;
}

async function loadOnce(): Promise<void> {
  if (state.loaded || inflight) return inflight ?? Promise.resolve();
  inflight = (async () => {
    try {
      const [d, s] = await Promise.all([
        api.player.outputDevices(),
        api.settings.get(),
      ]);
      setState({
        devices: d.devices,
        current: d.current,
        exclusiveMode: !!s.exclusive_mode,
        forceVolume: !!s.force_volume,
        loaded: true,
      });
    } catch {
      setState({ ...initial, loaded: true });
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

/**
 * Apply an optimistic field update, run the commit, roll back on
 * failure. Used by the public setters below so the
 * optimistic-update-with-rollback pattern lives in one place.
 */
async function applyOptimistic<K extends keyof AudioOptions>(
  field: K,
  value: AudioOptions[K],
  commit: () => Promise<unknown>,
): Promise<void> {
  const prev = state[field];
  setState({ ...state, [field]: value });
  try {
    await commit();
  } catch (err) {
    setState({ ...state, [field]: prev });
    throw err;
  }
}

export function useAudioOptions() {
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    loadOnce();
  }, []);

  const setDevice = useCallback(
    (id: string) =>
      applyOptimistic("current", id, () => api.player.setOutputDevice(id)),
    [],
  );

  const setExclusiveMode = useCallback(
    (v: boolean) =>
      applyOptimistic("exclusiveMode", v, () =>
        api.settings.put({ exclusive_mode: v }),
      ),
    [],
  );

  const setForceVolume = useCallback(
    (v: boolean) =>
      applyOptimistic("forceVolume", v, () =>
        api.settings.put({ force_volume: v }),
      ),
    [],
  );

  return { ...snap, setDevice, setExclusiveMode, setForceVolume };
}
