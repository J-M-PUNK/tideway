import { useMemo } from "react";
import { CheckCircle2, ChevronDown, Download, FolderOpen, Loader2, RefreshCw, Trash2, XCircle } from "lucide-react";
import type { DownloadItem } from "@/api/types";
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
import { cn } from "@/lib/utils";

export function Downloads({ items }: { items: DownloadItem[] }) {
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
          lastError instanceof Error ? lastError.message : "Couldn't re-queue any items.",
      });
      return;
    }
    const failedCount = failed.length - succeeded;
    toast.show({
      kind: failedCount > 0 ? "info" : "success",
      title: `Re-queued ${succeeded} download${succeeded === 1 ? "" : "s"}`,
      description: failedCount > 0 ? `${failedCount} couldn't be re-queued.` : undefined,
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
        <h1 className="flex items-center gap-3 text-3xl font-bold tracking-tight">
          <Download className="h-7 w-7" /> Downloads
        </h1>
        <div className="flex items-center gap-2">
          <AddUrlDialog />
          {failed.length > 0 && (
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

      {items.length === 0 && (
        <EmptyState
          icon={Download}
          title="No downloads yet"
          description="Browse your library or search, then hit the download button on any album, playlist, or track. You can also paste a Tidal URL."
          action={<AddUrlDialog />}
        />
      )}

      {active.length > 0 && (
        <>
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wider text-muted-foreground">
            In progress
          </h2>
          <div className="flex flex-col gap-2">
            {active.map((item) => (
              <Row key={item.id} item={item} onRetry={retry} onReveal={reveal} />
            ))}
          </div>
        </>
      )}

      {terminal.length > 0 && (
        <>
          <h2 className="mb-2 mt-8 text-sm font-semibold uppercase tracking-wider text-muted-foreground">
            Finished
          </h2>
          <div className="flex flex-col gap-2">
            {terminal.map((item) => (
              <Row key={item.id} item={item} onRetry={retry} onReveal={reveal} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function Row({
  item,
  onRetry,
  onReveal,
}: {
  item: DownloadItem;
  onRetry: (i: DownloadItem, quality?: string) => void;
  onReveal: (i: DownloadItem) => void;
}) {
  const failed = item.status === "Failed";
  const done = item.status === "Complete";
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
        <div className="truncate text-xs text-muted-foreground">{item.artist}</div>
      </div>
      <div className="truncate text-sm text-muted-foreground">{item.album}</div>
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>{item.status}</span>
          {!done && !failed && <span>{pct}%</span>}
        </div>
        {!done && !failed && <Progress value={pct} />}
        {failed && item.error && (
          <div className="truncate text-xs text-destructive">{item.error}</div>
        )}
      </div>
      <div className="flex items-center justify-end gap-2">
        {failed && <RetryButton onRetry={(q) => onRetry(item, q)} />}
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
      </div>
    </div>
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
                <span className="text-[11px] text-muted-foreground">{q.bitrate}</span>
              </div>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
