import { useState } from "react";
import { Link } from "react-router-dom";
import { Loader2, MoreHorizontal, Music, Play } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { ViewMoreLink } from "@/components/Grid";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";
import { LastfmConnectNudge } from "@/components/LastfmConnectNudge";
import { useToast } from "@/components/toast";
import { imageProxy } from "@/lib/utils";
import type {
  Album,
  Artist,
  MixItem,
  PageCategory,
  PageItem,
  Playlist,
  TidalPage,
  Track,
} from "@/api/types";

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
          <ViewMoreLink to={`/browse/${encodeURIComponent(category.viewAllPath)}`} />
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

const PILL_CLASS =
  "flex items-center gap-3 rounded-md bg-card/60 p-2 pr-3 transition-colors hover:bg-accent";

/**
 * Unified compact pill. Every kind renders the same three hit regions
 * Tidal's own client uses: the cover plays the item (or navigates for
 * artists, which can't be played directly), the title opens the item's
 * detail page, and the subtitle opens whoever "made" it when that's a
 * real entity the user can navigate to.
 */
function CompactPill({
  item,
  rowTracks,
}: {
  item: PageItem;
  rowTracks: Track[];
}) {
  switch (item.kind) {
    case "track":
      return <TrackPill track={item} rowTracks={rowTracks} />;
    case "album":
      return <AlbumPill album={item} />;
    case "playlist":
      return <PlaylistPill playlist={item} />;
    case "mix":
      return <MixPill mix={item} />;
    case "artist":
      return <ArtistPill artist={item} />;
    default:
      return null;
  }
}

/**
 * Shared layout for a pill whose cover plays something. The caller
 * owns the play handler, playing state, and the three text regions.
 */
function PlayablePill({
  cover,
  isPlaying,
  busy,
  ariaPlay,
  onPlay,
  title,
  titleTo,
  subtitle,
  subtitleTo,
}: {
  cover: string | null;
  isPlaying: boolean;
  busy: boolean;
  ariaPlay: string;
  onPlay: () => void;
  title: string;
  titleTo: string | null;
  subtitle: string | null;
  subtitleTo: string | null;
}) {
  return (
    <div className={`${PILL_CLASS} group`}>
      <button
        type="button"
        onClick={onPlay}
        disabled={busy}
        aria-label={ariaPlay}
        className="relative h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary disabled:opacity-80"
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
            isPlaying || busy
              ? "opacity-100"
              : "opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
          }`}
        >
          {busy ? (
            <Loader2 className="h-5 w-5 animate-spin text-foreground" />
          ) : (
            <Play className="h-5 w-5 text-foreground" fill="currentColor" />
          )}
        </span>
      </button>
      <div className="min-w-0 flex-1">
        {titleTo ? (
          <Link
            to={titleTo}
            className="block truncate text-sm font-semibold hover:underline"
          >
            {title}
          </Link>
        ) : (
          <div className="truncate text-sm font-semibold">{title}</div>
        )}
        {subtitle && (
          subtitleTo ? (
            <Link
              to={subtitleTo}
              className="block truncate text-xs text-muted-foreground hover:underline"
            >
              {subtitle}
            </Link>
          ) : (
            <div className="truncate text-xs text-muted-foreground">
              {subtitle}
            </div>
          )
        )}
      </div>
      <MoreHorizontal className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
    </div>
  );
}

function TrackPill({ track, rowTracks }: { track: Track; rowTracks: Track[] }) {
  const actions = usePlayerActions();
  const meta = usePlayerMeta();
  const isCurrent = meta.track?.id === track.id;
  const isPlaying = isCurrent && meta.playing;
  const primaryArtist = track.artists[0];
  return (
    <PlayablePill
      cover={track.album?.cover ?? null}
      isPlaying={isPlaying}
      busy={false}
      ariaPlay={isPlaying ? `Pause ${track.name}` : `Play ${track.name}`}
      onPlay={() => {
        if (isCurrent) actions.toggle();
        else actions.play(track, rowTracks);
      }}
      title={track.name}
      titleTo={track.album ? `/album/${track.album.id}` : null}
      subtitle={track.artists.map((a) => a.name).join(", ") || null}
      subtitleTo={primaryArtist ? `/artist/${primaryArtist.id}` : null}
    />
  );
}

function AlbumPill({ album }: { album: Album }) {
  const primaryArtist = album.artists[0];
  const { busy, onPlay } = useCollectionPlay({
    label: "album",
    fetch: () => api.album(album.id),
  });
  return (
    <PlayablePill
      cover={album.cover}
      isPlaying={false}
      busy={busy}
      ariaPlay={`Play ${album.name}`}
      onPlay={onPlay}
      title={album.name}
      titleTo={`/album/${album.id}`}
      subtitle={album.artists.map((a) => a.name).join(", ") || null}
      subtitleTo={primaryArtist ? `/artist/${primaryArtist.id}` : null}
    />
  );
}

function PlaylistPill({ playlist }: { playlist: Playlist }) {
  const { busy, onPlay } = useCollectionPlay({
    label: "playlist",
    fetch: () => api.playlist(playlist.id),
  });
  // Only link the creator when Tidal gives us a real id; the "0" sentinel
  // is their editorial account catch-all and doesn't resolve to a page.
  const creatorTo =
    playlist.creator_id && playlist.creator_id !== "0"
      ? `/user/${playlist.creator_id}`
      : null;
  return (
    <PlayablePill
      cover={playlist.cover}
      isPlaying={false}
      busy={busy}
      ariaPlay={`Play ${playlist.name}`}
      onPlay={onPlay}
      title={playlist.name}
      titleTo={`/playlist/${playlist.id}`}
      subtitle={playlist.creator || "Playlist"}
      subtitleTo={creatorTo}
    />
  );
}

function MixPill({ mix }: { mix: MixItem }) {
  const { busy, onPlay } = useCollectionPlay({
    label: "mix",
    fetch: () => api.mix(mix.id),
  });
  return (
    <PlayablePill
      cover={mix.cover}
      isPlaying={false}
      busy={busy}
      ariaPlay={`Play ${mix.name}`}
      onPlay={onPlay}
      title={mix.name}
      titleTo={`/mix/${encodeURIComponent(mix.id)}`}
      subtitle={mix.subtitle || "Mix"}
      subtitleTo={null}
    />
  );
}

/**
 * Artists are a special case: we don't have a safe "play an artist"
 * operation outside of a seed-based radio mix, and that needs a sample
 * ISRC which a pill doesn't know. Keep artist pills as a single Link so
 * the click is unambiguous.
 */
function ArtistPill({ artist }: { artist: Artist }) {
  return (
    <Link to={`/artist/${artist.id}`} className={PILL_CLASS}>
      <PillCover cover={artist.picture} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold">{artist.name}</div>
        <div className="truncate text-xs text-muted-foreground">Artist</div>
      </div>
      <MoreHorizontal className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
    </Link>
  );
}

/**
 * Fetch-then-play hook for collection pills (album / playlist / mix).
 * Returns a busy flag and a handler the cover button can hook into.
 * Same behaviour as PlayMediaButton: resolves the detail payload, then
 * kicks playback with the first track and the whole list as context.
 */
function useCollectionPlay({
  label,
  fetch,
}: {
  label: string;
  fetch: () => Promise<{ tracks?: Track[] }>;
}) {
  const actions = usePlayerActions();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const onPlay = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const detail = await fetch();
      const tracks = detail.tracks;
      if (!tracks?.length) {
        toast.show({
          kind: "info",
          title: "Nothing to play",
          description: `This ${label} has no playable tracks.`,
        });
        return;
      }
      actions.play(tracks[0], tracks);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't start playback",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };
  return { busy, onPlay };
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

  if (loading) {
    return (
      <div>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load home"} />;

  const { compactRows, page: filteredPage } = filterHomeRows(data);

  return (
    <div>
      <LastfmConnectNudge />
      {compactRows.map((cat, i) => (
        <CompactRow key={`compact-${i}`} category={cat} />
      ))}
      <PageView page={filteredPage} onDownload={onDownload} forceSingleRow />
    </div>
  );
}
