import { useState } from "react";
import { Link } from "react-router-dom";
import { Heart, Home, Music, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/EmptyState";
import type {
  Album,
  Artist,
  MixItem,
  PageCategory,
  PageContext,
  PageItem,
  PageLinkItem,
  Playlist,
  TidalPage,
  Track,
} from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useFavorites } from "@/hooks/useFavorites";
import { useHeartPop } from "@/components/HeartButton";
import { ViewMoreLink } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { PlayMediaButton } from "@/components/PlayMediaButton";
import { TrackList } from "@/components/TrackList";
import { useColumnCount } from "@/hooks/useColumnCount";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { cn, imageProxy } from "@/lib/utils";

interface Props {
  page: TidalPage;
  onDownload: OnDownload;
  /** When true, every row is capped to one row of cards (the same
   *  treatment normally reserved for "Because you liked X" and
   *  viewAll-bearing rows). Used on the Home page where scanning
   *  beats completeness. */
  forceSingleRow?: boolean;
}

/**
 * Renders a Tidal editorial page (home, explore, drill-down) as a stack
 * of rows. Each row's layout is chosen from its type:
 *   - HorizontalList / ShortcutList / FeaturedItems / ItemList → grid of cards
 *   - TrackList → TrackList component
 *   - PageLinks → pill-grid of clickable category tiles
 */
export function PageView({ page, onDownload, forceSingleRow = false }: Props) {
  if (page.categories.length === 0) {
    return (
      <EmptyState
        icon={Music}
        title="Nothing here yet"
        description="This page doesn't have any content right now. Try the home page or explore to find something to listen to."
        action={
          <Button asChild variant="secondary" size="sm">
            <Link to="/">
              <Home className="h-4 w-4" /> Go home
            </Link>
          </Button>
        }
      />
    );
  }
  // Tidal's "Shortcuts" row is their own quick-access guess (aggregated
  // from their backend analytics). It's redundant with our "Jump back
  // in" on Home — which uses our local history and is more accurate for
  // this client — so we drop the row everywhere rather than show two
  // near-identical grids stacked.
  //
  // Editorial-title blacklist: Tidal's home / explore feeds carry a few
  // sections that are pure marketing fill ("Fun playlists you'll love",
  // "Essentials to explore") rather than personalized recommendations.
  // Match on the lower-cased title since Tidal capitalisation has
  // shifted across regions and we want a single rule.
  const HIDDEN_SECTION_TITLES = new Set([
    "fun playlists you'll love",
    "fun playlists you’ll love",
    "essentials to explore",
  ]);
  const categories = page.categories.filter((cat) => {
    if (cat.type === "ShortcutList") return false;
    if (
      cat.title &&
      HIDDEN_SECTION_TITLES.has(cat.title.trim().toLowerCase())
    ) {
      return false;
    }
    return true;
  });
  return (
    <div className="flex flex-col gap-8">
      {categories.map((cat, i) => (
        <Section
          key={`${cat.type}-${i}`}
          category={cat}
          onDownload={onDownload}
          forceSingleRow={forceSingleRow}
        />
      ))}
    </div>
  );
}

function Section({
  category,
  onDownload,
  forceSingleRow = false,
}: {
  category: PageCategory;
  onDownload: OnDownload;
  forceSingleRow?: boolean;
}) {
  const { type, title, subtitle, items, context, viewAllPath } = category;
  const columnCount = useColumnCount();

  if (type === "TrackList") {
    return (
      <div>
        {title && (
          <SectionHeader
            title={title}
            subtitle={subtitle}
            context={context}
            viewAllPath={viewAllPath}
          />
        )}
        <TrackList
          tracks={items.filter((i): i is Track => i.kind === "track")}
          onDownload={onDownload}
        />
      </div>
    );
  }

  if (type === "PageLinks") {
    const links = items.filter((i): i is PageLinkItem => i.kind === "pagelink");
    if (links.length === 0) return null;
    return (
      <div>
        {title && <SectionHeader title={title} subtitle={subtitle} />}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
          {links.map((l) => (
            <PageLinkTile key={l.path} link={l} />
          ))}
        </div>
      </div>
    );
  }

  // "Because you liked X" rows (context present) and any row with a
  // viewAll get capped to a single row of cards — the rest live behind
  // the "View more" link in the header. Everything else keeps the
  // full responsive grid. Home passes forceSingleRow so scanning all
  // sections is quick; the only cost is some rows lose their "view
  // more" affordance when Tidal didn't send a viewAllPath for them.
  const singleRow = forceSingleRow || Boolean(context || viewAllPath);
  // Cap single rows at whichever is smaller: five (matches Tidal's
  // homepage density) or the actual visible column count. Without the
  // second cap a narrower viewport renders 4 cards then an orphaned
  // 5th on a partial second row.
  const ROW_CAP = 5;
  const visible = singleRow
    ? items.slice(0, Math.min(ROW_CAP, columnCount))
    : items;
  // Track rows need a shared playback context: clicking one track's
  // cover should play it with the row's other tracks queued up.
  const rowTracks = items.filter((i): i is Track => i.kind === "track");
  return (
    <div>
      {title && (
        <SectionHeader
          title={title}
          subtitle={subtitle}
          context={context}
          viewAllPath={viewAllPath}
        />
      )}
      <div
        className={cn(
          "grid gap-4",
          singleRow
            ? "grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5"
            : "grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6",
        )}
      >
        {visible.map((it, idx) => (
          <PageItemCard
            key={`${it.kind}-${itemKey(it)}-${idx}`}
            item={it}
            onDownload={onDownload}
            rowTracks={rowTracks}
          />
        ))}
      </div>
    </div>
  );
}

function itemKey(i: PageItem): string {
  return "id" in i ? i.id : i.kind === "pagelink" ? i.path : "";
}

function contextPath(ctx: PageContext): string | null {
  switch (ctx.kind) {
    case "album":
      return `/album/${ctx.id}`;
    case "artist":
      return `/artist/${ctx.id}`;
    case "playlist":
      return `/playlist/${ctx.id}`;
    case "mix":
      return `/mix/${encodeURIComponent(ctx.id)}`;
    default:
      return null;
  }
}

function SectionHeader({
  title,
  subtitle,
  context,
  viewAllPath,
}: {
  title: string;
  subtitle?: string;
  context?: PageContext;
  viewAllPath?: string;
}) {
  const ctxPath = context ? contextPath(context) : null;
  const ctxCover = context ? imageProxy(context.cover) : null;

  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div className="flex min-w-0 items-center gap-3">
        {context &&
          (ctxPath ? (
            <Link
              to={ctxPath}
              className="h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary transition-transform hover:scale-105"
              title={context.title}
            >
              {ctxCover ? (
                <img
                  src={ctxCover}
                  alt=""
                  className="h-full w-full object-cover"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center text-muted-foreground">
                  <Music className="h-5 w-5" />
                </div>
              )}
            </Link>
          ) : (
            <div className="h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary">
              {ctxCover ? (
                <img
                  src={ctxCover}
                  alt=""
                  className="h-full w-full object-cover"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center text-muted-foreground">
                  <Music className="h-5 w-5" />
                </div>
              )}
            </div>
          ))}
        <div className="min-w-0">
          {context ? (
            <>
              <div className="truncate text-xs uppercase tracking-wider text-muted-foreground">
                {title}
              </div>
              {ctxPath ? (
                <Link
                  to={ctxPath}
                  className="block truncate text-xl font-bold tracking-tight hover:underline"
                >
                  {context.title}
                </Link>
              ) : (
                <div className="truncate text-xl font-bold tracking-tight">
                  {context.title}
                </div>
              )}
            </>
          ) : (
            <>
              <h2 className="truncate text-xl font-bold tracking-tight">
                {title}
              </h2>
              {subtitle && (
                <div className="mt-0.5 truncate text-sm text-muted-foreground">
                  {subtitle}
                </div>
              )}
            </>
          )}
        </div>
      </div>
      {viewAllPath && (
        <ViewMoreLink to={`/browse/${encodeURIComponent(viewAllPath)}`} />
      )}
    </div>
  );
}

function PageItemCard({
  item,
  onDownload,
  rowTracks = [],
}: {
  item: PageItem;
  onDownload: OnDownload;
  rowTracks?: Track[];
}) {
  if (
    item.kind === "album" ||
    item.kind === "artist" ||
    item.kind === "playlist"
  ) {
    return (
      <MediaCard
        item={item as Album | Artist | Playlist}
        onDownload={onDownload}
      />
    );
  }
  if (item.kind === "mix") {
    return <MixCard mix={item} />;
  }
  if (item.kind === "track") {
    return <TrackCard track={item} rowTracks={rowTracks} />;
  }
  return null;
}

/**
 * Track card matching the MediaCard layout so tracks on a view-more
 * page feel like first-class items. The whole card is a Link to the
 * album; the hover overlay puts a play button in the bottom-left and
 * a heart in the bottom-right, same slots the album / playlist cards
 * use. Clicking play kicks off the track with the row's other tracks
 * as the queue. Artist names in the subtitle are their own Links that
 * stop propagation so the card's Link doesn't swallow the click.
 */
function TrackCard({ track, rowTracks }: { track: Track; rowTracks: Track[] }) {
  const actions = usePlayerActions();
  const meta = usePlayerMeta();
  const isCurrent = meta.track?.id === track.id;
  const isPlaying = isCurrent && meta.playing;
  const cover = track.album?.cover ? imageProxy(track.album.cover) : null;
  const albumPath = track.album ? `/album/${track.album.id}` : null;
  const handlePlay = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (isCurrent) {
      actions.toggle();
    } else {
      actions.play(track, rowTracks.length > 0 ? rowTracks : [track]);
    }
  };
  const overlayClass =
    "opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100";
  const inner = (
    <>
      <div className="relative aspect-square overflow-hidden rounded-md bg-secondary">
        {cover ? (
          <img
            src={cover}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
          />
        ) : (
          <Music className="m-auto h-10 w-10 text-muted-foreground" />
        )}
        <button
          type="button"
          onClick={handlePlay}
          aria-label={isPlaying ? `Pause ${track.name}` : `Play ${track.name}`}
          className={cn(
            "absolute bottom-2 left-2 flex h-10 w-10 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg transition-transform hover:scale-105",
            isPlaying ? "opacity-100" : overlayClass,
          )}
        >
          <Play className="h-5 w-5" fill="currentColor" />
        </button>
        <TrackHeart
          trackId={track.id}
          className={cn("absolute bottom-2 right-2", overlayClass)}
        />
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{track.name}</div>
        <div className="truncate text-xs text-muted-foreground">
          {track.artists.map((a, i) => (
            <span key={a.id || i}>
              {i > 0 && ", "}
              {a.id ? (
                <Link
                  to={`/artist/${a.id}`}
                  onClick={(e) => e.stopPropagation()}
                  className="hover:text-foreground hover:underline"
                >
                  {a.name}
                </Link>
              ) : (
                a.name
              )}
            </span>
          ))}
        </div>
      </div>
    </>
  );
  if (albumPath) {
    return (
      <Link
        to={albumPath}
        className="group flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent"
      >
        {inner}
      </Link>
    );
  }
  return (
    <div className="group flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent">
      {inner}
    </div>
  );
}

function TrackHeart({
  trackId,
  className,
}: {
  trackId: string;
  className?: string;
}) {
  const favs = useFavorites();
  const liked = favs.has("track", trackId);
  // Shared heart-pop hook from HeartButton — keeps the like
  // animation identical across every surface (track rows, now-
  // playing bar, MediaCard overlay, this PageView TrackList row,
  // detail-page Add-to-Library, etc).
  const popping = useHeartPop(liked);
  return (
    <button
      type="button"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        void favs.toggle("track", trackId);
      }}
      aria-pressed={liked}
      aria-label={liked ? "Unlike track" : "Like track"}
      title={liked ? "Unlike track" : "Like track"}
      className={cn(
        "flex h-10 w-10 items-center justify-center rounded-full bg-black/70 text-white shadow-lg transition-colors hover:bg-black/90",
        className,
      )}
    >
      <Heart
        className={cn(
          "h-5 w-5",
          liked && "fill-primary stroke-primary",
          popping && "animate-heart-pop",
        )}
      />
    </button>
  );
}

function MixHeart({ mixId, className }: { mixId: string; className?: string }) {
  const favs = useFavorites();
  const liked = favs.has("mix", mixId);
  const popping = useHeartPop(liked);
  return (
    <button
      type="button"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        void favs.toggle("mix", mixId);
      }}
      aria-pressed={liked}
      aria-label={liked ? "Unlike mix" : "Like mix"}
      title={liked ? "Unlike mix" : "Like mix"}
      className={cn(
        "flex h-10 w-10 items-center justify-center rounded-full bg-black/70 text-white shadow-lg transition-colors hover:bg-black/90",
        className,
      )}
    >
      <Heart
        className={cn(
          "h-5 w-5",
          liked && "fill-primary stroke-primary",
          popping && "animate-heart-pop",
        )}
      />
    </button>
  );
}

function MixCard({ mix }: { mix: MixItem }) {
  // Same bottom-left hover-play + bottom-right hover-heart treatment as
  // MediaCard. tidalapi exposes favorites/mixes/add|remove, so mixes
  // get the full card interaction instead of play-only.
  const [menuOpen, setMenuOpen] = useState(false);
  const hoverGroup = menuOpen
    ? "opacity-100"
    : "opacity-0 group-hover:opacity-100 focus-within:opacity-100";
  return (
    <Link
      to={`/mix/${encodeURIComponent(mix.id)}`}
      className="group relative flex flex-col gap-3 rounded-lg bg-card p-4 transition-colors hover:bg-accent"
    >
      <div className="relative aspect-square overflow-hidden rounded-md bg-secondary">
        {mix.cover ? (
          <img
            src={imageProxy(mix.cover)}
            alt=""
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
          />
        ) : (
          <Music className="m-auto h-10 w-10 text-muted-foreground" />
        )}
        <div
          className={`absolute bottom-2 left-2 transition-all ${hoverGroup}`}
        >
          <PlayMediaButton
            kind="mix"
            id={mix.id}
            className="h-10 w-10"
            onOpenChange={setMenuOpen}
          />
        </div>
        <MixHeart
          mixId={mix.id}
          className={`absolute bottom-2 right-2 transition-all ${hoverGroup}`}
        />
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{mix.name}</div>
        {mix.subtitle && (
          <div className="truncate text-xs text-muted-foreground">
            {mix.subtitle}
          </div>
        )}
      </div>
    </Link>
  );
}

function PageLinkTile({ link }: { link: PageLinkItem }) {
  // Deterministic color from the title so genre tiles have visual variety
  // without needing editorial artwork.
  const hue = hashHue(link.title);
  return (
    <Link
      to={`/browse/${encodeURIComponent(link.path)}`}
      className={cn(
        "relative flex aspect-[5/3] items-start overflow-hidden rounded-lg p-4 text-lg font-bold tracking-tight text-foreground transition-transform hover:scale-[1.02]",
      )}
      style={{
        background: `linear-gradient(135deg, hsl(${hue}, 60%, 35%), hsl(${(hue + 40) % 360}, 70%, 20%))`,
      }}
    >
      <span className="line-clamp-2">{link.title}</span>
    </Link>
  );
}

function hashHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h) % 360;
}
