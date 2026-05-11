import { useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Info,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { api } from "@/api/client";
import type { SignalPath } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

/**
 * "Signal path" panel — shows what's actually happening to the bits
 * between the source decoder and the output. The audiophile
 * community calls this a "DSP chain" or "signal chain"; the panel
 * lets users confirm whether the audio is bit-perfect at a glance
 * and see exactly which stages are modifying samples when it isn't.
 *
 * Re-fetched on every open and on Refresh. Toggling a stage in
 * Settings while the dialog is open doesn't auto-refresh (would
 * require subscribing to settings updates) — the Refresh button
 * is the explicit escape hatch.
 */
export function SignalPathDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [path, setPath] = useState<SignalPath | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    api.player
      .signalPath()
      .then((p) => {
        if (!cancelled) setPath(p);
      })
      .catch(() => {
        if (!cancelled) setPath(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, refreshTick]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between gap-3">
            <span>Signal path</span>
            {path && (
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => setRefreshTick((n) => n + 1)}
                aria-label="Refresh signal path"
                title="Refresh"
              >
                <RefreshCw
                  className={cn("h-4 w-4", loading && "animate-spin")}
                />
              </Button>
            )}
          </DialogTitle>
          <DialogDescription>
            Every stage between the source decoder and your DAC.
          </DialogDescription>
        </DialogHeader>
        {loading && !path ? (
          <div className="flex items-center justify-center py-8 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : path ? (
          <SignalPathBody path={path} />
        ) : (
          <p className="py-4 text-sm text-muted-foreground">
            Couldn't read the signal path. The audio engine may not be running
            yet — start a track and reopen.
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}

function SignalPathBody({ path }: { path: SignalPath }) {
  return (
    <div className="flex flex-col gap-1.5">
      <TopBadge path={path} />

      {path.output.external_output_active && (
        <div className="mb-2 flex items-start gap-2 rounded-md border border-sky-500/40 bg-sky-500/10 p-3 text-xs text-sky-500">
          <Info className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            A remote receiver (Tidal Connect / DLNA) is the active sink. The DSP
            stages below don't run on the remote audio — the receiver gets the
            raw decoded stream and applies its own processing.
          </div>
        </div>
      )}

      <ChainStage title="Source" active detail={formatSource(path.source)} />
      <ChainStage
        title="ReplayGain"
        active={path.replaygain.active}
        detail={formatReplayGain(path.replaygain)}
      />
      <ChainStage
        title="EQ"
        active={path.eq.active}
        detail={formatEq(path.eq)}
      />
      <ChainStage
        title="Crossfeed"
        active={path.crossfeed.active}
        detail={
          path.crossfeed.active
            ? `${path.crossfeed.amount}% — Bauer 700 Hz`
            : "off"
        }
      />
      <ChainStage title="Output" active detail={formatOutput(path.output)} />
    </div>
  );
}

/**
 * Top-of-panel summary badge. Three states:
 *  - Idle: no track loaded; the panel is informational only.
 *  - Bit-perfect: track loaded, no DSP, exclusive output, no remote.
 *  - Modifying samples: track loaded, at least one stage active OR
 *    a non-exclusive output OR a remote receiver running.
 */
function TopBadge({ path }: { path: SignalPath }) {
  if (!path.track_loaded) {
    return (
      <div className="mb-3 flex items-center gap-2 rounded-md border border-border/50 bg-card/40 p-3 text-sm font-semibold text-muted-foreground">
        <Info className="h-5 w-5" />
        <div className="flex flex-col">
          <span>Idle</span>
          <span className="text-[10px] font-normal uppercase tracking-wider opacity-70">
            Play a track to see the active signal path
          </span>
        </div>
      </div>
    );
  }
  if (path.bit_perfect) {
    return (
      <div className="mb-3 flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 p-3 text-sm font-semibold text-primary">
        <CheckCircle2 className="h-5 w-5" />
        <div className="flex flex-col">
          <span>Bit-perfect</span>
          <span className="text-[10px] font-normal uppercase tracking-wider opacity-70">
            Source bits go to your DAC unchanged
          </span>
        </div>
      </div>
    );
  }
  return (
    <div className="mb-3 flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm font-semibold text-amber-500">
      <AlertCircle className="h-5 w-5" />
      <div className="flex flex-col">
        <span>Modifying samples</span>
        <span className="text-[10px] font-normal uppercase tracking-wider opacity-70">
          At least one stage below is touching the audio
        </span>
      </div>
    </div>
  );
}

function ChainStage({
  title,
  active,
  detail,
}: {
  title: string;
  active: boolean;
  detail: string;
}) {
  return (
    <div className="flex items-center justify-between rounded-md border border-border/40 bg-card/40 px-3 py-2 text-sm">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "inline-block h-2 w-2 rounded-full",
            active ? "bg-primary" : "bg-muted-foreground/30",
          )}
        />
        <span className="font-semibold">{title}</span>
      </div>
      <span className="text-xs text-muted-foreground">{detail}</span>
    </div>
  );
}

function formatSource(src: SignalPath["source"]): string {
  const parts: string[] = [];
  if (src.codec) parts.push(src.codec);
  if (src.sample_rate_hz)
    parts.push(`${(src.sample_rate_hz / 1000).toFixed(1)} kHz`);
  if (src.bit_depth) parts.push(`${src.bit_depth}-bit`);
  return parts.length > 0 ? parts.join(" · ") : "—";
}

function formatReplayGain(rg: SignalPath["replaygain"]): string {
  if (rg.mode === "off") return "off";
  if (!rg.tags_present) return `${rg.mode} mode — no tags on this track`;
  const sign = rg.applied_db >= 0 ? "+" : "";
  return `${rg.mode} mode — ${sign}${rg.applied_db.toFixed(1)} dB`;
}

function formatEq(eq: SignalPath["eq"]): string {
  if (eq.mode === "off") return "off";
  if (eq.bypass) return `${eq.mode} — bypassed`;
  if (eq.mode === "manual") {
    return eq.manual_enabled ? "manual — 10-band biquad" : "manual — disabled";
  }
  // profile mode
  return eq.profile_id ? `profile — ${eq.profile_id}` : "profile — none picked";
}

function formatOutput(out: SignalPath["output"]): string {
  // When a remote receiver is the active sink, the local output
  // stream is silenced — the device fields are still populated
  // but they describe a sink nothing is reaching. Call that out
  // explicitly so the readout doesn't misleadingly imply audio is
  // playing locally.
  if (out.external_output_active) {
    return "remote receiver (local silenced)";
  }
  const parts: string[] = [];
  if (out.device_name) parts.push(out.device_name);
  const format: string[] = [];
  if (out.sample_rate_hz)
    format.push(`${(out.sample_rate_hz / 1000).toFixed(1)} kHz`);
  if (out.bit_depth) format.push(`${out.bit_depth}-bit`);
  if (format.length > 0) parts.push(format.join("/"));
  parts.push(out.exclusive_mode ? "exclusive" : "shared (OS mixer)");
  if (out.force_volume) parts.push("forced 100% vol");
  return parts.join(" · ");
}
