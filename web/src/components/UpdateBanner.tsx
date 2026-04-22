import { useEffect, useState } from "react";
import { Download, Loader2, X } from "lucide-react";
import { api } from "@/api/client";
import { useToast } from "@/components/toast";

/**
 * Update-check banner. Calls /api/update-check once on mount (backend
 * hits api.github.com/repos/.../releases/latest, cached 1h, bypasses
 * GitHub's 60/h rate limit worst-case by just silently reporting no
 * update).
 *
 * Two affordances when an update is available:
 *   - "Install now" — downloads the platform installer via the backend
 *     (~100 MB DMG / .exe), opens it, and quits the app so the user
 *     can run it against a clean slate.
 *   - "View release" — opens the GitHub release page in the system
 *     browser. Fallback for Linux (no installer asset today) and for
 *     users who want to read the release notes first.
 *
 * Users can dismiss per-version; a newer release re-triggers the
 * banner on next launch.
 */
const DISMISSED_KEY = "tideway:update-dismissed-version";

export function UpdateBanner() {
  const toast = useToast();
  const [update, setUpdate] = useState<{
    available: boolean;
    latest: string | null;
    url: string | null;
  } | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [installing, setInstalling] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.updateCheck()
      .then((res) => {
        if (cancelled) return;
        setUpdate(res);
        try {
          const d = localStorage.getItem(DISMISSED_KEY);
          if (d && res.latest && d === res.latest) setDismissed(true);
        } catch {
          /* ignore */
        }
      })
      .catch(() => {
        /* offline / rate-limited — no banner */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!update || !update.available || dismissed) return null;

  const onDismiss = () => {
    setDismissed(true);
    try {
      if (update.latest) localStorage.setItem(DISMISSED_KEY, update.latest);
    } catch {
      /* ignore */
    }
  };

  const onView = async () => {
    if (!update.url) return;
    try {
      await api.openExternal(update.url);
    } catch {
      window.open(update.url, "_blank", "noopener");
    }
  };

  const onInstall = async () => {
    setInstalling(true);
    try {
      const res = await api.updateInstall();
      if (!res.ok) {
        toast.show({
          kind: "error",
          title: "Couldn't install update",
          description: res.reason || "No installer for this platform.",
        });
        return;
      }
      toast.show({
        kind: "info",
        title: "Installer downloaded — opening now",
        description: res.downloaded_to || undefined,
      });
      // Quit after the installer has had a moment to surface. Quitting
      // too eagerly races the Finder / installer process and the user
      // loses sight of the prompt. 1.5 s is enough for `open` to
      // launch the DMG / Explorer to open the .exe.
      window.setTimeout(() => {
        api.quitApp().catch(() => {
          /* ignore */
        });
      }, 1500);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Update failed",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setInstalling(false);
    }
  };

  return (
    <div className="flex items-center gap-3 border-b border-primary/30 bg-primary/10 px-6 py-2 text-sm">
      <Download className="h-4 w-4 flex-shrink-0 text-primary" />
      <div className="min-w-0 flex-1">
        <span className="font-medium">Update available:</span>{" "}
        <span className="text-muted-foreground">{update.latest}</span>
      </div>
      <button
        type="button"
        onClick={onInstall}
        disabled={installing}
        className="flex items-center gap-1.5 rounded bg-primary px-3 py-1 text-xs font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
      >
        {installing && <Loader2 className="h-3 w-3 animate-spin" />}
        {installing ? "Downloading…" : "Install now"}
      </button>
      <button
        type="button"
        onClick={onView}
        className="rounded border border-primary/40 px-3 py-1 text-xs font-semibold text-primary hover:bg-primary/15"
      >
        Release notes
      </button>
      <button
        type="button"
        onClick={onDismiss}
        title="Dismiss"
        aria-label="Dismiss"
        className="flex h-7 w-7 items-center justify-center rounded text-muted-foreground hover:bg-primary/15 hover:text-foreground"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
