import { useParams } from "react-router-dom";
import { Video as VideoIcon } from "lucide-react";
import { api } from "@/api/client";
import type { Album, Artist, Track, Video } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { useLikedByArtist } from "@/hooks/useLikedByArtist";
import { useVideoPlayer } from "@/hooks/useVideoPlayer";
import { Grid } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import { VideoCard } from "@/components/VideoCard";

export type ArtistSectionKey =
  | "top-tracks"
  | "albums"
  | "eps"
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
  return (
    <Grid>
      {(items as (Album | Artist)[]).map((item) => (
        <MediaCard key={item.id} item={item} onDownload={onDownload} />
      ))}
    </Grid>
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
