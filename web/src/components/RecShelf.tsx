import { Link } from "react-router-dom";
import { Loader2, Music, Plus } from "lucide-react";
import type { Album } from "@/api/types";
import { InlineHeart } from "@/components/MediaCard";
import { PlayMediaButton } from "@/components/PlayMediaButton";
import { ViewMoreLink } from "@/components/Grid";
import { imageProxy } from "@/lib/utils";

export type RecAlbum = Album & { reason?: string };

export type RecSection = {
  key: string;
  title: string;
  subtitle: string;
  albums: RecAlbum[];
  /** Set by the backend on rows that have a drill-down page. Absent on
   *  rows that are already showing everything they have. */
  view_more?: string;
  /** Paging state, on drill-down rows that can fetch further pages. */
  slug?: string;
  offset?: number;
  /** Listing position the next page starts at, supplied by the backend. */
  next_offset?: number;
  has_more?: boolean;
};

/**
 * One horizontal shelf of recommendation cards. Shared by the For You
 * page and its drill-downs so a row looks and behaves identically
 * wherever it appears — the drill-downs are the same sections rendered
 * one-per-genre instead of blended.
 */
export function RecShelf({
  section,
  onLoadMore,
  loadingMore,
}: {
  section: RecSection;
  /** Supplied on drill-down rows. Appends the next page in place. */
  onLoadMore?: () => void;
  loadingMore?: boolean;
}) {
  return (
    <section>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-lg font-bold tracking-tight">{section.title}</h3>
          {section.subtitle && (
            <p className="text-xs text-muted-foreground">{section.subtitle}</p>
          )}
        </div>
        {section.view_more && <ViewMoreLink to={section.view_more} />}
      </div>
      <div className="mt-3 flex gap-4 overflow-x-auto pb-2 scrollbar-thin">
        {section.albums.map((item) => (
          <RecCard key={item.id} item={item} />
        ))}
        {onLoadMore && (
          <button
            type="button"
            onClick={onLoadMore}
            disabled={loadingMore}
            aria-label={`Load more ${section.title}`}
            // Sits at the end of the row so reaching the last card is
            // what surfaces it, rather than a control competing with
            // the heading.
            className="flex w-44 shrink-0 flex-col items-center justify-center gap-2 rounded-lg bg-card p-4 text-sm font-semibold text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-60"
          >
            {loadingMore ? (
              <Loader2 className="h-6 w-6 animate-spin" />
            ) : (
              <Plus className="h-6 w-6" />
            )}
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </div>
    </section>
  );
}

function RecCard({ item }: { item: RecAlbum }) {
  const cover = imageProxy(item.cover);
  const subtitle = item.artists?.map((a) => a.name).join(", ") ?? "";
  // Same hover-reveal contract as MediaCard: play (bottom-left) and like
  // (bottom-right) sit on the cover and fade in on hover / keyboard focus.
  const hoverActions =
    "opacity-0 transition-all duration-200 ease-out group-hover:opacity-100 focus-within:opacity-100";
  return (
    <div className="group relative flex w-44 shrink-0 flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent">
      <Link to={`/album/${item.id}`} className="flex flex-col gap-3">
        <div className="relative aspect-square overflow-hidden rounded-md bg-secondary">
          {cover ? (
            <img
              src={cover}
              alt=""
              className="h-full w-full object-cover transition-transform group-hover:scale-105"
              loading="lazy"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-muted-foreground">
              <Music className="h-10 w-10" />
            </div>
          )}
          <PlayMediaButton
            kind="album"
            id={item.id}
            className={`absolute bottom-2 left-2 h-10 w-10 ${hoverActions}`}
          />
          <InlineHeart
            kind="album"
            id={item.id}
            className={`absolute bottom-2 right-2 ${hoverActions}`}
          />
        </div>
        <div className="min-w-0">
          <div className="truncate font-semibold">{item.name}</div>
          <div className="truncate text-xs text-muted-foreground">
            {subtitle}
          </div>
          {item.reason && (
            <div
              className="mt-1 line-clamp-1 text-[11px] text-primary/80"
              title={item.reason}
            >
              {item.reason}
            </div>
          )}
        </div>
      </Link>
    </div>
  );
}
