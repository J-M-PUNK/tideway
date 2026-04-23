import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Heart, Music } from "lucide-react";
import type { Album, Artist, Playlist, FavoriteKind } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useFavorites } from "@/hooks/useFavorites";
import { imageProxy } from "@/lib/utils";
import { PlayMediaButton } from "@/components/PlayMediaButton";

type Item = Album | Artist | Playlist;

export function MediaCard({
  item,
}: {
  item: Item;
  /** Download triggering was part of the old card overlay. Callers still
   *  pass it through but the affordance now lives on the detail page so
   *  we no longer render it in the hover area. Prop kept so the call
   *  sites stay stable. */
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
  const showHoverActions = item.kind !== "artist";
  // Hide-on-leave is suppressed while the play request is in flight, so
  // the button doesn't flicker back to opacity-0 if the cursor drifts
  // off before the fetch resolves.
  const hoverGroup = showHoverActions
    ? menuOpen
      ? "opacity-100"
      : "opacity-0 group-hover:opacity-100 focus-within:opacity-100"
    : "";

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
        {showHoverActions && (
          <>
            <div
              className={`absolute bottom-2 left-2 transition-all ${hoverGroup}`}
            >
              <PlayMediaButton
                kind={item.kind as "album" | "playlist"}
                id={item.id}
                className="h-10 w-10"
                onOpenChange={setMenuOpen}
              />
            </div>
            <InlineHeart
              kind={item.kind as FavoriteKind}
              id={item.id}
              className={`absolute bottom-2 right-2 transition-all ${hoverGroup}`}
            />
          </>
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
 * Self-contained heart button for the hover overlay. Built in-place
 * rather than going through HeartButton+Button+cva so there's no
 * chance of an opacity or size class getting stripped by the merge
 * chain. Dark circle, white outline when un-liked, primary fill when
 * liked. Stops propagation so clicking it never follows the parent
 * Link.
 */
function InlineHeart({
  kind,
  id,
  className,
}: {
  kind: FavoriteKind;
  id: string;
  className?: string;
}) {
  const favs = useFavorites();
  const liked = favs.has(kind, id);
  return (
    <button
      type="button"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        void favs.toggle(kind, id);
      }}
      aria-pressed={liked}
      aria-label={liked ? `Unlike ${kind}` : `Like ${kind}`}
      title={liked ? `Unlike ${kind}` : `Like ${kind}`}
      className={`flex h-10 w-10 items-center justify-center rounded-full bg-black/70 text-white shadow-lg transition-colors hover:bg-black/90 ${className ?? ""}`}
    >
      <Heart
        className={`h-5 w-5 ${liked ? "fill-primary stroke-primary" : ""}`}
      />
    </button>
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
