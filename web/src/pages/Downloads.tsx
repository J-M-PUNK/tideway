import { useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  Download,
  FolderOpen,
  HardDrive,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import type { DownloadItem, VideoDownloadJob } from "@/api/types";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { AddUrlDialog } from "@/components/AddUrlDialog";
import { EmptyState } from "@/components/EmptyState";
import { useToast } from "@/components/toast";
import { useQualities } from "@/hooks/useQualities";
import { useVideoDownloads } from "@/hooks/useVideoDownloads";
import { cn } from "@/lib/utils";

export function Downloads({
  items,
  offline = false,
}: {
  items: DownloadItem[];
  /** True when the app is running without a live Tidal session. Retry
   *  would inevitably 401, so we hide it to avoid false affordances. */
  offline?: boolean;
}) {
  const toast = useToast();
  // Single pass over the list instead of three filters per render.
  const { active, terminal, failed } = useMemo(() => {
    const a: DownloadItem[] = [];
    const t: DownloadItem[] = [];
    const f: DownloadItem[] = [];
    for (const i of items) {
      if (i.status === "Complete" || i.status === "Failed") t.push(i);
      else a.push(i);
      if (i.status === "Failed") f.push(i);
    }
    return { active: a, terminal: t, failed: f };
  }, [items]);

  const retryAll = async () => {
    // Sequential, not parallel — retries trigger stream-URL lookups on
    // Tidal, and firing N simultaneous requests trips their rate limit.
    let succeeded = 0;
    let lastError: unknown = null;
    for (const item of failed) {
      try {
        await api.downloads.retry(item.id);
        succeeded += 1;
      } catch (err) {
        lastError = err;
      }
    }
    if (succeeded === 0 && failed.length > 0) {
      toast.show({
        kind: "error",
        title: "Retries failed",
        description:
          lastError instanceof Error
            ? lastError.message
            : "Couldn't re-queue any items.",
      });
      return;
    }
    const failedCount = failed.length - succeeded;
    toast.show({
      kind: failedCount > 0 ? "info" : "success",
      title: `Re-queued ${succeeded} download${succeeded === 1 ? "" : "s"}`,
      description:
        failedCount > 0 ? `${failedCount} couldn't be re-queued.` : undefined,
    });
  };

  const clearDone = async () => {
    try {
      await api.downloads.clearCompleted();
      toast.show({ kind: "info", title: "Cleared finished downloads" });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't clear downloads",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const cancel = async (item: DownloadItem) => {
    try {
      await api.downloads.cancel(item.id);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't cancel",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const cancelAll = async () => {
    try {
      const res = await api.downloads.cancelAll();
      if (res.cancelled > 0) {
        toast.show({
          kind: "info",
          title: `Cancelled ${res.cancelled} download${res.cancelled === 1 ? "" : "s"}`,
        });
      }
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't cancel downloads",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const reveal = async (item: DownloadItem) => {
    if (!item.file_path) return;
    try {
      await api.downloads.reveal(item.file_path);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't show file",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const retry = async (item: DownloadItem, quality?: string) => {
    try {
      await api.downloads.retry(item.id, quality);
      toast.show({
        kind: "info",
        title: "Retrying download",
        description: quality ? `At ${quality}` : undefined,
      });
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Retry failed",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
            <Download className="h-7 w-7" /> Downloads
          </h1>
          <DiskUsage terminalCount={terminal.length} />
        </div>
        <div className="flex items-center gap-2">
          <AddUrlDialog />
          <PauseToggle activeCount={active.length} />
          {active.length > 0 && (
            <Button variant="secondary" onClick={cancelAll}>
              <X className="h-4 w-4" /> Cancel all
            </Button>
          )}
          {failed.length > 0 && !offline && (
            <Button variant="secondary" onClick={retryAll}>
              <RefreshCw className="h-4 w-4" /> Retry failed
            </Button>
          )}
          {terminal.length > 0 && (
            <Button variant="secondary" onClick={clearDone}>
              <Trash2 className="h-4 w-4" /> Clear finished
            </Button>
          )}
        </div>
      </div>

      <MusicVideoSections
        active={active}
        terminal={terminal}
        offline={offline}
        onRetry={retry}
        onReveal={reveal}
        onCancel={cancel}
      />
    </div>
  );
}

/**
 * Music + Videos in parallel sections. Each section has its own
 * In Progress / Finished subheadings and uses the same row grid so
 * the two surfaces read as peers rather than "music + a weird
 * bolt-on for videos." Empty state only fires when BOTH are empty,
 * so a user with only video jobs doesn't see "No downloads yet."
 */
function MusicVideoSections({
  active,
  terminal,
  offline,
  onRetry,
  onReveal,
  onCancel,
}: {
  active: DownloadItem[];
  terminal: DownloadItem[];
  offline: boolean;
  onRetry: (i: DownloadItem, quality?: string) => void;
  onReveal: (i: DownloadItem) => void;
  onCancel: (i: DownloadItem) => void;
}) {
  // Read from the shared provider rather than polling here — the
  // sidebar also subscribes, and letting both components fan out from
  // a single interval keeps the network chatter deterministic.
  const { active: videoActive, terminal: videoTerminal } = useVideoDownloads();
  const hasMusic = active.length > 0 || terminal.length > 0;
  const hasVideos = videoActive.length > 0 || videoTerminal.length > 0;

  if (!hasMusic && !hasVideos) {
    return (
      <EmptyState
        icon={Download}
        title="No downloads yet"
        description="Browse your library or search, then hit the download button on any album, playlist, or track. You can also paste a Tidal URL."
        action={<AddUrlDialog />}
      />
    );
  }

  return (
    <div className="flex flex-col gap-8">
      {hasMusic && (
        <section>
          <h2 className="mb-3 text-lg font-bold tracking-tight">Music</h2>
          {active.length > 0 && (
            <>
              <SubHeader>In progress</SubHeader>
              <div className="flex flex-col gap-2">
                {active.map((item) => (
                  <Row
                    key={item.id}
                    item={item}
                    onRetry={onRetry}
                    onReveal={onReveal}
                    onCancel={onCancel}
                    offline={offline}
                  />
                ))}
              </div>
            </>
          )}
          {terminal.length > 0 && (
            <>
              <SubHeader className={active.length > 0 ? "mt-6" : ""}>
                Finished
              </SubHeader>
              <div className="flex flex-col gap-2">
                {terminal.map((item) => (
                  <Row
                    key={item.id}
                    item={item}
                    onRetry={onRetry}
                    onReveal={onReveal}
                    onCancel={onCancel}
                    offline={offline}
                  />
                ))}
              </div>
            </>
          )}
        </section>
      )}

      {hasVideos && (
        <section>
          <h2 className="mb-3 text-lg font-bold tracking-tight">Videos</h2>
          {videoActive.length > 0 && (
            <>
              <SubHeader>In progress</SubHeader>
              <div className="flex flex-col gap-2">
                {videoActive.map((j) => (
                  <VideoRow key={j.video_id} job={j} />
                ))}
              </div>
            </>
          )}
          {videoTerminal.length > 0 && (
            <>
              <SubHeader className={videoActive.length > 0 ? "mt-6" : ""}>
                Finished
              </SubHeader>
              <div className="flex flex-col gap-2">
                {videoTerminal.map((j) => (
                  <VideoRow key={j.video_id} job={j} />
                ))}
              </div>
            </>
          )}
        </section>
      )}
    </div>
  );
}

function SubHeader({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <h3
      className={cn(
        "mb-2 text-sm font-semibold uppercase tracking-wider text-muted-foreground",
        className,
      )}
    >
      {children}
    </h3>
  );
}

/**
 * Video download row. Grid layout, status column, and Show button
 * mirror the song `Row` so the two surfaces look like peers. Column
 * 3 (the "album" slot on songs) is empty for videos — they have no
 * album concept, and leaving it blank preserves column alignment
 * without inventing a fake field to fill the space.
 */
function VideoRow({ job }: { job: VideoDownloadJob }) {
  const failed = job.state === "error";
  const done = job.state === "done";
  const active = !failed && !done;
  const pct = job.progress != null ? Math.round(job.progress * 100) : 0;
  const title = job.title || `Video ${job.video_id}`;
  const statusLabel = done ? "Complete" : failed ? "Failed" : "Downloading";
  return (
    <div
      className={cn(
        "grid grid-cols-[32px_4fr_3fr_140px_auto] items-center gap-4 rounded-md border border-border/50 bg-card px-4 py-3",
        failed && "border-destructive/40",
      )}
    >
      <div>
        {failed ? (
          <XCircle className="h-5 w-5 text-destructive" />
        ) : done ? (
          <CheckCircle2 className="h-5 w-5 text-primary" />
        ) : (
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate font-medium">{title}</div>
        <div className="truncate text-xs text-muted-foreground">
          {job.artist || ""}
        </div>
      </div>
      <div className="truncate text-sm text-muted-foreground">
        {/* Songs show album here; videos have no album concept. Leave
            the column empty rather than shoehorn a substitute — the
            grid stays aligned with the music rows either way. */}
      </div>
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>{statusLabel}</span>
          {active && job.progress != null && <span>{pct}%</span>}
        </div>
        {active && job.progress != null && <Progress value={pct} />}
        {failed && job.error && (
          <div className="truncate text-xs text-destructive">{job.error}</div>
        )}
      </div>
      <div className="flex items-center justify-end gap-2">
        {done && job.output_path && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              job.output_path && api.revealInFinder(job.output_path)
            }
            title="Show in Finder"
          >
            <FolderOpen className="h-4 w-4" /> Show
          </Button>
        )}
      </div>
    </div>
  );
}

function Row({
  item,
  onRetry,
  onReveal,
  onCancel,
  offline = false,
}: {
  item: DownloadItem;
  onRetry: (i: DownloadItem, quality?: string) => void;
  onReveal: (i: DownloadItem) => void;
  onCancel: (i: DownloadItem) => void;
  offline?: boolean;
}) {
  const failed = item.status === "Failed";
  const done = item.status === "Complete";
  const active = !failed && !done;
  const pct = Math.round(item.progress * 100);

  return (
    <div
      className={cn(
        "grid grid-cols-[32px_4fr_3fr_140px_auto] items-center gap-4 rounded-md border border-border/50 bg-card px-4 py-3",
        failed && "border-destructive/40",
      )}
    >
      <div>
        {failed ? (
          <XCircle className="h-5 w-5 text-destructive" />
        ) : done ? (
          <CheckCircle2 className="h-5 w-5 text-primary" />
        ) : (
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate font-medium">{item.title}</div>
        <div className="truncate text-xs text-muted-foreground">
          {item.artist}
        </div>
      </div>
      <div className="truncate text-sm text-muted-foreground">{item.album}</div>
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            {item.status}
            {item.status === "Downloading" &&
            item.speed_bps &&
            item.speed_bps > 0 ? (
              <span className="ml-1.5 text-foreground tabular-nums">
                {formatSpeed(item.speed_bps)}
              </span>
            ) : null}
          </span>
          {!done && !failed && <span>{pct}%</span>}
        </div>
        {!done && !failed && <Progress value={pct} />}
        {failed && item.error && (
          <div className="truncate text-xs text-destructive">{item.error}</div>
        )}
      </div>
      <div className="flex items-center justify-end gap-2">
        {failed && !offline && (
          <RetryButton onRetry={(q) => onRetry(item, q)} />
        )}
        {done && item.file_path && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onReveal(item)}
            title="Show in Finder"
          >
            <FolderOpen className="h-4 w-4" /> Show
          </Button>
        )}
        {active && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onCancel(item)}
            title="Cancel download"
            aria-label="Cancel download"
          >
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * Inline disk-usage summary under the Downloads heading. Walks the
 * output directory server-side (scandir; fast even for large libraries)
 * and refreshes whenever a new item reaches a terminal state — so the
 * number grows visibly as the queue drains.
 */
function DiskUsage({ terminalCount }: { terminalCount: number }) {
  const [stats, setStats] = useState<{
    total_bytes: number;
    file_count: number;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.downloads
      .stats()
      .then((s) => {
        if (!cancelled) setStats(s);
      })
      .catch(() => {
        /* best-effort — if the folder doesn't exist yet just hide the widget */
      });
    return () => {
      cancelled = true;
    };
  }, [terminalCount]);

  if (!stats || stats.file_count === 0) return null;
  return (
    <p className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground">
      <HardDrive className="h-3.5 w-3.5" />
      {formatBytes(stats.total_bytes)} · {stats.file_count.toLocaleString()}{" "}
      file
      {stats.file_count === 1 ? "" : "s"}
    </p>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/**
 * Format a bytes-per-second value for the in-progress download row.
 * Picks the unit so the number stays in the 1-999 range — KB/s for
 * trickle, MB/s for normal, GB/s for the unrealistic case. Single
 * decimal place under 10, integer otherwise, so the value doesn't
 * jitter visibly between consecutive updates.
 */
function formatSpeed(bps: number): string {
  if (!isFinite(bps) || bps <= 0) return "0 KB/s";
  const units: { divisor: number; label: string }[] = [
    { divisor: 1024 ** 3, label: "GB/s" },
    { divisor: 1024 ** 2, label: "MB/s" },
    { divisor: 1024, label: "KB/s" },
  ];
  for (const u of units) {
    if (bps >= u.divisor) {
      const v = bps / u.divisor;
      return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${u.label}`;
    }
  }
  return `${Math.round(bps)} B/s`;
}

/**
 * Global pause/resume for the worker pool. Hidden when there's nothing
 * in flight — pausing an empty queue just causes confusion. State is
 * pulled from the server on mount so a reload doesn't reset the button
 * to the wrong icon while the backend is still paused.
 */
function PauseToggle({ activeCount }: { activeCount: number }) {
  const toast = useToast();
  const [paused, setPaused] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.downloads
      .state()
      .then((s) => {
        if (!cancelled) setPaused(s.paused);
      })
      .catch(() => {
        /* best-effort */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (paused === null) return null;
  if (!paused && activeCount === 0) return null;

  const toggle = async () => {
    try {
      if (paused) {
        await api.downloads.resume();
        setPaused(false);
        toast.show({ kind: "info", title: "Downloads resumed" });
      } else {
        await api.downloads.pause();
        setPaused(true);
        toast.show({ kind: "info", title: "Downloads paused" });
      }
    } catch (err) {
      toast.show({
        kind: "error",
        title: paused ? "Couldn't resume" : "Couldn't pause",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <Button variant="secondary" onClick={toggle}>
      {paused ? (
        <>
          <Play className="h-4 w-4" /> Resume
        </>
      ) : (
        <>
          <Pause className="h-4 w-4" /> Pause
        </>
      )}
    </Button>
  );
}

/**
 * Retry button with an optional quality picker. A plain click retries at
 * the same quality the item was originally queued at; the dropdown lets
 * the user step down (or up) a tier — useful when a hi-res download
 * failed because the account doesn't support it.
 */
function RetryButton({ onRetry }: { onRetry: (quality?: string) => void }) {
  // Shared across DownloadButton + every RetryButton on the page — one
  // request even with 20 failed rows in view.
  const qualities = useQualities() ?? [];

  return (
    <div className="flex">
      <Button
        size="sm"
        variant="secondary"
        onClick={() => onRetry()}
        className="rounded-r-none"
      >
        Retry
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            size="sm"
            variant="secondary"
            className="rounded-l-none border-l border-border/50 px-2"
            aria-label="Retry at different quality"
          >
            <ChevronDown className="h-3 w-3" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuLabel>Retry at quality</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {qualities.map((q) => (
            <DropdownMenuItem key={q.value} onSelect={() => onRetry(q.value)}>
              <div className="flex flex-col">
                <span className="font-semibold">
                  {q.label} — {q.codec}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  {q.bitrate}
                </span>
              </div>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
