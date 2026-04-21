import { useEffect, useState } from "react";
import { Loader2, Music, RefreshCw, Trash2 } from "lucide-react";
import { api } from "@/api/client";
import type { LastFmRecentTrack } from "@/api/types";
import type { OnDownload } from "@/api/download";
import { DetailHero } from "@/components/DetailHero";
import { EmptyState } from "@/components/EmptyState";
import { PlayAllButton } from "@/components/PlayAllButton";
import { TrackList } from "@/components/TrackList";
import { Button } from "@/components/ui/button";
import { HeroSkeleton, TrackListSkeleton } from "@/components/Skeletons";
import { useToast } from "@/components/toast";
import { useRecentlyPlayed } from "@/hooks/useRecentlyPlayed";
import { useTidalTracksFor } from "@/hooks/useTidalResolve";

/**
 * Listening history styled like MixDetail — a DetailHero at the top
 * and a standard TrackList below. Two data sources:
 *
 * 1. **Last.fm (preferred)** — when the user is connected, we pull
 *    `user.getRecentTracks`. Covers every device, persists long-term.
 *    Entries are (artist, track, album) triples without Tidal IDs, so
 *    they get lazily resolved via `useTidalTracksFor` before the
 *    TrackList can render them.
 * 2. **Local (fallback)** — our `useRecentlyPlayed` cache. These
 *    already have Tidal IDs so they feed the TrackList directly.
 */

// Last.fm returns up to 50 at a time cheaply; resolving 50 via Tidal
// search is ~50 parallel requests (cached across pages), which is fine
// but more would start stressing Tidal's rate limits.
const LASTFM_LIMIT = 50;

export function HistoryPage({ onDownload }: { onDownload: OnDownload }) {
  const toast = useToast();
  const { tracks: localTracks, clear: clearLocal } = useRecentlyPlayed();
  const [lastfmConnected, setLastfmConnected] = useState<boolean | null>(null);
  const [lastfmTracks, setLastfmTracks] = useState<LastFmRecentTrack[] | null>(null);
  const [loadingLastfm, setLoadingLastfm] = useState(false);
  const [lastfmError, setLastfmError] = useState<string | null>(null);

  const reloadLastfm = async () => {
    setLoadingLastfm(true);
    setLastfmError(null);
    try {
      const rows = await api.lastfm.recentTracks(LASTFM_LIMIT);
      setLastfmTracks(rows);
    } catch (err) {
      setLastfmError(err instanceof Error ? err.message : String(err));
      setLastfmTracks([]);
    } finally {
      setLoadingLastfm(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.lastfm.status();
        if (cancelled) return;
        setLastfmConnected(s.connected);
        if (s.connected) {
          setLoadingLastfm(true);
          const rows = await api.lastfm.recentTracks(LASTFM_LIMIT);
          if (!cancelled) setLastfmTracks(rows);
        }
      } catch {
        if (!cancelled) {
          setLastfmConnected(false);
          setLastfmTracks(null);
        }
      } finally {
        if (!cancelled) setLoadingLastfm(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Last.fm path: resolve every entry to a real Tidal Track. Unresolved
  // rows are hidden (they can't be played from our queue anyway). Dedup
  // by artist+title so a repeat-listen doesn't pad the page with copies.
  const lastfmQueries =
    lastfmConnected && lastfmTracks
      ? dedupeByTitle(lastfmTracks).map((t) => ({
          title: t.track,
          artist: t.artist,
        }))
      : [];
  const resolvedLastfm = useTidalTracksFor(lastfmQueries);

  const usingLastfm = !!lastfmConnected;
  const tracks = usingLastfm ? resolvedLastfm : localTracks;
  const initialLoading = usingLastfm && lastfmTracks === null && loadingLastfm;
  const waitingOnResolution =
    usingLastfm && lastfmTracks !== null && lastfmQueries.length > 0 && resolvedLastfm.length === 0;

  if (initialLoading) {
    return (
      <div>
        <HeroSkeleton />
        <div className="mt-10">
          <TrackListSkeleton />
        </div>
      </div>
    );
  }

  const subtitle = usingLastfm
    ? "Everything you've played on Last.fm — across every device."
    : "Tracks you've played in this app, newest first. Stored locally on this device.";

  const cover = tracks[0]?.album?.cover ?? null;

  return (
    <div>
      <DetailHero
        eyebrow="History"
        title="Listening history"
        cover={cover}
        meta={
          <div className="flex flex-col gap-1">
            <p>{subtitle}</p>
            {tracks.length > 0 && <span>{tracks.length} tracks</span>}
            {lastfmError && (
              <span className="text-destructive">{lastfmError}</span>
            )}
          </div>
        }
        actions={
          <>
            {tracks.length > 0 && <PlayAllButton tracks={tracks} />}
            {usingLastfm && (
              <Button
                variant="outline"
                onClick={reloadLastfm}
                disabled={loadingLastfm}
              >
                {loadingLastfm ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4" />
                )}
                Refresh
              </Button>
            )}
            {!usingLastfm && localTracks.length > 0 && (
              <Button
                variant="outline"
                onClick={() => {
                  clearLocal();
                  toast.show({ kind: "info", title: "Listening history cleared" });
                }}
              >
                <Trash2 className="h-4 w-4" /> Clear history
              </Button>
            )}
          </>
        }
      />
      <div className="mt-8">
        {waitingOnResolution ? (
          <TrackListSkeleton />
        ) : tracks.length === 0 ? (
          <EmptyState
            icon={Music}
            title="Nothing here yet"
            description={
              usingLastfm
                ? "Play some music and it'll show up here."
                : "Play a track for at least 10 seconds and it'll show up here."
            }
          />
        ) : (
          <TrackList tracks={tracks} onDownload={onDownload} />
        )}
      </div>
    </div>
  );
}

function dedupeByTitle(items: LastFmRecentTrack[]): LastFmRecentTrack[] {
  const seen = new Set<string>();
  const out: LastFmRecentTrack[] = [];
  for (const t of items) {
    const key = `${t.artist.toLowerCase()}::${t.track.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(t);
  }
  return out;
}
