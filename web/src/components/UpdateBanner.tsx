import { useEffect, useState } from "react";
import { Download, X } from "lucide-react";
import { api } from "@/api/client";

/**
 * Update-check banner. Calls /api/update-check once on mount (backend
 * hits api.github.com/repos/.../releases/latest, cached 1h, bypasses
 * GitHub's 60/h rate limit worst-case by just silently reporting no
 * update). Users can dismiss per-session; the dismissal doesn't
 * persist — a new launch re-checks and re-surfaces if a newer release
 * exists.
 */
const DISMISSED_KEY = "tidal-downloader:update-dismissed-version";

export function UpdateBanner() {
  const [update, setUpdate] = useState<{
    available: boolean;
    latest: string | null;
    url: string | null;
  } | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.updateCheck()
      .then((res) => {
        if (cancelled) return;
        setUpdate(res);
        // Persist dismissal per-version so we don't keep showing the
        // same update after the user has said "not now" — but a newer
        // release after that one re-triggers the banner.
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

  return (
    <div className="flex items-center gap-3 border-b border-primary/30 bg-primary/10 px-6 py-2 text-sm">
      <Download className="h-4 w-4 flex-shrink-0 text-primary" />
      <div className="min-w-0 flex-1">
        <span className="font-medium">Update available:</span>{" "}
        <span className="text-muted-foreground">
          {update.latest}
        </span>
      </div>
      <button
        type="button"
        onClick={onView}
        className="rounded bg-primary px-3 py-1 text-xs font-semibold text-primary-foreground hover:bg-primary/90"
      >
        View release
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
