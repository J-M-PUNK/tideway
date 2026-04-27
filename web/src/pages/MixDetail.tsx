import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { useTrackPrefetch } from "@/hooks/useTrackPrefetch";
import { AddTracksToPlaylistButton } from "@/components/AddTracksToPlaylistButton";
import { CollectionOverflowMenu } from "@/components/CollectionOverflowMenu";
import { DetailHero } from "@/components/DetailHero";
import { PlayAllButton } from "@/components/PlayAllButton";
import { ShareButton } from "@/components/ShareButton";
import { ShuffleButton } from "@/components/ShuffleButton";
import { TrackList } from "@/components/TrackList";
import { ErrorView } from "@/components/ErrorView";
import { HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";

export function MixDetail({ onDownload }: { onDownload: OnDownload }) {
  const { id = "" } = useParams();
  const { data: mix, loading, error } = useApi(() => api.mix(id), [id]);
  const [shuffleIntent, setShuffleIntent] = useState(false);
  // Warm the stream-manifest cache for every track on this mix so
  // the next click skips the Tidal playbackinfo round-trip.
  const { prefetchMany } = useTrackPrefetch();
  useEffect(() => {
    if (mix?.tracks?.length) prefetchMany(mix.tracks.map((t) => t.id));
  }, [mix, prefetchMany]);

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

  // Mixes don't expose their own share URL — synthesize one from the
  // ID so the share button still works. Matches the `/mix/:id` routes
  // Tidal itself uses on the web player.
  const shareUrl = `https://tidal.com/browse/mix/${mix.id}`;

  return (
    <div>
      <DetailHero
        eyebrow="Mix"
        title={mix.name}
        cover={mix.cover}
        meta={
          <div className="flex flex-col gap-2">
            {mix.subtitle && (
              <p className="text-muted-foreground">{mix.subtitle}</p>
            )}
            <span>{mix.tracks.length} tracks</span>
          </div>
        }
        actions={
          mix.tracks.length > 0 ? (
            <>
              <PlayAllButton
                tracks={mix.tracks}
                source={{ type: "MIX", id: mix.id }}
                shuffleIntent={shuffleIntent}
              />
              <ShuffleButton
                value={shuffleIntent}
                onChange={setShuffleIntent}
              />
              <div className="ml-auto flex items-center gap-6">
                <AddTracksToPlaylistButton tracks={mix.tracks} />
                <ShareButton shareUrl={shareUrl} />
                <CollectionOverflowMenu tracks={mix.tracks} />
              </div>
            </>
          ) : null
        }
      />
      <div className="mt-8">
        <TrackList
          tracks={mix.tracks}
          onDownload={onDownload}
          source={{ type: "MIX", id: mix.id }}
        />
      </div>
    </div>
  );
}
