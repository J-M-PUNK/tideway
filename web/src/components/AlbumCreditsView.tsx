import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Info, Loader2 } from "lucide-react";
import { api } from "@/api/client";
import type { CreditEntry } from "@/api/types";

type TrackCredits = {
  track_id: string;
  track_num: number;
  title: string;
  artists: { id: string | null; name: string }[];
  credits: CreditEntry[];
};

/**
 * Album-level credits, per-track — matches Tidal's "Credits" tab: a
 * 2-column grid of cards, each card showing one track's role →
 * contributor breakdown. Loads on mount, caches nothing because the
 * parent only renders this when the user has explicitly toggled
 * Credits on.
 */
export function AlbumCreditsView({ albumId }: { albumId: string }) {
  const [tracks, setTracks] = useState<TrackCredits[] | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setTracks(null);
    setLoading(true);
    api
      .albumCredits(albumId)
      .then((rows) => {
        if (!cancelled) setTracks(rows);
      })
      .catch(() => {
        if (!cancelled) setTracks([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [albumId]);

  if (loading && !tracks) {
    return (
      <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading credits…
      </div>
    );
  }
  if (!tracks || tracks.length === 0) {
    return (
      <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
        <Info className="h-4 w-4" /> No credits listed for this album.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      {tracks.map((t) => (
        <TrackCreditsCard key={t.track_id} track={t} />
      ))}
    </div>
  );
}

function TrackCreditsCard({ track }: { track: TrackCredits }) {
  const primaryArtist = track.artists[0];
  return (
    <div className="overflow-hidden rounded-lg border border-border/50 bg-card/40">
      {/* Header bar — track number + title + primary artist */}
      <div className="flex items-center gap-4 border-b border-border/50 bg-card/60 px-5 py-3">
        <span className="w-6 flex-shrink-0 text-sm tabular-nums text-muted-foreground">
          {track.track_num}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">{track.title}</div>
          {primaryArtist && (
            <div className="truncate text-xs text-muted-foreground">
              {primaryArtist.id ? (
                <Link to={`/artist/${primaryArtist.id}`} className="hover:underline">
                  {primaryArtist.name}
                </Link>
              ) : (
                primaryArtist.name
              )}
            </div>
          )}
        </div>
      </div>
      {/* Credits body — one role block per entry */}
      <div className="flex flex-col gap-4 px-5 py-4">
        {track.credits.length === 0 ? (
          <div className="text-xs text-muted-foreground">No credits listed.</div>
        ) : (
          track.credits.map((entry) => (
            <div key={entry.role} className="flex flex-col gap-1">
              <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                {entry.role}
              </div>
              <div className="text-sm">
                {entry.contributors.map((c, i) => (
                  <span key={`${c.name}-${i}`}>
                    {i > 0 && <span className="text-muted-foreground">, </span>}
                    {c.id ? (
                      <Link to={`/artist/${c.id}`} className="hover:underline">
                        {c.name}
                      </Link>
                    ) : (
                      c.name
                    )}
                  </span>
                ))}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
