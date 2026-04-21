import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Music } from "lucide-react";
import type { Album, Artist, Playlist } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { imageProxy } from "@/lib/utils";
import { DownloadButton } from "@/components/DownloadButton";
import { PlayMediaButton } from "@/components/PlayMediaButton";

type Item = Album | Artist | Playlist;

export function MediaCard({
  item,
  onDownload,
}: {
  item: Item;
  onDownload?: OnDownload;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const navigate = useNavigate();

  const href =
    item.kind === "album"
      ? `/album/${item.id}`
      : item.kind === "artist"
        ? `/artist/${item.id}`
        : `/playlist/${item.id}`;

  const cover = imageProxy(item.kind === "artist" ? item.picture : item.cover);
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
        {item.kind !== "artist" && (
          <div
            className={`absolute bottom-2 right-2 flex items-center gap-2 transition-all ${
              menuOpen
                ? "translate-y-0 opacity-100"
                : "translate-y-2 opacity-0 group-hover:translate-y-0 group-hover:opacity-100"
            }`}
          >
            <PlayMediaButton
              kind={item.kind}
              id={item.id}
              className="h-10 w-10"
              onOpenChange={setMenuOpen}
            />
            {onDownload && (
              <DownloadButton
                kind={item.kind}
                id={item.id}
                onPick={onDownload}
                iconOnly
                className="h-10 w-10 shadow-lg"
                onOpenChange={setMenuOpen}
              />
            )}
          </div>
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{item.name}</div>
        <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
          <Subtitle item={item} onNavigate={navigate} />
        </div>
      </div>
    </Link>
  );
}

/**
 * Subtitle renderer that embeds the creator name as a clickable link
 * to their profile (when we have a real creator_id). Stops event
 * propagation so the outer card `<Link>` doesn't swallow the click
 * and also respects the "0" sentinel Tidal uses for editorial
 * accounts.
 */
function Subtitle({
  item,
  onNavigate,
}: {
  item: Item;
  onNavigate: (path: string) => void;
}) {
  if (item.kind === "album") {
    // Each artist is a button (same pattern the playlist creator uses
    // below) so clicking navigates to /artist/:id without the outer
    // card Link swallowing the click.
    return (
      <>
        {item.artists.map((a, i) => (
          <span key={a.id}>
            {i > 0 && ", "}
            <button
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onNavigate(`/artist/${a.id}`);
              }}
              className="hover:text-foreground hover:underline"
            >
              {a.name}
            </button>
          </span>
        ))}
        {item.year && <span> · {item.year}</span>}
      </>
    );
  }
  if (item.kind === "artist") return <>Artist</>;
  // Playlist — creator may be clickable.
  const hasCreatorLink =
    item.creator && item.creator_id && item.creator_id !== "0";
  return (
    <>
      {item.creator && (
        <>
          By{" "}
          {hasCreatorLink ? (
            <button
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onNavigate(`/user/${item.creator_id}`);
              }}
              className="hover:text-foreground hover:underline"
            >
              {item.creator}
            </button>
          ) : (
            <span>{item.creator}</span>
          )}
          {" · "}
        </>
      )}
      {item.num_tracks} tracks
    </>
  );
}
