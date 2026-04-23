import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  BarChart3,
  Calendar,
  ExternalLink,
  Heart,
  Music,
  Play,
  Radio,
  Settings as SettingsIcon,
  User as UserIcon,
} from "lucide-react";
import { api } from "@/api/client";
import type {
  LastFmLovedTrack,
  LastFmPeriod,
  LastFmTopAlbum,
  LastFmTopArtist,
  LastFmTopTrack,
  LastFmUserInfo,
  Track,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/EmptyState";
import { LastFmActivityChart } from "@/components/LastFmActivityChart";
import { Skeleton } from "@/components/Skeletons";
import { useToast } from "@/components/toast";
import { usePlayerActions } from "@/hooks/PlayerContext";
import { useTidalArt } from "@/hooks/useTidalArt";
import { cn, imageProxy } from "@/lib/utils";

/**
 * Last.fm-powered listening dashboard. Mirrors the shape of stats.fm /
 * last.fm's own profile page: a header card with lifetime counts, a
 * time-range picker, and three ranked grids (artists / albums) plus a
 * numbered list (tracks). Loved tracks live in their own section since
 * they're curated, not frequency-sorted.
 *
 * Clicking any item does a lazy Tidal search → play — same pattern as
 * HistoryPage, so 50 rows don't cost 50 Tidal searches on page load.
 */

const PERIOD_LABELS: Record<LastFmPeriod, string> = {
  "7day": "Last 7 days",
  "1month": "Last month",
  "3month": "3 months",
  "6month": "6 months",
  "12month": "Last year",
  overall: "All time",
};
const PERIOD_ORDER: LastFmPeriod[] = [
  "7day",
  "1month",
  "3month",
  "6month",
  "12month",
  "overall",
];

export function StatsPage() {
  const [connected, setConnected] = useState<boolean | null>(null);
  const [user, setUser] = useState<LastFmUserInfo | null>(null);
  const [period, setPeriod] = useState<LastFmPeriod>("1month");

  // Probe status once. If Last.fm isn't connected the whole page
  // short-circuits to an explainer with a "Go to Settings" button —
  // no requests hit the Last.fm endpoints until auth is set up.
  useEffect(() => {
    let cancelled = false;
    api.lastfm
      .status()
      .then((s) => {
        if (cancelled) return;
        setConnected(s.connected);
        if (s.connected) {
          api.lastfm
            .userInfo()
            .then((u) => !cancelled && setUser(u))
            .catch(() => {
              /* header falls back to a monogram */
            });
        }
      })
      .catch(() => {
        if (!cancelled) setConnected(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (connected === null) {
    return (
      <div>
        <Skeleton className="mb-8 h-9 w-64" />
        <Skeleton className="mb-8 h-24 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }

  if (!connected) {
    return (
      <div>
        <h1 className="mb-6 flex items-center gap-3 text-3xl font-bold tracking-tight">
          <BarChart3 className="h-7 w-7" /> Stats
        </h1>
        <EmptyState
          icon={Radio}
          title="Connect Last.fm to see your stats"
          description="Listening stats come from Last.fm — plays from this app and any other client you use (Tidal's desktop app, Plexamp, browser extensions) all roll up into one view."
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
      <UserHeader user={user} />
      <p className="mb-6 text-xs text-muted-foreground">
        Stats provided by Last.fm. Numbers reflect every scrobble from
        your connected Last.fm account, including plays from this app
        and from any other client tied to the same account.
      </p>
      <LastFmActivityChart period={period} />
      <PeriodPicker period={period} onChange={setPeriod} />
      <div className="mt-8 flex flex-col gap-10">
        <TopArtistsSection period={period} />
        <TopTracksSection period={period} />
        <TopAlbumsSection period={period} />
        <LovedTracksSection />
      </div>
    </div>
  );
}

function UserHeader({ user }: { user: LastFmUserInfo | null }) {
  const avatar = user?.image ? imageProxy(user.image) : undefined;
  const initial = (user?.username || "?").trim().charAt(0).toUpperCase();
  const memberSince = user?.registered_at
    ? new Date(user.registered_at * 1000).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
      })
    : null;

  return (
    <div className="mb-8">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Your stats
      </div>
      <div className="flex flex-wrap items-center gap-5 rounded-lg border border-border/50 bg-card/40 p-5">
        <div className="flex h-16 w-16 flex-shrink-0 items-center justify-center overflow-hidden rounded-full bg-secondary text-2xl font-bold">
          {avatar ? (
            <img src={avatar} alt="" className="h-full w-full object-cover" />
          ) : (
            <span>{initial}</span>
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            {user?.realname || user?.username || "…"}
            {user?.url && (
              <a
                href={user.url}
                target="_blank"
                rel="noreferrer"
                className="text-muted-foreground hover:text-foreground"
                title="Open Last.fm profile"
              >
                <ExternalLink className="h-4 w-4" />
              </a>
            )}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {user?.username ? `@${user.username}` : ""}
            {memberSince && (
              <>
                {user?.username && " · "}
                <Calendar className="mr-1 inline h-3 w-3" />
                Since {memberSince}
              </>
            )}
          </div>
        </div>
        {user && (
          <div className="flex flex-wrap gap-6">
            <Stat label="Plays" value={user.playcount} />
            <Stat label="Artists" value={user.artist_count} />
            <Stat label="Albums" value={user.album_count} />
            <Stat label="Tracks" value={user.track_count} />
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="text-2xl font-bold tabular-nums">
        {value.toLocaleString()}
      </div>
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
    </div>
  );
}

function PeriodPicker({
  period,
  onChange,
}: {
  period: LastFmPeriod;
  onChange: (p: LastFmPeriod) => void;
}) {
  return (
    <div className="mb-2 inline-flex flex-wrap gap-1 rounded-full border border-border/50 bg-card/40 p-1">
      {PERIOD_ORDER.map((p) => (
        <button
          key={p}
          onClick={() => onChange(p)}
          className={cn(
            "rounded-full px-3 py-1.5 text-xs font-semibold transition-colors",
            period === p
              ? "bg-foreground text-background"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {PERIOD_LABELS[p]}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section components
// ---------------------------------------------------------------------------

// Collapsed track lists show 5, matching the ArtistDetail
// "Popular" section. Grids cap at the actual column count at the
// current viewport (see useGridCols) so "one row" stays one row
// regardless of window width — a fixed count like 6 spills into a
// half second row at the xl (5-col) breakpoint.
const LIST_COLLAPSED_COUNT = 5;

/**
 * Number of cards per row on the stat grids at the current viewport
 * width. Mirrors the Tailwind breakpoints on those grids exactly:
 *   base  <640   → 2
 *   sm    ≥640   → 3
 *   lg    ≥1024  → 4
 *   xl    ≥1280  → 5
 *   2xl   ≥1536  → 6
 * Updates on window resize so the collapsed view always fills
 * exactly one row.
 */
function useGridCols(): number {
  const compute = () => {
    if (typeof window === "undefined") return 6;
    const w = window.innerWidth;
    if (w >= 1536) return 6;
    if (w >= 1280) return 5;
    if (w >= 1024) return 4;
    if (w >= 640) return 3;
    return 2;
  };
  const [cols, setCols] = useState(compute);
  useEffect(() => {
    const onResize = () => setCols(compute());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return cols;
}

function ViewMoreButton({
  expanded,
  onToggle,
}: {
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      className="mt-4 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
    >
      {expanded ? "View less" : "View more"}
    </button>
  );
}

function TopArtistsSection({ period }: { period: LastFmPeriod }) {
  const [data, setData] = useState<LastFmTopArtist[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const cols = useGridCols();
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setExpanded(false);
    api.lastfm
      .topArtists(period, 30)
      .then((rows) => !cancelled && setData(rows))
      .catch(() => !cancelled && setData([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [period]);
  const shown = data && !expanded ? data.slice(0, cols) : data ?? [];
  return (
    <Section title="Top artists" subtitle="Ranked by plays">
      {loading && !data ? (
        <GridSkeleton />
      ) : !data || data.length === 0 ? (
        <EmptyState icon={UserIcon} title="No data" description="No plays in this range yet." />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
            {shown.map((a, i) => (
              <ArtistCard key={`${a.name}-${i}`} rank={i + 1} artist={a} />
            ))}
          </div>
          {data.length > cols && (
            <ViewMoreButton
              expanded={expanded}
              onToggle={() => setExpanded((v) => !v)}
            />
          )}
        </>
      )}
    </Section>
  );
}

function TopTracksSection({ period }: { period: LastFmPeriod }) {
  const [data, setData] = useState<LastFmTopTrack[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setExpanded(false);
    api.lastfm
      .topTracks(period, 50)
      .then((rows) => !cancelled && setData(rows))
      .catch(() => !cancelled && setData([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [period]);
  const shown =
    data && !expanded ? data.slice(0, LIST_COLLAPSED_COUNT) : data ?? [];
  return (
    <Section title="Top tracks" subtitle="Ranked by plays">
      {loading && !data ? (
        <ListSkeleton />
      ) : !data || data.length === 0 ? (
        <EmptyState icon={Music} title="No data" description="No plays in this range yet." />
      ) : (
        <>
          <div className="flex flex-col gap-1">
            {shown.map((t, i) => (
              <TrackRow key={`${t.name}-${t.artist}-${i}`} rank={i + 1} track={t} />
            ))}
          </div>
          {data.length > LIST_COLLAPSED_COUNT && (
            <ViewMoreButton
              expanded={expanded}
              onToggle={() => setExpanded((v) => !v)}
            />
          )}
        </>
      )}
    </Section>
  );
}

function TopAlbumsSection({ period }: { period: LastFmPeriod }) {
  const [data, setData] = useState<LastFmTopAlbum[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const cols = useGridCols();
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setExpanded(false);
    api.lastfm
      .topAlbums(period, 30)
      .then((rows) => !cancelled && setData(rows))
      .catch(() => !cancelled && setData([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [period]);
  const shown = data && !expanded ? data.slice(0, cols) : data ?? [];
  return (
    <Section title="Top albums" subtitle="Ranked by plays">
      {loading && !data ? (
        <GridSkeleton />
      ) : !data || data.length === 0 ? (
        <EmptyState icon={Music} title="No data" description="No plays in this range yet." />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
            {shown.map((a, i) => (
              <AlbumCard key={`${a.name}-${a.artist}-${i}`} rank={i + 1} album={a} />
            ))}
          </div>
          {data.length > cols && (
            <ViewMoreButton
              expanded={expanded}
              onToggle={() => setExpanded((v) => !v)}
            />
          )}
        </>
      )}
    </Section>
  );
}

function LovedTracksSection() {
  const [data, setData] = useState<LastFmLovedTrack[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.lastfm
      .lovedTracks(30)
      .then((rows) => !cancelled && setData(rows))
      .catch(() => !cancelled && setData([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);
  // Suppress the section entirely when the user has never loved a
  // track — empty Loved lists are the norm, not a "no data" state
  // worth flagging.
  if (!loading && (!data || data.length === 0)) return null;
  const shown =
    data && !expanded ? data.slice(0, LIST_COLLAPSED_COUNT) : data ?? [];
  return (
    <Section title="Loved tracks" subtitle="Hearted on Last.fm">
      {loading && !data ? (
        <ListSkeleton />
      ) : (
        <>
          <div className="flex flex-col gap-1">
            {shown.map((t, i) => (
              <LovedRow key={`${t.name}-${t.artist}-${i}`} row={t} />
            ))}
          </div>
          {data && data.length > LIST_COLLAPSED_COUNT && (
            <ViewMoreButton
              expanded={expanded}
              onToggle={() => setExpanded((v) => !v)}
            />
          )}
        </>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Item cards
// ---------------------------------------------------------------------------

function ArtistCard({ rank, artist }: { rank: number; artist: LastFmTopArtist }) {
  const play = usePlayArtist();
  const navigate = useNavigate();
  const toast = useToast();
  const tidalArt = useTidalArt("artist", artist.name);
  const img = imageProxy(artist.image || tidalArt || undefined);

  const onOpen = async () => {
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
    <div
      onClick={onOpen}
      onDoubleClick={() => play(artist.name)}
      className="group relative flex cursor-pointer flex-col gap-3 rounded-lg bg-card p-4 text-left transition-colors hover:bg-accent"
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
        <button
          onClick={(e) => {
            e.stopPropagation();
            play(artist.name);
          }}
          className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100"
          title="Play artist"
          aria-label="Play artist"
        >
          <Play className="h-5 w-5 text-foreground" fill="currentColor" />
        </button>
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{artist.name}</div>
        <div className="text-xs text-muted-foreground">
          {artist.playcount.toLocaleString()} plays
        </div>
      </div>
    </div>
  );
}

function AlbumCard({ rank, album }: { rank: number; album: LastFmTopAlbum }) {
  const play = usePlayAlbum();
  const navigate = useNavigate();
  const toast = useToast();
  const tidalArt = useTidalArt("album", album.name, album.artist);
  const img = imageProxy(album.image || tidalArt || undefined);

  const onOpen = async () => {
    try {
      const res = await api.search(`${album.artist} ${album.name}`, 10);
      const exact = res.albums.find(
        (a) =>
          a.name.toLowerCase() === album.name.toLowerCase() &&
          a.artists.some((ar) => ar.name.toLowerCase() === album.artist.toLowerCase()),
      );
      const match = exact ?? res.albums[0];
      if (!match) {
        toast.show({
          kind: "info",
          title: "Not on Tidal",
          description: `Couldn't find "${album.name}" by ${album.artist}.`,
        });
        return;
      }
      navigate(`/album/${match.id}`);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't open album",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <div
      onClick={onOpen}
      onDoubleClick={() => play(album.name, album.artist)}
      className="group relative flex cursor-pointer flex-col gap-3 rounded-lg bg-card p-4 text-left transition-colors hover:bg-accent"
    >
      <span className="absolute left-5 top-5 z-10 flex h-6 min-w-6 items-center justify-center rounded-full bg-background/80 px-1.5 text-[10px] font-bold tabular-nums text-foreground shadow">
        {rank}
      </span>
      <div className="relative aspect-square overflow-hidden rounded-md bg-secondary">
        {img ? (
          <img
            src={img}
            alt=""
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-10 w-10" />
          </div>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            play(album.name, album.artist);
          }}
          className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100"
          title="Play album"
          aria-label="Play album"
        >
          <Play className="h-5 w-5 text-foreground" fill="currentColor" />
        </button>
      </div>
      <div className="min-w-0">
        <div className="truncate font-semibold">{album.name}</div>
        <div className="truncate text-xs text-muted-foreground">{album.artist}</div>
        <div className="text-[11px] text-muted-foreground/70">
          {album.playcount.toLocaleString()} plays
        </div>
      </div>
    </div>
  );
}

function TrackRow({ rank, track }: { rank: number; track: LastFmTopTrack }) {
  const play = usePlayTrack();
  const tidalArt = useTidalArt("track", track.name, track.artist);
  const img = imageProxy(track.image || tidalArt || undefined);
  const onPlay = () => play(track.name, track.artist);
  return (
    <div
      onDoubleClick={onPlay}
      className="group grid cursor-default grid-cols-[32px_48px_1fr_auto_auto] items-center gap-4 rounded-md px-3 py-2 text-left text-sm transition-colors select-none hover:bg-accent"
    >
      <span className="text-xs font-semibold tabular-nums text-muted-foreground">
        {rank}
      </span>
      <div className="relative h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary">
        {img ? (
          <img src={img} alt="" className="h-full w-full object-cover" loading="lazy" />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Music className="h-5 w-5" />
          </div>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onPlay();
          }}
          className="absolute inset-0 flex items-center justify-center bg-black/50 opacity-0 transition-opacity group-hover:opacity-100"
          title="Play"
          aria-label="Play"
        >
          <Play className="h-4 w-4 text-foreground" fill="currentColor" />
        </button>
      </div>
      <div className="min-w-0">
        <div className="truncate font-medium">{track.name}</div>
        <div className="truncate text-xs text-muted-foreground">{track.artist}</div>
      </div>
      <div className="text-xs text-muted-foreground">
        {track.playcount.toLocaleString()} plays
      </div>
      {track.url && (
        <a
          href={track.url}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => e.stopPropagation()}
          className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-accent-foreground/10 hover:text-foreground group-hover:opacity-100"
          title="Open on Last.fm"
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </a>
      )}
    </div>
  );
}

function LovedRow({ row }: { row: LastFmLovedTrack }) {
  const play = usePlayTrack();
  const tidalArt = useTidalArt("track", row.name, row.artist);
  const img = imageProxy(row.image || tidalArt || undefined);
  const onPlay = () => play(row.name, row.artist);
  return (
    <div
      onDoubleClick={onPlay}
      className="group grid cursor-default grid-cols-[48px_1fr_auto] items-center gap-4 rounded-md px-3 py-2 text-left text-sm transition-colors select-none hover:bg-accent"
    >
      <div className="relative h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary">
        {img ? (
          <img src={img} alt="" className="h-full w-full object-cover" loading="lazy" />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Heart className="h-5 w-5" />
          </div>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onPlay();
          }}
          className="absolute inset-0 flex items-center justify-center bg-black/50 opacity-0 transition-opacity group-hover:opacity-100"
          title="Play"
          aria-label="Play"
        >
          <Play className="h-4 w-4 text-foreground" fill="currentColor" />
        </button>
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-2 truncate font-medium">
          <Heart className="h-3.5 w-3.5 flex-shrink-0 text-primary" fill="currentColor" />
          <span className="truncate">{row.name}</span>
        </div>
        <div className="truncate text-xs text-muted-foreground">{row.artist}</div>
      </div>
      {row.loved_at && (
        <div className="text-[11px] text-muted-foreground">
          {new Date(row.loved_at * 1000).toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
            year: "numeric",
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Search-and-play helpers. Mirror HistoryPage's lazy resolution — we
// don't prefetch Tidal IDs for every stat row, only on click.
// ---------------------------------------------------------------------------

function usePlayTrack() {
  const toast = useToast();
  const actions = usePlayerActions();
  const inflight = useRef(new Set<string>());
  return async (title: string, artist: string) => {
    const key = `${artist}__${title}`.toLowerCase();
    if (inflight.current.has(key)) return;
    inflight.current.add(key);
    try {
      const res = await api.search(`${artist} ${title}`, 5);
      const match = findBestTrack(res.tracks, title, artist);
      if (!match) {
        toast.show({
          kind: "info",
          title: "Not on Tidal",
          description: `Couldn't find "${title}" by ${artist}.`,
        });
        return;
      }
      actions.play(match, [match]);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't play",
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      inflight.current.delete(key);
    }
  };
}

function usePlayArtist() {
  const toast = useToast();
  const actions = usePlayerActions();
  return async (name: string) => {
    try {
      const res = await api.search(name, 10);
      // Prefer playing the artist's top tracks. `search.artists` gives us
      // an artist id we could drill into — but another search for
      // "<name>" already returns their top matched tracks in most
      // cases, which is a cheaper approximation that keeps this helper
      // one round-trip.
      if (!res.tracks || res.tracks.length === 0) {
        toast.show({
          kind: "info",
          title: "Not on Tidal",
          description: `Couldn't find ${name}.`,
        });
        return;
      }
      const tracks = res.tracks
        .filter((t) => t.artists.some((a) => a.name.toLowerCase() === name.toLowerCase()))
        .slice(0, 20);
      const queue = tracks.length > 0 ? tracks : res.tracks.slice(0, 20);
      actions.play(queue[0], queue);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't play",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };
}

function usePlayAlbum() {
  const toast = useToast();
  const actions = usePlayerActions();
  return async (album: string, artist: string) => {
    try {
      const res = await api.search(`${artist} ${album}`, 10);
      const hit = res.albums?.[0];
      if (!hit) {
        toast.show({
          kind: "info",
          title: "Not on Tidal",
          description: `Couldn't find "${album}" by ${artist}.`,
        });
        return;
      }
      const detail = await api.album(hit.id);
      if (!detail.tracks || detail.tracks.length === 0) return;
      actions.play(detail.tracks[0], detail.tracks);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't play",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };
}

function findBestTrack(
  candidates: Track[] | undefined,
  title: string,
  artist: string,
): Track | null {
  if (!candidates || candidates.length === 0) return null;
  const wantTitle = title.toLowerCase();
  const wantArtist = artist.toLowerCase();
  const exact = candidates.find(
    (t) =>
      t.name.toLowerCase() === wantTitle &&
      t.artists.some((a) => a.name.toLowerCase() === wantArtist),
  );
  return exact ?? candidates[0];
}

// ---------------------------------------------------------------------------
// Section wrapper + loading skeletons
// ---------------------------------------------------------------------------

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-4 flex items-baseline gap-3">
        <h2 className="text-xl font-bold tracking-tight">{title}</h2>
        {subtitle && (
          <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {subtitle}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

function GridSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
      {Array.from({ length: 12 }).map((_, i) => (
        <Skeleton key={i} className="aspect-square w-full" />
      ))}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: 8 }).map((_, i) => (
        <Skeleton key={i} className="h-14 w-full" />
      ))}
    </div>
  );
}

