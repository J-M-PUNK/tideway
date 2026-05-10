import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Music,
  Radio,
  Settings as SettingsIcon,
  User as UserIcon,
} from "lucide-react";
import { api } from "@/api/client";
import { queryKeys } from "@/api/queryKeys";
import type { LastFmChartArtist } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/EmptyState";
import { Skeleton, TrackListSkeleton } from "@/components/Skeletons";
import { useToast } from "@/components/toast";
import { preseedSpotifyPlaycounts } from "@/hooks/useSpotifyEnrichment";
import { useTidalArt } from "@/hooks/useTidalArt";
import { useTidalArtistId } from "@/hooks/useTidalResolve";
import { ChartsNav } from "@/components/ChartsNav";
import { TrackList } from "@/components/TrackList";
import { cn, imageProxy } from "@/lib/utils";

/**
 * Global popularity page powered by Last.fm's `chart.*` endpoints.
 *
 * Different perspective from Tidal's editorial charts:
 *   · Tidal Top Charts = curated by Tidal's team, reflects what they
 *     promote.
 *   · Last.fm Popular  = aggregated from what Last.fm's entire
 *     listening community actually plays. No editorial filter.
 *
 * Two tabs: Artists and Tracks.
 *   - Artists → clicking a card navigates to the Tidal artist page
 *     (lazy-resolved). No auto-play — matches how the rest of the app
 *     treats artist cards.
 *   - Tracks → each Last.fm entry is resolved to a real Tidal Track
 *     and rendered via the standard `TrackList`, so the row looks and
 *     behaves identically to an album or artist-top-tracks list
 *     (clickable artist, clickable album, duration, context menu).
 */

type Tab = "artists" | "tracks";

export function PopularPage({ onDownload }: { onDownload: OnDownload }) {
  const [tab, setTab] = useState<Tab>("artists");
  // Checking credentials (not connection) — charts work with just an
  // api_key, no Last.fm session required.
  const [hasCredentials, setHasCredentials] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.lastfm
      .status()
      .then((s) => !cancelled && setHasCredentials(s.has_credentials))
      .catch(() => !cancelled && setHasCredentials(false));
    return () => {
      cancelled = true;
    };
  }, []);

  if (hasCredentials === null) {
    return (
      <div>
        <ChartsNav />
        <Skeleton className="mb-8 h-9 w-64" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (!hasCredentials) {
    return (
      <div>
        <ChartsNav />
        <EmptyState
          icon={Radio}
          title="Connect Last.fm to browse global charts"
          description="Charts aggregated from what Last.fm's entire listening community plays — a different perspective from Tidal's editorial picks."
          action={
            <Button asChild variant="secondary" size="sm">
              <Link to="/settings">
                <SettingsIcon className="h-4 w-4" /> Go to Settings
              </Link>
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div>
      <ChartsNav />
      <TabBar tab={tab} onChange={setTab} />
      <div className="mt-6">
        {tab === "artists" && <ChartArtists />}
        {tab === "tracks" && <ChartTracks onDownload={onDownload} />}
      </div>
    </div>
  );
}

function TabBar({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  return (
    <div className="inline-flex gap-1 rounded-full border border-border/50 bg-card/40 p-1">
      <TabButton active={tab === "artists"} onClick={() => onChange("artists")}>
        <UserIcon className="h-3.5 w-3.5" /> Artists
      </TabButton>
      <TabButton active={tab === "tracks"} onClick={() => onChange("tracks")}>
        <Music className="h-3.5 w-3.5" /> Tracks
      </TabButton>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-full px-4 py-1.5 text-xs font-semibold transition-colors",
        active
          ? "bg-foreground text-background"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Artists tab — card grid, click = navigate to artist page.
// ---------------------------------------------------------------------------

function ChartArtists() {
  const { data, loading } = useApi(() => api.lastfm.chartTopArtists(50), [], {
    cacheKey: queryKeys.popularArtists,
  });
  if (loading && !data) return <ArtistGridSkeleton />;
  if (!data || data.length === 0) {
    return (
      <EmptyState
        icon={UserIcon}
        title="No data"
        description="Last.fm didn't return any results."
      />
    );
  }
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 min-[1920px]:grid-cols-7 min-[2400px]:grid-cols-8">
      {data.map((a, i) => (
        <ArtistChartCard key={`${a.name}-${i}`} rank={i + 1} artist={a} />
      ))}
    </div>
  );
}

function ArtistChartCard({
  rank,
  artist,
}: {
  rank: number;
  artist: LastFmChartArtist;
}) {
  const navigate = useNavigate();
  const toast = useToast();
  const tidalId = useTidalArtistId(artist.name);
  const tidalArt = useTidalArt("artist", artist.name);
  const img = imageProxy(artist.image || tidalArt || undefined);

  const onClick = async () => {
    // Prefer the cached Tidal id from the background hook. If it isn't
    // ready yet (user clicked before the search resolved), do the
    // lookup inline so there's no visible stall.
    if (tidalId) {
      navigate(`/artist/${tidalId}`);
      return;
    }
    try {
      const res = await api.search(artist.name, 10);
      const exact = res.artists.find(
        (a) => a.name.toLowerCase() === artist.name.toLowerCase(),
      );
      const match = exact ?? res.artists[0];
      if (!match) {
        toast.show({
          kind: "info",
          title: "Not on Tidal",
          description: `Couldn't find ${artist.name}.`,
        });
        return;
      }
      navigate(`/artist/${match.id}`);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't open artist",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <button
      onClick={onClick}
      className="group relative flex flex-col gap-3 rounded-lg bg-card p-4 text-left transition-colors hover:bg-accent"
    >
      <span className="absolute left-5 top-5 z-10 flex h-6 min-w-6 items-center justify-center rounded-full bg-background/80 px-1.5 text-[10px] font-bold tabular-nums text-foreground shadow">
        {rank}
      </span>
      <div className="relative aspect-square overflow-hidden rounded-full bg-secondary">
        {img ? (
          <img
            src={img}
            alt=""
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <UserIcon className="h-10 w-10" />
          </div>
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{artist.name}</div>
        <div className="text-xs text-muted-foreground">
          {formatCompact(artist.listeners)} listeners (all-time)
        </div>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Tracks tab — resolved to Tidal tracks, rendered via the standard
// TrackList so a chart row behaves like a track anywhere else in the
// app (clickable artist/album, duration, context menu).
// ---------------------------------------------------------------------------

function ChartTracks({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading } = useApi(
    () => api.lastfm.chartTopTracksResolved(50),
    [],
    { cacheKey: queryKeys.popularTracks },
  );

  // Preseed Spotify playcounts in one bounded-pool server call so the
  // 50 per-row hooks don't each fire a browser request and hit
  // Spotify's throttle. We send title + primary artist alongside the
  // ISRC so the server can fall back to a fuzzy search when Spotify
  // doesn't have the exact ISRC (covers feature-version ISRCs and
  // fresh releases). Re-runs whenever data identity changes so an
  // SWR revalidate that brings new chart tracks gets fresh
  // playcounts too.
  useEffect(() => {
    if (!data || data.length === 0) return;
    let cancelled = false;
    const lookup = data
      .filter((t) => !!t.isrc)
      .map((t) => ({
        isrc: t.isrc as string,
        title: t.name,
        artist: t.artists[0]?.name ?? "",
      }));
    if (lookup.length === 0) return;
    // `refresh: true` drops stale null/zero cache entries so chart
    // tracks that missed their playcount on an earlier visit (Spotify
    // throttle, release-week zero) get a retry instead of sitting dark.
    api.spotify
      .trackPlaycounts(lookup, { refresh: true })
      .then(({ playcounts }) => {
        if (!cancelled) preseedSpotifyPlaycounts(playcounts);
      })
      .catch(() => {
        /* fine — per-row hooks will fall back to their own fetch */
      });
    return () => {
      cancelled = true;
    };
  }, [data]);

  if (loading && !data) return <TrackListSkeleton />;
  if (!data || data.length === 0) {
    return (
      <EmptyState
        icon={Music}
        title="No data"
        description="Last.fm didn't return any results."
      />
    );
  }
  return (
    <TrackList tracks={data} onDownload={onDownload} numbered showPlaycount />
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatCompact(n: number): string {
  if (n < 1000) return n.toLocaleString();
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000)
    return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

function ArtistGridSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 min-[1920px]:grid-cols-7 min-[2400px]:grid-cols-8">
      {Array.from({ length: 12 }).map((_, i) => (
        <Skeleton key={i} className="aspect-square w-full" />
      ))}
    </div>
  );
}
