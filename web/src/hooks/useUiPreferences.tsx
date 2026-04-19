import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

const STORAGE_KEY = "tidal-downloader:ui-prefs";

/** Quality values that the browser's `<audio>` element can actually
 *  play. Hi-res (hi_res_lossless) is intentionally excluded — it's
 *  DASH-segmented and not natively streamable. Download the track for
 *  that quality. */
export type StreamingQuality = "low_96k" | "low_320k" | "high_lossless";

interface UiPreferences {
  /** When true, TrackList instances hide any track not present on disk.
   *  Useful when you're on a plane and want to stop seeing tracks you
   *  can't actually play anyway. */
  offlineOnly: boolean;
  /** Quality at which the Now-Playing bar streams non-downloaded tracks.
   *  Doesn't affect local files or downloads. */
  streamingQuality: StreamingQuality;
}

interface UiPreferencesContextValue extends UiPreferences {
  set: (patch: Partial<UiPreferences>) => void;
}

const DEFAULTS: UiPreferences = {
  offlineOnly: false,
  streamingQuality: "low_320k",
};

const Ctx = createContext<UiPreferencesContextValue>({
  ...DEFAULTS,
  set: () => {},
});

function load(): UiPreferences {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return DEFAULTS;
    return { ...DEFAULTS, ...parsed };
  } catch {
    return DEFAULTS;
  }
}

/**
 * Local-only preferences that don't touch the backend settings. Stored in
 * localStorage so they survive reloads but stay per-device.
 */
export function UiPreferencesProvider({ children }: { children: ReactNode }) {
  const [prefs, setPrefs] = useState<UiPreferences>(() => load());

  const set = useCallback((patch: Partial<UiPreferences>) => {
    setPrefs((prev) => {
      const next = { ...prev, ...patch };
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* storage full or disabled */
      }
      return next;
    });
  }, []);

  // Sync across tabs — if the user changes the preference in another tab,
  // we pick it up. Only apply parseable values. Without this guard,
  // malformed JSON written by another tab (bug, buggy extension, manual
  // DevTools edit) would make load() fall back to DEFAULTS and silently
  // reset prefs in every OTHER tab.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== STORAGE_KEY) return;
      if (e.newValue === null) {
        setPrefs(DEFAULTS);
        return;
      }
      try {
        const parsed = JSON.parse(e.newValue);
        if (parsed && typeof parsed === "object") {
          setPrefs((prev) => ({ ...prev, ...parsed }));
        }
      } catch {
        /* ignore malformed cross-tab writes */
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const value = useMemo(() => ({ ...prefs, set }), [prefs, set]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useUiPreferences() {
  return useContext(Ctx);
}
