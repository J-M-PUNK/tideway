import { useState } from "react";
import { Check, Download } from "lucide-react";
import type { OnDownload } from "@/api/download";
import type { Track } from "@/api/types";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useDownloadedIds } from "@/hooks/useDownloadedSet";
import { useQualities } from "@/hooks/useQualities";
import {
  DOWNLOAD_GATE_TOOLTIP,
  useSubscription,
} from "@/hooks/useSubscription";
import { effectiveFormatLabel } from "@/lib/quality";
import { cn } from "@/lib/utils";

/**
 * Album-level download button for a detail-page actions row. Visually
 * matches AddToLibraryButton + ShareButton (icon above, small text
 * below). Click opens the same quality picker the track-row download
 * buttons use; selection forwards to onDownload(kind, id, quality)
 * which enqueues via /api/downloads/enqueue. When every track on the
 * album is already present locally the label flips to "Downloaded"
 * with a check icon, but the menu is still openable for re-download.
 */
export function DownloadAlbumButton({
  albumId,
  tracks,
  mediaTags,
  onDownload,
}: {
  albumId: string;
  tracks: Track[];
  mediaTags?: string[];
  onDownload: OnDownload;
}) {
  const downloaded = useDownloadedIds();
  const qualities = useQualities() ?? [];
  const sub = useSubscription();
  const [open, setOpen] = useState(false);

  const have = tracks.filter((t) => downloaded.has(String(t.id))).length;
  const total = tracks.length;
  const allHave = total > 0 && have === total;

  const Icon = allHave ? Check : Download;
  const label = allHave ? "Downloaded" : "Download";

  if (!sub.canDownload) {
    return (
      <button
        disabled
        className="flex cursor-not-allowed flex-col items-center gap-1 text-muted-foreground opacity-50"
        title={sub.reason ?? DOWNLOAD_GATE_TOOLTIP}
      >
        <Download className="h-5 w-5" />
        <div className="text-xs font-semibold">Download</div>
      </button>
    );
  }

  const stop = (e: React.SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  // When the album is fully downloaded, only the check ICON tints
  // sky blue — the "Downloaded" label stays in the same muted tone
  // the unhealthy / not-yet-downloaded "Download" label uses, so
  // the row of action buttons (Add to library, Download, Credits,
  // Share, More) still reads as a uniform set with the check as
  // the only emphatic element. text-sky-500 mirrors the high-
  // lossless badge in NowPlaying, which is the codebase's
  // established blue.
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild onClick={stop}>
        <button
          className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground data-[state=open]:text-primary"
          title={allHave ? "Re-download album" : "Download album"}
          aria-label={allHave ? "Re-download album" : "Download album"}
        >
          <Icon className={cn("h-5 w-5", allHave && "text-sky-500")} />
          <div className="text-xs font-semibold">{label}</div>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        onClick={stop}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <DropdownMenuLabel>Download quality</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {qualities.map((q) => {
          const effective = effectiveFormatLabel(q.value, mediaTags);
          return (
            <DropdownMenuItem
              key={q.value}
              onSelect={() => onDownload("album", albumId, q.value)}
            >
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{q.label}</span>
                  <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    {q.codec}
                  </span>
                  {effective && (
                    <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-primary">
                      {effective}
                    </span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">{q.bitrate}</div>
                <div className="text-[11px] text-muted-foreground/70">
                  {q.description}
                </div>
              </div>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
