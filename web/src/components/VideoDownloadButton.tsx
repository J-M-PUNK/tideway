import { useEffect, useRef, useState } from "react";
import { Check, Download, Loader2, AlertTriangle } from "lucide-react";
import { api } from "@/api/client";
import type { VideoDownloadJob } from "@/api/types";
import { useToast } from "@/components/toast";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type VideoQuality = "HIGH" | "MEDIUM" | "LOW";

const QUALITY_OPTIONS: { value: VideoQuality; label: string; hint: string }[] =
  [
    {
      value: "HIGH",
      label: "High",
      hint: "1080p when available — largest file",
    },
    { value: "MEDIUM", label: "Medium", hint: "720p — good balance" },
    { value: "LOW", label: "Low", hint: "480p — smallest file" },
  ];

/**
 * Starts an HLS → MP4 remux on the backend. Click opens a
 * quality picker (HIGH / MEDIUM / LOW) before starting — Tidal
 * serves three variants and letting the user pick avoids always
 * hitting "worst" or "largest".
 *
 * While running the button renders a circular progress ring with
 * inline percent; when idle / done / error it's a compact icon.
 * On completion a toast fires with the output path + a "Reveal"
 * button so users can see exactly where their file landed.
 */
export function VideoDownloadButton({
  videoId,
  className,
}: {
  videoId: string;
  className?: string;
}) {
  const toast = useToast();
  const [job, setJob] = useState<VideoDownloadJob | null>(null);
  const prevStateRef = useRef<VideoDownloadJob["state"] | null>(null);

  // One-shot fetch on mount so the button reflects a download in
  // progress if the user reopened the modal for a video they already
  // started downloading.
  useEffect(() => {
    let cancelled = false;
    api
      .videoDownloadStatus(videoId)
      .then((j) => {
        if (!cancelled) setJob(j);
      })
      .catch(() => {
        /* no job yet — state stays null which renders as idle */
      });
    return () => {
      cancelled = true;
    };
  }, [videoId]);

  // Poll while running. Stops once we hit a terminal state.
  useEffect(() => {
    if (job?.state !== "running") return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const next = await api.videoDownloadStatus(videoId);
        if (!cancelled) setJob(next);
      } catch {
        /* transient — keep polling */
      }
    };
    const interval = window.setInterval(tick, 500);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [job?.state, videoId]);

  // Transition-triggered toasts. Compare prior state so a reopen of
  // the modal on a finished job doesn't fire a fresh toast.
  useEffect(() => {
    const prev = prevStateRef.current;
    prevStateRef.current = job?.state ?? null;
    if (!job) return;
    if (prev === "running" && job.state === "done" && job.output_path) {
      toast.show({
        kind: "success",
        title: `Saved "${job.title || "video"}"`,
        description: job.output_path,
        action: {
          label: "Reveal",
          onClick: () => {
            if (job.output_path) api.revealInFinder(job.output_path);
          },
        },
      });
    } else if (prev === "running" && job.state === "error" && job.error) {
      toast.show({
        kind: "error",
        title: "Video download failed",
        description: job.error,
      });
    }
  }, [job, toast]);

  const state = job?.state ?? "idle";

  const startWithQuality = async (quality: VideoQuality) => {
    try {
      const next = await api.videoDownloadStart(videoId, quality);
      setJob(next);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't start video download",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const pct = job?.progress != null ? Math.round(job.progress * 100) : null;

  // Running: circular progress ring + inline percent. The ring fills
  // as the backend remux reports progress; when progress is null
  // (right at the start, before the first packet lands) the ring
  // stays empty and we render the spinner inside so the affordance
  // is still obviously "doing something".
  if (state === "running") {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={cn(
              "relative flex h-9 w-9 items-center justify-center",
              className,
            )}
            aria-label={
              pct != null ? `Downloading video — ${pct}%` : "Downloading video"
            }
          >
            <ProgressRing fraction={job?.progress ?? null} />
            <span className="relative z-10 text-[9px] font-bold tabular-nums text-foreground">
              {pct != null ? `${pct}%` : ""}
            </span>
            {pct == null && (
              <Loader2 className="absolute h-3.5 w-3.5 animate-spin text-muted-foreground" />
            )}
          </span>
        </TooltipTrigger>
        <TooltipContent align="center" className="max-w-xs">
          <div className="flex flex-col gap-0.5">
            <span className="font-semibold">
              Downloading video{pct != null ? ` — ${pct}%` : "…"}
            </span>
            {job?.title && <span>{job.title}</span>}
          </div>
        </TooltipContent>
      </Tooltip>
    );
  }

  // Done → click reveals in Finder (re-downloading on accident is
  // worse than letting the user re-open via quality dropdown if they
  // really want a new copy).
  if (state === "done") {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={() => {
              if (job?.output_path) api.revealInFinder(job.output_path);
            }}
            aria-label="Downloaded — click to show in Finder"
            className={cn(
              "flex h-9 w-9 items-center justify-center rounded-full text-primary transition-colors hover:text-primary",
              className,
            )}
          >
            <Check className="h-4 w-4" />
          </button>
        </TooltipTrigger>
        <TooltipContent align="center" className="max-w-xs">
          <div className="flex flex-col gap-0.5">
            <span className="font-semibold">
              Downloaded — click to show in Finder
            </span>
            {job?.output_path && (
              <span className="break-all text-muted-foreground">
                {job.output_path}
              </span>
            )}
          </div>
        </TooltipContent>
      </Tooltip>
    );
  }

  // Idle or error — open a dropdown to pick quality and start.
  // Deliberately NOT wrapped in Tooltip: nesting Radix TooltipTrigger
  // (asChild) around DropdownMenuTrigger (asChild) makes Radix's
  // Slot composition swallow the click — both wrappers try to clone
  // the same child and the event handler is lost. The dropdown
  // content itself labels what it does; a plain `title` covers the
  // error-retry case for users who hover without clicking.
  const iconEl =
    state === "error" ? (
      <AlertTriangle className="h-4 w-4 text-amber-300" />
    ) : (
      <Download className="h-4 w-4" />
    );
  const label =
    state === "error"
      ? job?.error || "Video download failed — pick a quality to retry"
      : "Download video";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={label}
          title={label}
          className={cn(
            "flex h-9 w-9 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-foreground data-[state=open]:text-primary",
            className,
          )}
        >
          {iconEl}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="z-[70] w-56">
        {/* z-[70] beats VideoPlayerModal's z-[60] shell + z-[65] siblings.
            Without the override this menu opens behind the modal and
            every click on it hits the modal instead. */}
        <DropdownMenuLabel>Download quality</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {QUALITY_OPTIONS.map((q) => (
          <DropdownMenuItem
            key={q.value}
            onSelect={() => startWithQuality(q.value)}
          >
            <div className="flex min-w-0 flex-1 flex-col">
              <span className="font-semibold">{q.label}</span>
              <span className="text-[11px] text-muted-foreground">
                {q.hint}
              </span>
            </div>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * 32×32 circular progress ring. Renders an empty track + a filled
 * arc whose length tracks `fraction`. Uses stroke-dasharray for the
 * fill, which is pixel-exact and has no layout cost. When fraction
 * is null we render just the track (the spinner in the parent fills
 * the indeterminate-state UX).
 */
function ProgressRing({ fraction }: { fraction: number | null }) {
  // r chosen so stroke of 2 fits comfortably in a 32x32 viewBox.
  const r = 13;
  const c = 2 * Math.PI * r;
  const dash = fraction != null ? c * Math.min(1, Math.max(0, fraction)) : 0;
  return (
    <svg
      viewBox="0 0 32 32"
      className="absolute inset-0 h-full w-full -rotate-90"
      aria-hidden="true"
    >
      <circle
        cx="16"
        cy="16"
        r={r}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        className="text-muted-foreground/25"
      />
      {fraction != null && fraction > 0 && (
        <circle
          cx="16"
          cy="16"
          r={r}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${c}`}
          className="text-primary transition-[stroke-dasharray] duration-300 ease-out"
        />
      )}
    </svg>
  );
}
