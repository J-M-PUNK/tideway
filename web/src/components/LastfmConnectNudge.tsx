import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Radio, X } from "lucide-react";
import { api } from "@/api/client";

/**
 * Top-of-Home nudge that quietly pushes users toward connecting
 * Last.fm. Listening stats across every device + historical data is
 * the app's biggest differentiator against Tidal's own "Rewind" —
 * worth surfacing to first-time users who might not realize it's
 * available. Renders nothing when:
 *   - the status probe hasn't landed yet
 *   - Last.fm is already connected
 *   - the user has dismissed this nudge
 */
const DISMISSED_KEY = "tideway:lastfm-nudge-dismissed";

export function LastfmConnectNudge() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    let cancelled = false;
    try {
      if (localStorage.getItem(DISMISSED_KEY) === "1") return;
    } catch {
      /* ignore */
    }
    api.lastfm
      .status()
      .then((s) => {
        if (cancelled) return;
        if (!s.connected) setVisible(true);
      })
      .catch(() => {
        /* offline — skip the nudge */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const dismiss = () => {
    setVisible(false);
    try {
      localStorage.setItem(DISMISSED_KEY, "1");
    } catch {
      /* ignore */
    }
  };

  if (!visible) return null;

  return (
    <div className="mb-8 flex items-start gap-4 rounded-lg border border-primary/30 bg-primary/10 px-4 py-3">
      <Radio className="mt-0.5 h-5 w-5 flex-shrink-0 text-primary" />
      <div className="min-w-0 flex-1">
        <div className="font-semibold">Connect Last.fm for full stats</div>
        <p className="mt-0.5 text-sm text-muted-foreground">
          Every track you play here gets scrobbled, and your full listening
          history — across this app and any other player you've used — shows
          up on the Stats page. Tidal's "Rewind" only goes back one year;
          Last.fm keeps everything.
        </p>
        <Link
          to="/settings"
          className="mt-2 inline-block text-sm font-semibold text-primary hover:underline"
        >
          Open Settings →
        </Link>
      </div>
      <button
        type="button"
        onClick={dismiss}
        title="Dismiss"
        aria-label="Dismiss"
        className="flex h-7 w-7 items-center justify-center rounded text-muted-foreground hover:bg-primary/15 hover:text-foreground"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
