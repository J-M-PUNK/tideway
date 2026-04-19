import { Link, useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { DetailHero } from "@/components/DetailHero";
import { DownloadButton } from "@/components/DownloadButton";
import { HeartButton } from "@/components/HeartButton";
import { PlayAllButton } from "@/components/PlayAllButton";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { Grid, SectionHeader } from "@/components/Grid";
import { MediaCard } from "@/components/MediaCard";
import { HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import { formatDuration } from "@/lib/utils";

export function AlbumDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const { data: album, loading, error } = useApi(() => api.album(id), [id]);

  if (loading) {
    return (
      <div>
        <HeroSkeleton />
        <div className="mt-10">
          <TrackListSkeleton />
        </div>
      </div>
    );
  }
  if (error || !album) return <ErrorView error={error ?? "Album not found"} />;

  const artists = album.artists.map((a, i) => (
    <span key={a.id}>
      {i > 0 && <span className="text-muted-foreground"> · </span>}
      <Link to={`/artist/${a.id}`} className="font-semibold text-foreground hover:underline">
        {a.name}
      </Link>
    </span>
  ));

  return (
    <div>
      <DetailHero
        eyebrow="Album"
        title={album.name}
        cover={album.cover}
        meta={
          <div className="flex flex-wrap items-center gap-x-2">
            {artists}
            {album.year && <span>· {album.year}</span>}
            <span>
              · {album.num_tracks} tracks · {formatDuration(album.duration)}
            </span>
          </div>
        }
        actions={
          <>
            <PlayAllButton tracks={album.tracks} />
            <DownloadButton
              kind="album"
              id={album.id}
              onPick={onDownload}
              size="lg"
              label="Download album"
            />
            <HeartButton kind="album" id={album.id} />
          </>
        }
      />
      <div className="mt-8">
        <TrackList tracks={album.tracks} onDownload={onDownload} showAlbum={false} />
      </div>

      {album.similar.length > 0 && (
        <>
          <SectionHeader title="Similar albums" />
          <Grid>
            {album.similar.map((a) => (
              <MediaCard key={a.id} item={a} onDownload={onDownload} />
            ))}
          </Grid>
        </>
      )}
    </div>
  );
}
