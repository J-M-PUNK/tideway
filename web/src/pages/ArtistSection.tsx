import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { List, Video as VideoIcon } from "lucide-react";
import { api } from "@/api/client";
import type { Album, Artist, Track, Video } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { useLikedByArtist } from "@/hooks/useLikedByArtist";
import { useVideoPlayer } from "@/hooks/useVideoPlayer";
import { Grid } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { MediaListRow } from "@/components/MediaListRow";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import { VideoCard } from "@/components/VideoCard";
import { ViewToggle, type ViewMode } from "@/components/ViewToggle";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

/**
 * Sort orders the album-list view exposes. Album discography is
 * unusual versus the rest of the app: items don't have a
 * "recently added" notion (they're not user actions), they have
 * release dates. So the menu offers newest/oldest/alpha instead
 * of Library's recent/alpha pair.
 */
type AlbumSort = "newest" | "oldest" | "alpha";

const ALBUM_VIEW_KEY = "tideway:artist-section-view";
const ALBUM_SORT_KEY = "tideway:artist-section-sort";

function loadAlbumView(): ViewMode {
  try {
    const v = localStorage.getItem(ALBUM_VIEW_KEY);
    return v === "list" ? "list" : "grid";
  } catch {
    return "grid";
  }
}

function loadAlbumSort(): AlbumSort {
  try {
    const v = localStorage.getItem(ALBUM_SORT_KEY);
    if (v === "oldest" || v === "alpha") return v;
    return "newest";
  } catch {
    return "newest";
  }
}

/**
 * Sort an album list by the chosen order. Stable for ties (e.g.
 * two albums released the same day) and tolerant of missing
 * release_date / name. Pure so it can be unit-tested without
 * standing up the page.
 */
export function sortAlbums(albums: Album[], sort: AlbumSort): Album[] {
  // Build sortable keys once so the comparator doesn't repeatedly
  // parse the same release_date string. .slice() so we don't
  // mutate the array the parent has cached.
  const decorated = albums.map((a, i) => ({
    album: a,
    // release_date arrives as ISO yyyy-MM-dd from Tidal. Fall
    // back to the year column when the day-level date is null
    // so older catalog entries still sort reasonably.
    when: a.release_date
      ? Date.parse(a.release_date)
      : a.year != null
        ? Date.parse(`${a.year}-01-01`)
        : Number.NEGATIVE_INFINITY,
    name: (a.name ?? "").toLocaleLowerCase(),
    originalIndex: i,
  }));
  decorated.sort((x, y) => {
    if (sort === "alpha") {
      const cmp = x.name.localeCompare(y.name);
      if (cmp !== 0) return cmp;
    } else {
      // Numeric subtract works for both orders.
      const cmp = sort === "newest" ? y.when - x.when : x.when - y.when;
      if (cmp !== 0) return cmp;
    }
    // Stable tiebreak on the input order so re-sorting between two
    // equally-valued items doesn't reshuffle them.
    return x.originalIndex - y.originalIndex;
  });
  return decorated.map((d) => d.album);
}

export type ArtistSectionKey =
  | "top-tracks"
  | "albums"
  | "eps"
  | "compilations"
  | "appears-on"
  | "similar"
  | "videos"
  | "liked";

interface SectionMeta {
  title: string;
  /** Field on the artist payload to read from. Set to `null` for
   *  sections sourced from elsewhere (e.g. the user's library). */
  field: keyof ArtistData | null;
  /** Render shape: tracks → TrackList; videos → video grid; media →
   *  MediaCard grid (albums and artists); liked → hearted albums
   *  grid + hearted tracks list. Used by both the loading skeleton
   *  picker and the body dispatcher so the two stay in sync. */
  kind: "tracks" | "videos" | "media" | "liked";
}

const SECTIONS: Record<ArtistSectionKey, SectionMeta> = {
  "top-tracks": { title: "Popular", field: "top_tracks", kind: "tracks" },
  albums: { title: "Albums", field: "albums", kind: "media" },
  eps: { title: "EPs & Singles", field: "ep_singles", kind: "media" },
  compilations: {
    title: "Compilations",
    field: "compilations",
    kind: "media",
  },
  "appears-on": { title: "Appears on", field: "appears_on", kind: "media" },
  similar: { title: "Fans also like", field: "similar", kind: "media" },
  videos: { title: "Videos", field: "videos", kind: "videos" },
  // Liked content isn't in the artist payload — comes from the
  // user's library filtered by artist. The custom render path
  // (kind: "liked") shows hearted albums on top of hearted tracks
  // so both surfaces are reachable from one place. The page-level
  // h1 reads "Liked from [Artist]" (computed below); this `title`
  // is the bare fallback used by the loading skeleton.
  liked: { title: "Liked", field: null, kind: "liked" },
};

interface ArtistData {
  id: string;
  name: string;
  top_tracks: Track[];
  albums: Album[];
  ep_singles: Album[];
  compilations: Album[];
  appears_on: Album[];
  similar: Artist[];
  videos: Video[];
}

/**
 * Full-list drill-down for an artist-page section. The artist page
 * shows a single row + "View more" link that lands here.
 */
export function ArtistSection({ onDownload }: { onDownload: OnDownload }) {
  const { id = "", section = "albums" } = useParams<{
    id: string;
    section: string;
  }>();
  const { data: artist, loading, error } = useApi(() => api.artist(id), [id]);
  const meta = SECTIONS[section as ArtistSectionKey];
  // Liked source: pull from the user's library filtered to this
  // artist. Returns null while the library fetch is loading.
  const liked = useLikedByArtist(artist?.id);

  if (!meta) {
    return <ErrorView error={`Unknown artist section "${section}"`} />;
  }
  if (loading) {
    return (
      <div>
        <h1 className="mb-6 text-3xl font-bold tracking-tight">{meta.title}</h1>
        {meta.kind === "tracks" || meta.kind === "liked" ? (
          <TrackListSkeleton count={10} />
        ) : (
          <GridSkeleton count={12} />
        )}
      </div>
    );
  }
  if (error || !artist) {
    return <ErrorView error={error ?? "Artist not found"} />;
  }

  // Header for the liked-section concatenates the artist name into
  // the h1 instead of using the artist eyebrow above it. Reads as
  // "things you liked from this artist" — works whether the page
  // shows tracks, albums, or both, unlike the literal "Liked
  // Songs by [Artist]" which mismatches when albums are also
  // shown.
  const isLiked = meta.kind === "liked";
  const headerTitle = isLiked ? `Liked from ${artist.name}` : meta.title;

  return (
    <div>
      <div className="mb-6">
        {!isLiked && (
          <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {artist.name}
          </div>
        )}
        <h1
          className={
            isLiked
              ? "text-3xl font-bold tracking-tight"
              : "mt-1 text-3xl font-bold tracking-tight"
          }
        >
          {headerTitle}
        </h1>
      </div>
      {meta.kind === "liked" ? (
        <LikedBody
          tracks={liked?.tracks ?? []}
          albums={liked?.albums ?? []}
          onDownload={onDownload}
        />
      ) : (
        <SectionBody
          kind={meta.kind}
          items={
            ((artist as unknown as ArtistData)[
              meta.field as keyof ArtistData
            ] ?? []) as Track[] | Album[] | Artist[] | Video[]
          }
          onDownload={onDownload}
        />
      )}
    </div>
  );
}

/**
 * Liked-section body: hearted albums on top (when present), then a
 * track list. Empty state when the user has neither — though the
 * artist page only links here when at least one is non-empty, so
 * this is mostly defensive.
 */
function LikedBody({
  tracks,
  albums,
  onDownload,
}: {
  tracks: Track[];
  albums: Album[];
  onDownload: OnDownload;
}) {
  if (tracks.length === 0 && albums.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        Nothing liked from this artist yet.
      </p>
    );
  }
  return (
    <>
      {albums.length > 0 && (
        <div className="mb-10">
          <h2 className="mb-4 text-lg font-bold tracking-tight">Albums</h2>
          <Grid>
            {albums.map((a) => (
              <MediaCard key={a.id} item={a} onDownload={onDownload} />
            ))}
          </Grid>
        </div>
      )}
      {tracks.length > 0 && (
        <div>
          {albums.length > 0 && (
            <h2 className="mb-4 text-lg font-bold tracking-tight">Songs</h2>
          )}
          <TrackList
            tracks={tracks}
            onDownload={onDownload}
            numbered
            showAlbum
          />
        </div>
      )}
    </>
  );
}

function SectionBody({
  kind,
  items,
  onDownload,
}: {
  kind: SectionMeta["kind"];
  items: Track[] | Album[] | Artist[] | Video[];
  onDownload: OnDownload;
}) {
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">Nothing here yet.</p>;
  }
  if (kind === "tracks") {
    return (
      <TrackList
        tracks={items as Track[]}
        onDownload={onDownload}
        numbered
        showPlaycount
      />
    );
  }
  if (kind === "videos") {
    return <VideosGrid videos={items as Video[]} />;
  }
  // The "media" kind covers both album lists (albums, EPs,
  // compilations, appears-on) and artist lists ("similar"). Only
  // the album lists get the view toggle + sort — sorting artists
  // by release date is nonsense, and they're rare enough on this
  // page (only one section, "Fans also like") that the simpler
  // tile-only render is fine.
  const firstItemKind = (items as (Album | Artist)[])[0]?.kind ?? "album";
  if (firstItemKind === "album") {
    return (
      <AlbumSectionBody albums={items as Album[]} onDownload={onDownload} />
    );
  }
  return (
    <Grid>
      {(items as Artist[]).map((item) => (
        <MediaCard key={item.id} item={item} onDownload={onDownload} />
      ))}
    </Grid>
  );
}

/**
 * Album-shaped section with a view toggle (tiles ↔ list) and a
 * sort dropdown (newest / oldest / A–Z). Preferences are
 * persisted in localStorage so a user who picks list+alpha on
 * one artist sees the same setup on the next artist they open.
 */
function AlbumSectionBody({
  albums,
  onDownload,
}: {
  albums: Album[];
  onDownload: OnDownload;
}) {
  const [view, setView] = useState<ViewMode>(() => loadAlbumView());
  const [sort, setSort] = useState<AlbumSort>(() => loadAlbumSort());
  // Persist immediately on change so the next mount picks up the
  // same value. Cheap (localStorage writes are sync but tiny) and
  // avoids the user wondering why their preference reset.
  useEffect(() => {
    try {
      localStorage.setItem(ALBUM_VIEW_KEY, view);
    } catch {
      /* ignore quota / disabled storage */
    }
  }, [view]);
  useEffect(() => {
    try {
      localStorage.setItem(ALBUM_SORT_KEY, sort);
    } catch {
      /* ignore */
    }
  }, [sort]);

  const sorted = useMemo(() => sortAlbums(albums, sort), [albums, sort]);

  return (
    <div>
      <div className="mb-4 flex items-center justify-end gap-2">
        <AlbumSortMenu sort={sort} onSort={setSort} />
        <ViewToggle view={view} onChange={setView} />
      </div>
      {view === "grid" ? (
        <Grid>
          {sorted.map((album) => (
            <MediaCard key={album.id} item={album} onDownload={onDownload} />
          ))}
        </Grid>
      ) : (
        <div className="flex flex-col gap-0.5">
          {sorted.map((album) => (
            <MediaListRow key={album.id} item={album} onDownload={onDownload} />
          ))}
        </div>
      )}
    </div>
  );
}

function AlbumSortMenu({
  sort,
  onSort,
}: {
  sort: AlbumSort;
  onSort: (s: AlbumSort) => void;
}) {
  const label =
    sort === "newest" ? "Newest" : sort === "oldest" ? "Oldest" : "A–Z";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm">
          <List className="h-4 w-4" />
          {label}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Sort by</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onSort("newest")}>
          <span className={cn(sort === "newest" ? "text-primary" : "")}>
            Newest first
          </span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onSort("oldest")}>
          <span className={cn(sort === "oldest" ? "text-primary" : "")}>
            Oldest first
          </span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onSort("alpha")}>
          <span className={cn(sort === "alpha" ? "text-primary" : "")}>
            Alphabetical
          </span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function VideosGrid({ videos }: { videos: Video[] }) {
  const { open } = useVideoPlayer();
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
      {videos.map((v) => (
        <VideoCard
          key={v.id}
          video={v}
          onPlay={() => open(v, videos)}
          icon={VideoIcon}
        />
      ))}
    </div>
  );
}
