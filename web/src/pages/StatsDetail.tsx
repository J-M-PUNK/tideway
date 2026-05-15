import { useParams, useSearchParams } from "react-router-dom";
import { Heart, Music, User as UserIcon } from "lucide-react";
import { api } from "@/api/client";
import { queryKeys } from "@/api/queryKeys";
import { useApi } from "@/hooks/useApi";
import type {
  LastFmLovedTrack,
  LastFmPeriod,
  LastFmTopAlbum,
  LastFmTopArtist,
  LastFmTopTrack,
} from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { Skeleton } from "@/components/Skeletons";
import { VirtualList } from "@/components/VirtualList";
import {
  AlbumCard,
  ArtistCard,
  LovedRow,
  PeriodPicker,
  TrackRow,
} from "@/pages/StatsPage";

const VALID_PERIODS: LastFmPeriod[] = [
  "7day",
  "1month",
  "3month",
  "6month",
  "12month",
  "overall",
];

function parsePeriod(raw: string | null): LastFmPeriod {
  if (raw && (VALID_PERIODS as string[]).includes(raw)) {
    return raw as LastFmPeriod;
  }
  return "1month";
}

/**
 * Full-list drill-down for a Stats section: artists, tracks, albums,
 * or loved. The Stats page's "View more" links point here with the
 * kind in the URL path and the current period as a query param so
 * the drill-down shows the same time range the user was looking at.
 */
export function StatsDetail() {
  const { kind = "artists" } = useParams<{ kind?: string }>();
  const [params, setParams] = useSearchParams();
  const period = parsePeriod(params.get("period"));

  const setPeriod = (p: LastFmPeriod) => {
    const next = new URLSearchParams(params);
    next.set("period", p);
    setParams(next, { replace: true });
  };

  const title = TITLES[kind] ?? "Stats";

  return (
    <div>
      <h1 className="mb-6 text-3xl font-bold tracking-tight">{title}</h1>
      {kind !== "loved" && (
        <div className="mb-6">
          <PeriodPicker period={period} onChange={setPeriod} />
        </div>
      )}
      {kind === "artists" && <ArtistsList period={period} />}
      {kind === "tracks" && <TracksList period={period} />}
      {kind === "albums" && <AlbumsList period={period} />}
      {kind === "loved" && <LovedList />}
    </div>
  );
}

const TITLES: Record<string, string> = {
  artists: "Top artists",
  tracks: "Top tracks",
  albums: "Top albums",
  loved: "Loved tracks",
};

function ArtistsList({ period }: { period: LastFmPeriod }) {
  const { data, loading } = useApi<LastFmTopArtist[]>(
    () => api.lastfm.topArtists(period, 200),
    [period],
    { cacheKey: queryKeys.statsTopArtists(period, 200) },
  );
  if (loading && !data) return <GridSkeleton />;
  if (!data || data.length === 0) {
    return (
      <EmptyState
        icon={UserIcon}
        title="No data"
        description="No plays in this range yet."
      />
    );
  }
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 min-[1920px]:grid-cols-7 min-[2400px]:grid-cols-8">
      {data.map((a, i) => (
        <ArtistCard key={`${a.name}-${i}`} rank={i + 1} artist={a} />
      ))}
    </div>
  );
}

function TracksList({ period }: { period: LastFmPeriod }) {
  const { data, loading } = useApi<LastFmTopTrack[]>(
    () => api.lastfm.topTracks(period, 200),
    [period],
    { cacheKey: queryKeys.statsTopTracks(period, 200) },
  );
  if (loading && !data) return <ListSkeleton />;
  if (!data || data.length === 0) {
    return (
      <EmptyState
        icon={Music}
        title="No data"
        description="No plays in this range yet."
      />
    );
  }
  return (
    <VirtualList
      count={data.length}
      estimateSize={64}
      rowKey={(i) => `${data[i].name}-${data[i].artist}-${i}`}
      renderRow={(i) => <TrackRow rank={i + 1} track={data[i]} />}
    />
  );
}

function AlbumsList({ period }: { period: LastFmPeriod }) {
  const { data, loading } = useApi<LastFmTopAlbum[]>(
    () => api.lastfm.topAlbums(period, 200),
    [period],
    { cacheKey: queryKeys.statsTopAlbums(period, 200) },
  );
  if (loading && !data) return <GridSkeleton />;
  if (!data || data.length === 0) {
    return (
      <EmptyState
        icon={Music}
        title="No data"
        description="No plays in this range yet."
      />
    );
  }
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 min-[1920px]:grid-cols-7 min-[2400px]:grid-cols-8">
      {data.map((a, i) => (
        <AlbumCard key={`${a.name}-${a.artist}-${i}`} rank={i + 1} album={a} />
      ))}
    </div>
  );
}

function LovedList() {
  const { data, loading } = useApi<LastFmLovedTrack[]>(
    () => api.lastfm.lovedTracks(500),
    [],
    { cacheKey: queryKeys.statsLoved(500) },
  );
  if (loading && !data) return <ListSkeleton />;
  if (!data || data.length === 0) {
    return (
      <EmptyState
        icon={Heart}
        title="No loved tracks"
        description="Heart a track on Last.fm and it shows up here."
      />
    );
  }
  return (
    <VirtualList
      count={data.length}
      estimateSize={64}
      rowKey={(i) => `${data[i].name}-${data[i].artist}-${i}`}
      renderRow={(i) => <LovedRow row={data[i]} />}
    />
  );
}

function GridSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 min-[1920px]:grid-cols-7 min-[2400px]:grid-cols-8">
      {Array.from({ length: 18 }).map((_, i) => (
        <Skeleton key={i} className="aspect-square w-full" />
      ))}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: 12 }).map((_, i) => (
        <Skeleton key={i} className="h-14 w-full" />
      ))}
    </div>
  );
}
