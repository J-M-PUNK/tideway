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
import { effectiveFormatLabel } from "@/lib/quality";

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
  const [open, setOpen] = useState(false);

  const have = tracks.filter((t) => downloaded.has(String(t.id))).length;
  const total = tracks.length;
  const allHave = total > 0 && have === total;

  const Icon = allHave ? Check : Download;
  const label = allHave ? "Downloaded" : "Download";

  const stop = (e: React.SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild onClick={stop}>
        <button
          className="flex flex-col items-center gap-1 text-muted-foreground transition-colors hover:text-foreground data-[state=open]:text-primary"
          title={allHave ? "Re-download album" : "Download album"}
          aria-label={allHave ? "Re-download album" : "Download album"}
        >
          <Icon className="h-5 w-5" />
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
                <div className="text-[11px] text-muted-foreground/70">{q.description}</div>
              </div>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

