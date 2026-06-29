import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { queryKeys } from "@/api/queryKeys";
import { useTrackPrefetch } from "@/hooks/useTrackPrefetch";
import { AddToLibraryButton } from "@/components/AddToLibraryButton";
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
  const {
    data: mix,
    loading,
    error,
  } = useApi(() => api.mix(id), [id], {
    cacheKey: queryKeys.mix(id),
  });
  const [shuffleIntent, setShuffleIntent] = useState(false);
  // Warm the stream-manifest cache for the first handful of tracks so
  // the next click skips the Tidal playbackinfo round-trip. Capped at
  // 10: mixes run 50-100 tracks, and prefetching all of them fires 3
  // Tidal API calls each at once, enough to trigger rate-limiting or a
  // temporary account suspension. Hover-prefetch covers the rest.
  const { prefetchMany } = useTrackPrefetch();
  useEffect(() => {
    if (mix?.tracks?.length)
      prefetchMany(mix.tracks.slice(0, 10).map((t) => t.id));
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
                <AddToLibraryButton kind="mix" id={mix.id} />
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
