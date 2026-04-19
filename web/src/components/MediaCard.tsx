import { useState } from "react";
import { Link } from "react-router-dom";
import { Music } from "lucide-react";
import type { Album, Artist, Playlist } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { imageProxy } from "@/lib/utils";
import { DownloadButton } from "@/components/DownloadButton";

type Item = Album | Artist | Playlist;

export function MediaCard({
  item,
  onDownload,
}: {
  item: Item;
  onDownload?: OnDownload;
}) {
  const [menuOpen, setMenuOpen] = useState(false);

  const href =
    item.kind === "album"
      ? `/album/${item.id}`
      : item.kind === "artist"
        ? `/artist/${item.id}`
        : `/playlist/${item.id}`;

  const cover = imageProxy(item.kind === "artist" ? item.picture : item.cover);
  const subtitle = subtitleFor(item);
  const rounded = item.kind === "artist" ? "rounded-full" : "rounded-md";

  return (
    <Link
      to={href}
      className="group relative flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent"
    >
      <div className={`relative aspect-square w-full overflow-hidden bg-secondary ${rounded}`}>
        {cover ? (
          <img
            src={cover}
            alt={item.name}
            loading="lazy"
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-10 w-10" />
          </div>
        )}
        {onDownload && item.kind !== "artist" && (
          <div
            className={`absolute bottom-2 right-2 transition-all ${
              menuOpen
                ? "translate-y-0 opacity-100"
                : "translate-y-2 opacity-0 group-hover:translate-y-0 group-hover:opacity-100"
            }`}
          >
            <DownloadButton
              kind={item.kind}
              id={item.id}
              onPick={onDownload}
              iconOnly
              className="h-10 w-10 shadow-lg"
              onOpenChange={setMenuOpen}
            />
          </div>
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{item.name}</div>
        <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{subtitle}</div>
      </div>
    </Link>
  );
}

function subtitleFor(item: Item): string {
  if (item.kind === "album") {
    const artists = item.artists.map((a) => a.name).join(", ");
    const parts = [artists];
    if (item.year) parts.push(String(item.year));
    return parts.filter(Boolean).join(" · ");
  }
  if (item.kind === "artist") return "Artist";
  const parts: string[] = [];
  if (item.creator) parts.push(`By ${item.creator}`);
  parts.push(`${item.num_tracks} tracks`);
  return parts.join(" · ");
}
