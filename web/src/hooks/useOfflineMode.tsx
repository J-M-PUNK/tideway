import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "@/api/client";

/**
 * Global offline-mode state.
 *
 * Two sources combine into one effective `offline` flag:
 *
 * 1. User preference (persisted in settings.json). Flipped manually
 *    from Settings. Intentional, survives restarts.
 * 2. Auto-detected loss of connectivity. Driven by the browser's
 *    `navigator.onLine` + the `online` / `offline` window events.
 *    Transient; not persisted.
 *
 * `offline` returned to consumers is the OR of the two — if either
 * says offline, the app gates network-dependent surfaces off. The
 * `offlineSource` field lets UI (e.g. the top-of-app banner)
 * distinguish between "user chose this" and "your wifi dropped."
 *
 * `null` while loading the user setting and when that probe failed
 * without auth — treated as "not offline" for gating decisions.
 */
type OfflineSource = "user" | "auto" | null;

type OfflineCtx = {
  offline: boolean | null;
  offlineSource: OfflineSource;
  set: (v: boolean) => void;
  reload: () => Promise<void>;
};

const Ctx = createContext<OfflineCtx | null>(null);

function readNavigatorOnline(): boolean {
  if (typeof navigator === "undefined") return true;
  // `navigator.onLine` is "true" unless the browser is confident
  // there's no network at all (WiFi off, airplane mode, cable
  // unplugged). A flaky or captive-portal network shows up as
  // online, which is a limitation of the browser API — not
  // something we can fix from here.
  return navigator.onLine !== false;
}

export function OfflineProvider({ children }: { children: React.ReactNode }) {
  const [userOffline, setUserOffline] = useState<boolean | null>(null);
  const [autoOffline, setAutoOffline] = useState<boolean>(
    () => !readNavigatorOnline(),
  );

  const reload = useCallback(async () => {
    try {
      const s = await api.settings.get();
      setUserOffline(s.offline_mode);
    } catch {
      setUserOffline(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // Wire navigator.onLine. The events fire synchronously on WiFi
  // on/off, laptop wake-from-sleep with no network, airplane-mode
  // toggle, etc. We don't probe Tidal specifically — the backend
  // will surface its own errors if it's the Tidal side that's
  // unreachable while the LAN is fine.
  useEffect(() => {
    const onOnline = () => setAutoOffline(false);
    const onOffline = () => setAutoOffline(true);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  // Compose the effective offline state. `userOffline === null`
  // means the settings probe is still in flight — we don't let
  // that delay auto-offline surfacing to the UI.
  const value = useMemo<OfflineCtx>(() => {
    const userBool = userOffline === true;
    const effective =
      userOffline === null
        ? autoOffline
          ? true
          : null
        : userBool || autoOffline;
    const source: OfflineSource = userBool
      ? "user"
      : autoOffline
        ? "auto"
        : null;
    return {
      offline: effective,
      offlineSource: source,
      set: setUserOffline,
      reload,
    };
  }, [userOffline, autoOffline, reload]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useOfflineMode(): OfflineCtx {
  const ctx = useContext(Ctx);
  if (!ctx)
    throw new Error("useOfflineMode must be used within OfflineProvider");
  return ctx;
}
