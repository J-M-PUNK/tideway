import { Link } from "react-router-dom";
import { ChevronRight, MoreHorizontal, Music, Play } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";
import { LastfmConnectNudge } from "@/components/LastfmConnectNudge";
import { imageProxy } from "@/lib/utils";
import type { PageCategory, PageItem, TidalPage, Track } from "@/api/types";

// Titles of Tidal editorial rows we do not want on our home page.
// Matched case insensitively as a substring against the row title,
// normalised to strip curly Unicode apostrophes so either `'` or `'`
// in the source catches.
const HIDDEN_HOME_ROW_TITLES = [
  "albums you'll enjoy",
  "your favorite artists",
  "popular playlists",
  // Tidal renders this as a single-card row whose only item is the
  // "My Most Listened" auto-playlist. Redundant as a launcher when
  // the playlist itself is already in Your Library.
  "your listening history",
  "spotlighted uploads",
  // "Because you liked X" / "Because you listened to X" rows are a
  // long tail of duplicates of the items that sit right above them.
  // The user can still find the same recommendations inside the full
  // discovery surfaces (artist pages, mixes). Drop them from the home
  // stream so the page stays scannable.
  "because you liked",
  "because you listened",
];

// "Recommended new tracks" and "Uploads for you" both surface
// newly-added catalog content Tidal thinks we'd like. Fold them into
// a single row titled to match the sibling "Suggested new albums for
// you" row Tidal also emits, so both the songs and the albums feed
// read like a consistent section pair.
const MERGE_SOURCE_TITLES = ["recommended new tracks", "uploads for you"];
const MERGED_ROW_TITLE = "Suggested new songs for you";

// Rows that read better as a compact pill shelf than as a grid of
// big cards. Track-heavy rows belong here because the density lets
// the user scan more recently-played / suggested tracks without
// scrolling. Albums / playlists / mixes stay as big cards below.
const COMPACT_ROW_TITLES = new Set([
  "recently played",
  normalizeTitle(MERGED_ROW_TITLE),
]);

// Desired order for the card-style rows that render through PageView.
// Anything not listed here keeps whatever order Tidal sent.
const PRIORITY_ROW_ORDER = [
  "suggested new albums for you",
  "custom mixes",
  "personal radio stations",
];

function normalizeTitle(s: string): string {
  return s
    .toLowerCase()
    .replace(/[‘’‛′]/g, "'") // curly / prime apostrophes
    .replace(/[“”‟″]/g, '"'); // curly / prime double quotes
}

function filterHomeRows(page: TidalPage): {
  compactRows: PageCategory[];
  page: TidalPage;
} {
  const hideNeedles = HIDDEN_HOME_ROW_TITLES.map(normalizeTitle);
  const mergeNeedles = MERGE_SOURCE_TITLES.map(normalizeTitle);

  // First pass: drop hidden rows, and collect the merge sources into a
  // single merged row that takes the first merge source's slot.
  const kept: PageCategory[] = [];
  const mergedItems: PageItem[] = [];
  let mergedTemplate: PageCategory | null = null;

  for (const cat of page.categories) {
    const title = normalizeTitle(cat.title ?? "");
    if (hideNeedles.some((n) => title.includes(n))) continue;
    if (mergeNeedles.some((n) => title.includes(n))) {
      if (mergedTemplate === null) {
        mergedTemplate = cat;
        kept.push(cat); // placeholder, overwritten below
      }
      for (const it of cat.items) mergedItems.push(it);
      continue;
    }
    kept.push(cat);
  }

  if (mergedTemplate) {
    const seen = new Set<string>();
    const unique = mergedItems.filter((it) => {
      const key = `${(it as { kind?: string }).kind ?? ""}:${
        (it as { id?: string }).id ?? ""
      }`;
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    const mergedIdx = kept.indexOf(mergedTemplate);
    kept[mergedIdx] = {
      ...mergedTemplate,
      title: MERGED_ROW_TITLE,
      items: unique,
    };
  }

  // Second pass: split out the compact pill rows (rendered above the
  // fold) from everything else (rendered as big cards via PageView).
  // Compact rows come out in the order declared in COMPACT_ROW_TITLES
  // so the visual sequence on the page stays predictable regardless
  // of Tidal's feed order.
  const compactByTitle = new Map<string, PageCategory>();
  const otherRows: PageCategory[] = [];
  for (const cat of kept) {
    const title = normalizeTitle(cat.title ?? "");
    if (COMPACT_ROW_TITLES.has(title)) {
      compactByTitle.set(title, cat);
    } else {
      otherRows.push(cat);
    }
  }
  const compactRows: PageCategory[] = [];
  for (const title of COMPACT_ROW_TITLES) {
    const cat = compactByTitle.get(title);
    if (cat) compactRows.push(cat);
  }

  // Third pass: reorder the card rows so the priority titles appear
  // first in the configured order, with everything else following in
  // its original position.
  const priorityOrder = PRIORITY_ROW_ORDER.map(normalizeTitle);
  const priorityRows: Array<PageCategory | undefined> = priorityOrder.map(
    () => undefined,
  );
  const leftover: PageCategory[] = [];
  for (const cat of otherRows) {
    const title = normalizeTitle(cat.title ?? "");
    const idx = priorityOrder.indexOf(title);
    if (idx >= 0) {
      priorityRows[idx] = cat;
    } else {
      leftover.push(cat);
    }
  }
  const finalCategories: PageCategory[] = [
    ...priorityRows.filter((c): c is PageCategory => !!c),
    ...leftover,
  ];

  return {
    compactRows,
    page: { ...page, categories: finalCategories },
  };
}

// ---------------------------------------------------------------------------
// Compact pill row. Cover on the left, title + subtitle to the right,
// three across, up to nine visible. Used for Recently played and the
// merged Suggested new songs row where density reads better than a
// wall of big cards.
// ---------------------------------------------------------------------------
const COMPACT_VISIBLE_COUNT = 9;

function CompactRow({ category }: { category: PageCategory }) {
  const items = category.items.slice(0, COMPACT_VISIBLE_COUNT);
  if (items.length === 0) return null;
  // View more routes to the dedicated drill-down page Tidal emits for
  // this row, matching the card rows' SectionHeader behaviour.
  const hasMore =
    category.items.length > COMPACT_VISIBLE_COUNT && !!category.viewAllPath;
  return (
    <div className="mb-10">
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <h2 className="text-xl font-bold tracking-tight">{category.title}</h2>
        {hasMore && category.viewAllPath && (
          <Link
            to={`/browse/${encodeURIComponent(category.viewAllPath)}`}
            className="flex flex-shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground transition-colors hover:text-foreground"
          >
            View more <ChevronRight className="h-3.5 w-3.5" />
          </Link>
        )}
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item, i) => (
          <CompactPill
            key={`${item.kind}-${pillId(item, i)}`}
            item={item}
            rowTracks={rowTracks(items)}
          />
        ))}
      </div>
    </div>
  );
}

// Pull the row's tracks out once so every CompactPill that represents a
// track shares the same context queue. Clicking a track's cover plays
// that track and sets next/prev to walk the rest of the row's tracks.
function rowTracks(items: PageItem[]): Track[] {
  return items.filter((it): it is Track => it.kind === "track");
}

function pillId(item: PageItem, fallback: number): string {
  if ("id" in item) return String(item.id);
  if (item.kind === "pagelink") return item.path;
  return String(fallback);
}

function pillRoute(item: PageItem): string | null {
  switch (item.kind) {
    case "track":
      return item.album ? `/album/${item.album.id}` : null;
    case "album":
      return `/album/${item.id}`;
    case "artist":
      return `/artist/${item.id}`;
    case "playlist":
      return `/playlist/${item.id}`;
    case "mix":
      return `/mix/${item.id}`;
    case "pagelink":
      return null;
  }
}

function pillCover(item: PageItem): string | null {
  switch (item.kind) {
    case "track":
      return item.album?.cover ?? null;
    case "album":
      return item.cover ?? null;
    case "artist":
      return item.picture ?? null;
    case "playlist":
      return item.cover ?? null;
    case "mix":
      return item.cover ?? null;
    default:
      return null;
  }
}

function pillSubtitle(item: PageItem): string {
  switch (item.kind) {
    case "track":
      return item.artists.map((a) => a.name).join(", ");
    case "album":
      return item.artists.map((a) => a.name).join(", ");
    case "artist":
      return "Artist";
    case "playlist":
      return item.creator || "Playlist";
    case "mix":
      return item.subtitle || "Mix";
    default:
      return "";
  }
}

function CompactPill({
  item,
  rowTracks,
}: {
  item: PageItem;
  rowTracks: Track[];
}) {
  // Tracks have three separate hit regions so the card behaves like
  // Tidal's own client: cover plays, title opens the album, subtitle
  // opens the artist. Non-track items (albums, artists, playlists,
  // mixes) keep the simpler whole-pill-is-a-link behaviour.
  if (item.kind === "track") {
    return <TrackPill track={item} rowTracks={rowTracks} />;
  }
  return <EntityPill item={item} />;
}

const PILL_CLASS =
  "flex items-center gap-3 rounded-md bg-card/60 p-2 pr-3 transition-colors hover:bg-accent";

function EntityPill({ item }: { item: PageItem }) {
  const to = pillRoute(item);
  const cover = pillCover(item);
  const name = "name" in item ? item.name : "title" in item ? item.title : "";
  const subtitle = pillSubtitle(item);
  const inner = (
    <>
      <PillCover cover={cover} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold">{name}</div>
        {subtitle && (
          <div className="truncate text-xs text-muted-foreground">
            {subtitle}
          </div>
        )}
      </div>
      <MoreHorizontal className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
    </>
  );
  if (to) {
    return (
      <Link to={to} className={PILL_CLASS}>
        {inner}
      </Link>
    );
  }
  return <div className={PILL_CLASS}>{inner}</div>;
}

function TrackPill({ track, rowTracks }: { track: Track; rowTracks: Track[] }) {
  const actions = usePlayerActions();
  const meta = usePlayerMeta();
  const isCurrent = meta.track?.id === track.id;
  const isPlaying = isCurrent && meta.playing;
  const cover = track.album?.cover ?? null;
  const albumPath = track.album ? `/album/${track.album.id}` : null;
  const primaryArtist = track.artists[0];
  const artistPath = primaryArtist ? `/artist/${primaryArtist.id}` : null;
  const artistLabel = track.artists.map((a) => a.name).join(", ");
  const handlePlay = () => {
    if (isCurrent) {
      actions.toggle();
    } else {
      actions.play(track, rowTracks);
    }
  };
  return (
    <div className={`${PILL_CLASS} group`}>
      <button
        type="button"
        onClick={handlePlay}
        aria-label={isPlaying ? `Pause ${track.name}` : `Play ${track.name}`}
        className="relative h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary"
      >
        {cover ? (
          <img
            src={imageProxy(cover)}
            alt=""
            className="h-full w-full object-cover"
            loading="lazy"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-5 w-5" />
          </div>
        )}
        <span
          className={`absolute inset-0 flex items-center justify-center bg-black/50 transition-opacity ${
            isPlaying
              ? "opacity-100"
              : "opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
          }`}
        >
          <Play className="h-5 w-5 text-foreground" fill="currentColor" />
        </span>
      </button>
      <div className="min-w-0 flex-1">
        {albumPath ? (
          <Link
            to={albumPath}
            className="block truncate text-sm font-semibold hover:underline"
          >
            {track.name}
          </Link>
        ) : (
          <div className="truncate text-sm font-semibold">{track.name}</div>
        )}
        {artistLabel && (
          artistPath ? (
            <Link
              to={artistPath}
              className="block truncate text-xs text-muted-foreground hover:underline"
            >
              {artistLabel}
            </Link>
          ) : (
            <div className="truncate text-xs text-muted-foreground">
              {artistLabel}
            </div>
          )
        )}
      </div>
      <MoreHorizontal className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
    </div>
  );
}

function PillCover({ cover }: { cover: string | null }) {
  return (
    <div className="h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary">
      {cover ? (
        <img
          src={imageProxy(cover)}
          alt=""
          className="h-full w-full object-cover"
          loading="lazy"
        />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-muted-foreground">
          <Music className="h-5 w-5" />
        </div>
      )}
    </div>
  );
}

export function Home({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading, error } = useApi(() => api.page("home"), []);

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  if (loading) {
    return (
      <div>
        <h1 className="mb-6 text-4xl font-bold tracking-tight">{greeting}</h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load home"} />;

  const { compactRows, page: filteredPage } = filterHomeRows(data);

  return (
    <div>
      <h1 className="mb-8 text-4xl font-bold tracking-tight">{greeting}</h1>
      <LastfmConnectNudge />
      {compactRows.map((cat, i) => (
        <CompactRow key={`compact-${i}`} category={cat} />
      ))}
      <PageView page={filteredPage} onDownload={onDownload} forceSingleRow />
    </div>
  );
}
