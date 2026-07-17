import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Sparkles } from "lucide-react";
import { api } from "@/api/client";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/**
 * One-time Home-screen notice announcing that Tideway now hides
 * AI-generated tracks by default. Shown once to existing installs
 * upgrading into the change; fresh installs never see it because
 * load_settings() marks the acknowledgment true for a new settings
 * file (see app/settings.py).
 *
 * Visibility is driven by the server-side `ai_filter_notice_ack` flag
 * rather than localStorage, because only the backend can tell an
 * upgrade (settings.json already on disk) from a fresh install. Any
 * way of closing the dialog acknowledges it; the two buttons also let
 * the user opt out of the filter right here without a trip to Settings.
 */
export function AiFilterNotice() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.settings
      .get()
      .then((s) => {
        if (cancelled) return;
        if (!s.ai_filter_notice_ack) setOpen(true);
      })
      .catch(() => {
        /* offline / settings unavailable — skip the notice */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const acknowledge = (extra?: { hide_ai_content: boolean }) => {
    setOpen(false);
    // Fire-and-forget: the dialog is dismissed optimistically. If the
    // PUT fails the notice simply reappears next time Home mounts,
    // which is the safe direction for a "have you seen this" flag.
    void api.settings
      .put({ ai_filter_notice_ack: true, ...extra })
      .catch(() => {
        /* leave unacknowledged; it'll show again next launch */
      });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Closing by any means (X, escape, overlay) counts as seen.
        if (!next) acknowledge();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-primary" />
            AI-generated music is now hidden
          </DialogTitle>
          <DialogDescription>What changed in this update</DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-sm text-muted-foreground">
          <p>
            Tidal tags tracks it identifies as 100% AI-generated. Tideway now
            hides those tracks by default across search, artist and album pages,
            mixes, and recommendations, and skips them when downloading.
          </p>
          <p>
            Your library is untouched, so anything you&apos;ve already favorited
            still shows. This is a local setting on this install and you can
            change it anytime in{" "}
            <Link
              to="/settings"
              className="text-primary underline underline-offset-2"
              onClick={() => acknowledge()}
            >
              Settings → Playback
            </Link>
            .
          </p>
        </div>
        <div className="mt-2 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button
            variant="outline"
            onClick={() => acknowledge({ hide_ai_content: false })}
          >
            Allow AI content
          </Button>
          <Button onClick={() => acknowledge()}>Keep it hidden</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
