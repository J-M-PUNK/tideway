import { Link } from "react-router-dom";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import type { DownloadItem } from "@/api/types";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";

export function DownloadDock({
  items,
  activeCount,
}: {
  items: DownloadItem[];
  activeCount: number;
}) {
  if (items.length === 0) return null;
  const recent = items.slice(0, 4);

  return (
    <div className="pointer-events-auto border-t border-border bg-black/80 px-6 py-3 backdrop-blur-sm">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {activeCount > 0 ? `Downloading · ${activeCount}` : "Recently downloaded"}
          </div>
          <Link
            to="/downloads"
            className="text-xs font-semibold text-muted-foreground hover:text-foreground"
          >
            Show all
          </Link>
        </div>
      </div>
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-4">
        {recent.map((item) => (
          <DockRow key={item.id} item={item} />
        ))}
      </div>
    </div>
  );
}

function DockRow({ item }: { item: DownloadItem }) {
  const failed = item.status === "Failed";
  const done = item.status === "Complete";
  const pct = Math.round(item.progress * 100);

  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-md border border-border/50 bg-card/80 px-3 py-2",
        failed && "border-destructive/40",
      )}
    >
      <div className="flex items-center gap-2">
        {failed ? (
          <XCircle className="h-3.5 w-3.5 text-destructive" />
        ) : done ? (
          <CheckCircle2 className="h-3.5 w-3.5 text-primary" />
        ) : (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
        )}
        <div className="flex-1 truncate text-xs font-semibold">{item.title}</div>
        <div className="text-[10px] text-muted-foreground">{item.status}</div>
      </div>
      <div className="truncate text-[11px] text-muted-foreground">
        {item.artist}
        {item.album ? ` · ${item.album}` : ""}
      </div>
      {!done && !failed && <Progress value={pct} className="h-1" />}
      {failed && item.error && (
        <div className="truncate text-[10px] text-destructive">{item.error}</div>
      )}
    </div>
  );
}
