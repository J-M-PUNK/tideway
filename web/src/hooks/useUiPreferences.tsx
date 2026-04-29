import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

const STORAGE_KEY = "tideway:ui-prefs";

/** Quality values that the browser's `<audio>` element can actually
 *  play. The backend concatenates DASH segments server-side for the
 *  lossless tiers, so all four qualities are now streamable — hi-res
 *  needs a PKCE session and enough bandwidth (~140 MB for a 4-min
 *  24/192 FLAC), but works the same way as Lossless otherwise. */
export type StreamingQuality =
  | "low_96k"
  | "low_320k"
  | "high_lossless"
  | "hi_res_lossless";

export type ThemeMode = "dark" | "light";

interface UiPreferences {
  /** When true, TrackList instances hide any track not present on disk.
   *  Useful when you're on a plane and want to stop seeing tracks you
   *  can't actually play anyway. */
  offlineOnly: boolean;
  /** Quality at which the Now-Playing bar streams non-downloaded tracks.
   *  Doesn't affect local files or downloads. */
  streamingQuality: StreamingQuality;
  /** Active color theme. Applied via a root-element class so CSS
   *  variables in index.css can swap their values. */
  theme: ThemeMode;
  /** When true, hide the Import entry from the sidebar. The feature
   *  stays reachable from Settings; this is just the user saying
   *  "I've seen it, stop suggesting it." */
  importLinkDismissed: boolean;
}

interface UiPreferencesContextValue extends UiPreferences {
  set: (patch: Partial<UiPreferences>) => void;
}

const DEFAULTS: UiPreferences = {
  offlineOnly: false,
  // Default to Max (hi-res lossless). Backend clamps down to
  // whatever the subscription tier actually allows, so users on
  // HiFi/Free still get the best quality available to them without
  // 401s — no client-side pre-gating needed.
  streamingQuality: "hi_res_lossless",
  theme: "dark",
  importLinkDismissed: false,
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

  // Flip the `light` class on <html> whenever the theme pref changes.
  // CSS variables in index.css default to dark; the `.light` block
  // overrides them. Running this before paint (useLayoutEffect would
  // also work) avoids a visible flash on first mount.
  useEffect(() => {
    const root = document.documentElement;
    if (prefs.theme === "light") root.classList.add("light");
    else root.classList.remove("light");
    // Push the theme down to the OS shell so the native window
    // titlebar (where the close / minimize buttons live) tints to
    // match the app body. Loopback-only endpoint, no auth, fire
    // and forget — a 403/404 in browser-only dev mode (no shell)
    // is the expected outcome and we want to swallow it silently.
    fetch("/api/_internal/window-theme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme: prefs.theme }),
    }).catch(() => {
      /* no shell, no problem */
    });
  }, [prefs.theme]);

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
