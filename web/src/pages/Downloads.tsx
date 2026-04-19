import { useMemo } from "react";
import { CheckCircle2, Download, FolderOpen, Loader2, RefreshCw, Trash2, XCircle } from "lucide-react";
import type { DownloadItem } from "@/api/types";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { AddUrlDialog } from "@/components/AddUrlDialog";
import { EmptyState } from "@/components/EmptyState";
import { useToast } from "@/components/toast";
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
    await Promise.allSettled(failed.map((i) => api.downloads.retry(i.id)));
    toast.show({ kind: "info", title: `Re-queued ${failed.length} downloads` });
  };

  const clearDone = async () => {
    await api.downloads.clearCompleted();
    toast.show({ kind: "info", title: "Cleared finished downloads" });
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

  const retry = async (item: DownloadItem) => {
    try {
      await api.downloads.retry(item.id);
      toast.show({ kind: "info", title: "Retrying download" });
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
  onRetry: (i: DownloadItem) => void;
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
        {failed && (
          <Button size="sm" variant="secondary" onClick={() => onRetry(item)}>
            Retry
          </Button>
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
      </div>
    </div>
  );
}
