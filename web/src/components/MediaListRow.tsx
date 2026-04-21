import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Music } from "lucide-react";
import type { Album, Artist, Playlist } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { imageProxy, formatDuration } from "@/lib/utils";
import { DownloadButton } from "@/components/DownloadButton";
import { PlayMediaButton } from "@/components/PlayMediaButton";

type Item = Album | Artist | Playlist;

/**
 * Dense row variant of MediaCard. The library page offers a
 * grid-vs-list toggle the way every other streaming service does —
 * cards are nicer for casual browsing, list is faster to scan and
 * fits more items in the viewport at once.
 */
export function MediaListRow({
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
      className="group flex items-center gap-4 rounded-md px-3 py-2 transition-colors hover:bg-accent"
    >
      <div
        className={`h-12 w-12 flex-shrink-0 overflow-hidden bg-secondary ${rounded}`}
      >
        {cover ? (
          <img
            src={cover}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-5 w-5" />
          </div>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium">{item.name}</div>
        <div className="truncate text-xs text-muted-foreground">
          <Subtitle item={item} onNavigate={navigate} />
        </div>
      </div>
      <Trailing item={item} />
      {item.kind !== "artist" && (
        <div
          className={`flex flex-shrink-0 items-center gap-1 transition-opacity ${
            menuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100"
          }`}
        >
          <PlayMediaButton
            kind={item.kind}
            id={item.id}
            className="h-8 w-8"
            onOpenChange={setMenuOpen}
          />
          {onDownload && (
            <DownloadButton
              kind={item.kind}
              id={item.id}
              onPick={onDownload}
              iconOnly
              variant="ghost"
              size="sm"
              onOpenChange={setMenuOpen}
            />
          )}
        </div>
      )}
    </Link>
  );
}

function Subtitle({
  item,
  onNavigate,
}: {
  item: Item;
  onNavigate: (path: string) => void;
}) {
  if (item.kind === "album") {
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
      </>
    );
  }
  if (item.kind === "artist") return <>Artist</>;
  const hasCreatorLink =
    item.creator && item.creator_id && item.creator_id !== "0";
  return (
    <>
      {item.creator &&
        (hasCreatorLink ? (
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
        ))}
    </>
  );
}

/**
 * Right-aligned metadata: Tidal-style "year" for albums, track count
 * (and duration when present) for playlists. Artists get nothing —
 * there isn't a useful secondary field.
 */
function Trailing({ item }: { item: Item }) {
  if (item.kind === "album") {
    return (
      <div className="hidden w-24 flex-shrink-0 text-right text-xs text-muted-foreground sm:block">
        {item.year ?? ""}
      </div>
    );
  }
  if (item.kind === "playlist") {
    return (
      <div className="hidden w-32 flex-shrink-0 text-right text-xs text-muted-foreground sm:block">
        {item.num_tracks} tracks
        {item.duration ? ` · ${formatDuration(item.duration)}` : ""}
      </div>
    );
  }
  return null;
}
