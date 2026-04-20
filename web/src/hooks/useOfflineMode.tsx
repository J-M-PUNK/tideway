import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";

/**
 * Global offline-mode state. Separate from useAuth because it's a
 * user preference (persisted server-side in settings.json) rather
 * than a session fact. When on, the app lets a signed-out user
 * browse and play already-downloaded tracks without hitting Tidal.
 *
 * Fetched once on mount. SettingsPage calls `set` after its autosave
 * so the rest of the tree reacts without a reload. `null` while
 * loading and when the probe failed without auth — treat as "not
 * offline" for gating decisions.
 */
type OfflineCtx = {
  offline: boolean | null;
  set: (v: boolean) => void;
  reload: () => Promise<void>;
};

const Ctx = createContext<OfflineCtx | null>(null);

export function OfflineProvider({ children }: { children: React.ReactNode }) {
  const [offline, setOffline] = useState<boolean | null>(null);

  const reload = useCallback(async () => {
    try {
      const s = await api.settings.get();
      setOffline(s.offline_mode);
    } catch {
      // 401 when neither signed in nor offline — both mean "not offline
      // from the app's point of view", so we coalesce to false. The auth
      // gate will redirect to Login.
      setOffline(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // Memo the value so consumers don't re-render every time the
  // provider's parent re-renders — only when `offline` actually
  // changes. `set` and `reload` are already stable.
  const value = useMemo(
    () => ({ offline, set: setOffline, reload }),
    [offline, reload],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useOfflineMode(): OfflineCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useOfflineMode must be used within OfflineProvider");
  return ctx;
}
