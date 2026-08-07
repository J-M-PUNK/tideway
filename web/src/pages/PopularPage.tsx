import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Music,
  Radio,
  Settings as SettingsIcon,
  User as UserIcon,
} from "lucide-react";
import { api } from "@/api/client";
import type { LastFmChartArtist, Track } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { useInfinitePages } from "@/hooks/useInfinitePages";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/EmptyState";
import { Skeleton, TrackListSkeleton } from "@/components/Skeletons";
import { useToast } from "@/components/toast";
import { preseedSpotifyPlaycounts } from "@/hooks/useSpotifyEnrichment";
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
        <Skeleton className="mb-8 h-9 w-64" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (!hasCredentials) {
    return (
      <div>
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
  const { items, hasMore, loading, error, sentinelRef } = useInfinitePages<
    LastFmChartArtist,
    { items: LastFmChartArtist[]; has_more: boolean }
  >(
    (offset) => api.lastfm.chartTopArtistsResolved({ offset, limit: 18 }),
    18,
    [],
  );

  if (loading && items.length === 0) return <ArtistGridSkeleton />;
  if (items.length === 0) {
    return (
      <EmptyState
        icon={UserIcon}
        title={error ? "Couldn't load" : "No data"}
        description={
          error
            ? `Couldn't load chart artists: ${error}`
            : "Last.fm didn't return any results."
        }
      />
    );
  }
  return (
    <>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 min-[1920px]:grid-cols-7 min-[2400px]:grid-cols-8">
        {items.map((a, i) => (
          <ArtistChartCard key={`${a.name}-${i}`} rank={i + 1} artist={a} />
        ))}
      </div>
      {hasMore && (
        <div ref={sentinelRef} className="mt-4" aria-hidden>
          <ArtistGridSkeleton />
        </div>
      )}
    </>
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
  const img = imageProxy(artist.image || artist.tidal_picture || undefined);

  const onClick = async () => {
    // The id is resolved server-side with the page. Only fall back to an
    // inline lookup when it came back null (a transient miss, or an
    // artist that genuinely didn't resolve) — never a burst of searches
    // on mount.
    if (artist.tidal_id) {
      navigate(`/artist/${artist.tidal_id}`);
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
  const { items, hasMore, loading, error, sentinelRef } = useInfinitePages<
    Track,
    { items: Track[]; has_more: boolean }
  >(
    (offset) => api.lastfm.chartTopTracksResolved({ offset, limit: 18 }),
    18,
    [],
  );

  // Preseed Spotify playcounts for each newly-loaded page in one
  // bounded-pool server call, so per-row hooks don't each fire a browser
  // request and hit Spotify's throttle. Only the fresh slice is looked up
  // (the count reset handles the list clearing on reload). `refresh: true`
  // retries entries that missed their playcount before.
  const preseeded = useRef(0);
  useEffect(() => {
    if (items.length < preseeded.current) preseeded.current = 0;
    if (items.length <= preseeded.current) return;
    const fresh = items.slice(preseeded.current);
    preseeded.current = items.length;
    const lookup = fresh
      .filter((t) => !!t.isrc)
      .map((t) => ({
        isrc: t.isrc as string,
        title: t.name,
        artist: t.artists[0]?.name ?? "",
      }));
    if (lookup.length === 0) return;
    let cancelled = false;
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
  }, [items]);

  if (loading && items.length === 0) return <TrackListSkeleton />;
  if (items.length === 0) {
    // An empty resolved list almost always means Tidal couldn't resolve
    // the chart entries (Last.fm's chart JSON is basically never empty),
    // so blame Tidal unless the request itself errored.
    const description = error
      ? `Couldn't load chart tracks: ${error}`
      : "Last.fm had chart entries but none of them resolved to Tidal — likely a temporary Tidal hiccup. Try again in a few seconds.";
    return (
      <EmptyState
        icon={Music}
        title={error ? "Couldn't load" : "No matches"}
        description={description}
      />
    );
  }
  return (
    <>
      <TrackList
        tracks={items}
        onDownload={onDownload}
        numbered
        showPlaycount
      />
      {hasMore && (
        <div ref={sentinelRef} className="mt-2" aria-hidden>
          <TrackListSkeleton />
        </div>
      )}
    </>
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
