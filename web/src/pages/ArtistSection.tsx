import { useParams } from "react-router-dom";
import { Video as VideoIcon } from "lucide-react";
import { api } from "@/api/client";
import type { Album, Artist, Track, Video } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
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
  | "videos";

interface SectionMeta {
  title: string;
  field: keyof ArtistData;
  /** Render shape: tracks → TrackList; videos → video grid; media →
   *  MediaCard grid (albums and artists). Used by both the loading
   *  skeleton picker and the body dispatcher so the two stay in sync. */
  kind: "tracks" | "videos" | "media";
}

const SECTIONS: Record<ArtistSectionKey, SectionMeta> = {
  "top-tracks": { title: "Popular", field: "top_tracks", kind: "tracks" },
  albums: { title: "Albums", field: "albums", kind: "media" },
  eps: { title: "EPs & Singles", field: "ep_singles", kind: "media" },
  "appears-on": { title: "Appears on", field: "appears_on", kind: "media" },
  similar: { title: "Fans also like", field: "similar", kind: "media" },
  videos: { title: "Videos", field: "videos", kind: "videos" },
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

  if (!meta) {
    return <ErrorView error={`Unknown artist section "${section}"`} />;
  }
  if (loading) {
    return (
      <div>
        <h1 className="mb-6 text-3xl font-bold tracking-tight">{meta.title}</h1>
        {meta.kind === "tracks" ? (
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

  const items = (artist as unknown as ArtistData)[meta.field] as
    | Track[]
    | Album[]
    | Artist[]
    | Video[];

  return (
    <div>
      <div className="mb-6">
        <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {artist.name}
        </div>
        <h1 className="mt-1 text-3xl font-bold tracking-tight">{meta.title}</h1>
      </div>
      <SectionBody kind={meta.kind} items={items} onDownload={onDownload} />
    </div>
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
