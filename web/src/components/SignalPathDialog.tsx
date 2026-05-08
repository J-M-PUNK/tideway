import { useEffect, useState } from "react";
import { CheckCircle2, Circle, Loader2 } from "lucide-react";
import { api } from "@/api/client";
import type { SignalPath } from "@/api/types";
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
 * Re-fetched on every open so the readout reflects the current
 * track + the latest user toggles. Static-after-open — the panel
 * is informational, not interactive (toggles still live in
 * Settings).
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
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Signal path</DialogTitle>
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
      <BitPerfectBadge bitPerfect={path.bit_perfect} />

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

function BitPerfectBadge({ bitPerfect }: { bitPerfect: boolean }) {
  return (
    <div
      className={cn(
        "mb-3 flex items-center gap-2 rounded-md border p-3 text-sm font-semibold",
        bitPerfect
          ? "border-primary/40 bg-primary/10 text-primary"
          : "border-amber-500/40 bg-amber-500/10 text-amber-500",
      )}
    >
      {bitPerfect ? (
        <CheckCircle2 className="h-5 w-5" />
      ) : (
        <Circle className="h-5 w-5" />
      )}
      <div className="flex flex-col">
        <span>{bitPerfect ? "Bit-perfect" : "Modifying samples"}</span>
        <span className="text-[10px] font-normal uppercase tracking-wider opacity-70">
          {bitPerfect
            ? "Source bits go to your DAC unchanged"
            : "At least one stage below is touching the audio"}
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
  const parts: string[] = [];
  parts.push(out.exclusive_mode ? "exclusive" : "shared (OS mixer)");
  if (out.force_volume) parts.push("forced 100% vol");
  return parts.join(" · ");
}
