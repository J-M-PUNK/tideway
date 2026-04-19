import { useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { DetailHero } from "@/components/DetailHero";
import { PlayAllButton } from "@/components/PlayAllButton";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";

export function MixDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const { data: mix, loading, error } = useApi(() => api.mix(id), [id]);

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
  if (error || !mix) return <ErrorView error={error ?? "Mix not found"} />;

  return (
    <div>
      <DetailHero
        eyebrow="Mix"
        title={mix.name}
        cover={mix.cover}
        meta={
          <div className="flex flex-col gap-2">
            {mix.subtitle && <p className="text-muted-foreground">{mix.subtitle}</p>}
            <span>{mix.tracks.length} tracks</span>
          </div>
        }
        actions={mix.tracks.length > 0 ? <PlayAllButton tracks={mix.tracks} /> : null}
      />
      <div className="mt-8">
        <TrackList tracks={mix.tracks} onDownload={onDownload} />
      </div>
    </div>
  );
}
