import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { LastFmRecentTrack, Track } from "@/api/types";
import { useRecentlyPlayed } from "./useRecentlyPlayed";

/**
 * Unified history item — a row consumers can render uniformly whether
 * the source is Last.fm (cross-device, no Tidal IDs) or our local
 * recently-played cache (Tidal IDs, this device only).
 *
 * - `tidalTrack` is set for items that have a real Tidal `Track` and
 *   can play directly by ID (local history).
 * - `lookup` is set for items without a Tidal ID; the consumer does a
 *   search-by-name before playback (Last.fm entries). Same shape as
 *   what `LastFmRow` in HistoryPage already does.
 */
export type HistoryItem = {
  key: string;
  name: string;
  artist: string;
  cover: string | null;
  nowPlaying?: boolean;
  playedAt?: number | null;
  tidalTrack?: Track;
  lookup?: { artist: string; title: string };
};

export type HistorySource = "lastfm" | "local";

/**
 * Hybrid listening history. Prefers Last.fm when connected so history
 * reflects plays from every device; otherwise falls back to the local
 * cache `useRecentlyPlayed` maintains.
 *
 * `limit` caps the returned list — the same data is used both for the
 * short row on Home (limit 6) and the full History page (limit 100).
 */
export function useListeningHistory(limit = 100): {
  items: HistoryItem[];
  source: HistorySource;
  loading: boolean;
  connected: boolean | null;
  reload: () => Promise<void>;
} {
  const { tracks: localTracks } = useRecentlyPlayed();
  const [connected, setConnected] = useState<boolean | null>(null);
  const [lastfm, setLastfm] = useState<LastFmRecentTrack[] | null>(null);
  const [loading, setLoading] = useState(false);

  const reload = async () => {
    if (!connected) return;
    setLoading(true);
    try {
      const rows = await api.lastfm.recentTracks(Math.min(limit, 100));
      setLastfm(rows);
    } catch {
      // Silent — hybrid hook falls through to local history on failure.
      setLastfm([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.lastfm.status();
        if (cancelled) return;
        setConnected(s.connected);
        if (s.connected) {
          setLoading(true);
          const rows = await api.lastfm.recentTracks(Math.min(limit, 100));
          if (!cancelled) setLastfm(rows);
        }
      } catch {
        if (!cancelled) {
          setConnected(false);
          setLastfm(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [limit]);

  if (connected && lastfm) {
    return {
      items: lastfm.slice(0, limit).map((t, i) => ({
        key: `lastfm:${t.artist}:${t.track}:${t.played_at ?? i}`,
        name: t.track,
        artist: t.artist,
        cover: t.cover || null,
        nowPlaying: t.now_playing,
        playedAt: t.played_at,
        lookup: { artist: t.artist, title: t.track },
      })),
      source: "lastfm",
      loading,
      connected,
      reload,
    };
  }

  return {
    items: localTracks.slice(0, limit).map((t) => ({
      key: `local:${t.id}`,
      name: t.name,
      artist: t.artists[0]?.name ?? "",
      cover: t.album?.cover ?? null,
      tidalTrack: t,
    })),
    source: "local",
    loading: false,
    connected,
    reload,
  };
}
