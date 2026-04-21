import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ChevronRight, Loader2, Music, Play, Sparkles } from "lucide-react";
import { api } from "@/api/client";
import type { OnDownload } from "@/api/download";
import { useApi } from "@/hooks/useApi";
import { useColumnCount } from "@/hooks/useColumnCount";
import { useListeningHistory, type HistoryItem } from "@/hooks/useListeningHistory";
import { usePlayerActions, usePlayerMeta } from "@/hooks/PlayerContext";
import { useToast } from "@/components/toast";
import { PageView } from "@/components/PageView";
import { ErrorView } from "@/components/ErrorView";
import { GridSkeleton } from "@/components/Skeletons";
import { imageProxy } from "@/lib/utils";
import { findBestMatch } from "@/lib/match";
import { useTidalArtistId } from "@/hooks/useTidalResolve";

type MixSummary = {
  kind: "mix";
  id: string;
  name: string;
  subtitle: string;
  cover: string | null;
};

export function Home({ onDownload }: { onDownload: OnDownload }) {
  const { data, loading, error } = useApi(() => api.page("home"), []);
  const { data: mixes } = useApi(() => api.mixes(), []);
  // Fetch a padded buffer (not just the widest row count) — dedup in
  // JumpBackIn filters out repeat plays of the same track, so on a
  // heavy-repeat session we'd otherwise leave the row half-empty.
  // 30 gives plenty of headroom for both local and Last.fm sources.
  const history = useListeningHistory(30);

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  if (loading) {
    return (
      <div>
        <h1 className="mb-6 text-4xl font-bold tracking-tight">{greeting}</h1>
        <GridSkeleton count={12} />
      </div>
    );
  }
  if (error || !data) return <ErrorView error={error ?? "Couldn't load home"} />;

  return (
    <div>
      <h1 className="mb-8 text-4xl font-bold tracking-tight">{greeting}</h1>
      {history.items.length > 0 && <JumpBackIn items={history.items} />}
      {mixes && mixes.length > 0 && <MadeForYou mixes={mixes} />}
      <PageView page={data} onDownload={onDownload} forceSingleRow />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row header + "View more" link — matches the treatment PageView uses so
// our bespoke rows visually blend with the Tidal-editorial rows below.
// ---------------------------------------------------------------------------
function RowHeader({ title, viewAllPath }: { title: string; viewAllPath: string }) {
  return (
    <div className="mb-4 flex items-baseline justify-between gap-4">
      <h2 className="text-xl font-bold tracking-tight">{title}</h2>
      <Link
        to={viewAllPath}
        className="flex flex-shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
      >
        View more <ChevronRight className="h-3.5 w-3.5" />
      </Link>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Jump back in — single row, capped. History source (Last.fm vs local)
// is decided by useListeningHistory; this component doesn't care.
// ---------------------------------------------------------------------------
function JumpBackIn({ items }: { items: HistoryItem[] }) {
  // Dedupe by `artist + name` so replaying the same track back-to-back
  // doesn't fill the row with copies of it. We keep the first seen
  // occurrence (newest, since the source list is sorted newest-first).
  // The full per-play history is still available on the History page
  // via "View more".
  const unique = dedupeByTitle(items);
  // Compact horizontal "pill" layout — small thumbnail + text, arranged
  // in a responsive 2–3 column grid. Trades the big-cover eye-candy
  // for density: six items fit in the space one row of square cards
  // used to occupy.
  const visible = unique.slice(0, 6);
  return (
    <div className="mb-10">
      <RowHeader title="Jump back in" viewAllPath="/history" />
      <div className="grid gap-2 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3">
        {visible.map((item) => (
          <HistoryCard key={item.key} item={item} />
        ))}
      </div>
    </div>
  );
}

function dedupeByTitle(items: HistoryItem[]): HistoryItem[] {
  const seen = new Set<string>();
  const out: HistoryItem[] = [];
  for (const item of items) {
    const key = `${item.artist.toLowerCase()}::${item.name.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

function HistoryCard({ item }: { item: HistoryItem }) {
  const toast = useToast();
  const actions = usePlayerActions();
  const meta = usePlayerMeta();
  const navigate = useNavigate();
  const [resolving, setResolving] = useState(false);
  // Pre-warm the Tidal artist id for Last.fm items so the artist-name
  // link feels instant on click. No-ops for local items which already
  // have the id on their tidalTrack.
  const preWarmedArtistId = useTidalArtistId(
    item.tidalTrack ? "" : item.artist,
  );

  const isCurrent =
    meta.track &&
    meta.track.name.toLowerCase() === item.name.toLowerCase() &&
    meta.track.artists.some((a) => a.name.toLowerCase() === item.artist.toLowerCase());

  // Resolve the item to a Tidal Track — returns the cached local
  // Track, or kicks off a search for Last.fm entries and returns the
  // best match. Used by Play, Go-to-album, and Go-to-artist actions.
  const resolveTrack = async () => {
    if (item.tidalTrack) return item.tidalTrack;
    if (!item.lookup) return null;
    const res = await api.search(`${item.lookup.artist} ${item.lookup.title}`, 5);
    return findBestMatch(res.tracks, {
      track: item.lookup.title,
      artist: item.lookup.artist,
    });
  };

  const onPlay = async () => {
    if (resolving) return;
    if (item.tidalTrack) {
      actions.play(item.tidalTrack, [item.tidalTrack]);
      return;
    }
    setResolving(true);
    try {
      const match = await resolveTrack();
      if (!match) {
        toast.show({
          kind: "info",
          title: "Not on Tidal",
          description: `Couldn't find "${item.name}" by ${item.artist}.`,
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
      setResolving(false);
    }
  };

  const onOpenAlbum = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (item.tidalTrack?.album) {
      navigate(`/album/${item.tidalTrack.album.id}`);
      return;
    }
    try {
      const match = await resolveTrack();
      if (!match?.album) {
        toast.show({
          kind: "info",
          title: "Not on Tidal",
          description: "Couldn't find an album for that track.",
        });
        return;
      }
      navigate(`/album/${match.album.id}`);
    } catch (err) {
      toast.show({
        kind: "error",
        title: "Couldn't open album",
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const onOpenArtist = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (item.tidalTrack?.artists[0]) {
      navigate(`/artist/${item.tidalTrack.artists[0].id}`);
      return;
    }
    if (preWarmedArtistId) {
      navigate(`/artist/${preWarmedArtistId}`);
      return;
    }
    try {
      const res = await api.search(item.artist, 10);
      const exact = res.artists.find(
        (a) => a.name.toLowerCase() === item.artist.toLowerCase(),
      );
      const match = exact ?? res.artists[0];
      if (!match) {
        toast.show({
          kind: "info",
          title: "Not on Tidal",
          description: `Couldn't find ${item.artist}.`,
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
      onDoubleClick={onPlay}
      className="group flex cursor-default items-center gap-3 rounded-md bg-card/60 p-2 pr-3 transition-colors select-none hover:bg-accent"
    >
      <div className="relative h-12 w-12 flex-shrink-0 overflow-hidden rounded bg-secondary">
        {item.cover ? (
          <img
            src={imageProxy(item.cover)}
            alt=""
            className="h-full w-full object-cover"
            loading="lazy"
          />
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
          {resolving ? (
            <Loader2 className="h-4 w-4 animate-spin text-foreground" />
          ) : (
            <Play className="h-4 w-4 text-foreground" fill="currentColor" />
          )}
        </button>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 truncate text-sm font-semibold">
          {item.nowPlaying && (
            <span className="flex-shrink-0 rounded-sm bg-primary/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-primary">
              Now
            </span>
          )}
          <button
            onClick={onOpenAlbum}
            className={`truncate text-left hover:underline ${isCurrent ? "text-primary" : ""}`}
            title="Go to album"
          >
            {item.name}
          </button>
        </div>
        <button
          onClick={onOpenArtist}
          className="block truncate text-left text-xs text-muted-foreground hover:text-foreground hover:underline"
          title="Go to artist"
        >
          {item.artist}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Made for you — single row, capped; view more → /mixes.
// ---------------------------------------------------------------------------
function MadeForYou({ mixes }: { mixes: MixSummary[] }) {
  const cols = useColumnCount();
  return (
    <div className="mb-10">
      <RowHeader title="Made for you" viewAllPath="/mixes" />
      <div className="grid gap-4 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 2xl:grid-cols-6">
        {mixes.slice(0, cols).map((m) => (
          <MixCard key={m.id} mix={m} />
        ))}
      </div>
    </div>
  );
}

function MixCard({ mix }: { mix: MixSummary }) {
  return (
    <Link
      to={`/mix/${mix.id}`}
      className="group flex flex-col gap-2 rounded-lg p-2 transition-colors hover:bg-accent"
    >
      <div className="aspect-square overflow-hidden rounded-md bg-secondary shadow">
        {mix.cover ? (
          <img
            src={imageProxy(mix.cover)}
            alt=""
            className="h-full w-full object-cover transition-transform group-hover:scale-105"
            loading="lazy"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-muted-foreground">
            <Sparkles className="h-10 w-10" />
          </div>
        )}
      </div>
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold">{mix.name}</div>
        {mix.subtitle && (
          <div className="truncate text-xs text-muted-foreground">{mix.subtitle}</div>
        )}
      </div>
    </Link>
  );
}

