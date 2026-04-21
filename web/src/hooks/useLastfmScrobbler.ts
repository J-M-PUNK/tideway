import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import { usePlayerMeta, usePlayerTime } from "./PlayerContext";

/**
 * Wire the player into Last.fm scrobbling.
 *
 * Last.fm's scrobble spec (https://www.last.fm/api/scrobbling):
 * 1. The track must be longer than 30 seconds.
 * 2. The track has been played for at least half its duration, OR for
 *    4 minutes (whichever occurs earlier). Seeks don't count — only
 *    actual listening time.
 * 3. `now_playing` is fired as soon as the user starts listening.
 *
 * We approximate rule 2 by watching `currentTime` instead of summing
 * play seconds across pauses. For the common case (play a track, maybe
 * pause, resume, finish) the result is the same — `currentTime`
 * monotonically increases while playing and Last.fm accepts it.
 * Pathological seek-around behavior may cause under-scrobbling, which
 * is fine (better than over-scrobbling tracks the user didn't really
 * listen to).
 */
export function useLastfmScrobbler(): void {
  const { track, playing } = usePlayerMeta();
  const { currentTime, duration } = usePlayerTime();

  // Per-track bookkeeping. Keyed by track.id so quickly skipping between
  // the same track twice still works (rare but possible).
  const nowPlayingFiredRef = useRef<string | null>(null);
  const scrobbledRef = useRef<Set<string>>(new Set());
  const startTimestampRef = useRef<number>(0);

  // Track change: remember when this listen started so we can stamp the
  // scrobble with the accurate begin-of-listen UNIX timestamp Last.fm
  // wants. Also clear nowPlayingFired so the new track fires its own.
  useEffect(() => {
    if (!track) return;
    nowPlayingFiredRef.current = null;
    startTimestampRef.current = Math.floor(Date.now() / 1000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [track?.id]);

  // Fire updateNowPlaying the first time we observe the track actually
  // playing. This is the "is now listening" status on their profile —
  // not persisted, safe to be a little lazy about it.
  useEffect(() => {
    if (!track || !playing) return;
    if (nowPlayingFiredRef.current === track.id) return;
    nowPlayingFiredRef.current = track.id;
    api.lastfm
      .nowPlaying({
        artist: primaryArtist(track.artists),
        title: track.name,
        album: track.album?.name,
        duration: track.duration,
      })
      .catch(() => {
        /* Not connected / creds missing. Silence is the correct UX
           here — we don't want to spam the user with toasts about an
           optional feature they haven't set up. */
      });
  }, [track, playing]);

  // Scrobble when the track crosses the Last.fm threshold. We never
  // scrobble the same track twice in one listen — scrobbledRef is the
  // set of track.ids that already fired for this listen. It gets
  // cleared on track change so replaying the same track later works.
  useEffect(() => {
    if (!track || !playing) return;
    if (duration < 30) return; // Under the spec's floor.
    if (scrobbledRef.current.has(track.id)) return;
    const threshold = Math.min(duration / 2, 240);
    if (currentTime < threshold) return;
    scrobbledRef.current.add(track.id);
    api.lastfm
      .scrobble({
        artist: primaryArtist(track.artists),
        title: track.name,
        album: track.album?.name,
        duration: track.duration,
        timestamp: startTimestampRef.current,
      })
      .catch(() => {
        /* Best-effort. */
      });
  }, [track, playing, currentTime, duration]);

  // Re-arm the scrobble flag on track change so a replay of the same
  // track (after switching away and back) will scrobble again.
  useEffect(() => {
    if (!track) return;
    // Keep the set bounded — in practice this runs once per track so
    // unbounded growth isn't a real concern, but a defensive cap keeps
    // the hook's memory footprint flat.
    if (scrobbledRef.current.size > 500) {
      scrobbledRef.current = new Set([track.id]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [track?.id]);
}

function primaryArtist(artists: { name: string }[] | undefined): string {
  if (!artists || artists.length === 0) return "";
  // Last.fm's canonical scrobble convention: the primary artist goes
  // in `artist`, secondary artists aren't duplicated — last.fm
  // resolves featuring artists automatically by looking them up in
  // track metadata on their end.
  return artists[0].name;
}
