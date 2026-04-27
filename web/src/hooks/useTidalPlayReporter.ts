import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import { usePlayerMeta, usePlayerTime } from "./PlayerContext";
import { useUiPreferences, type StreamingQuality } from "./useUiPreferences";

/**
 * Report playback to Tidal's Event Producer so every track we play
 * counts for the user's Tidal Recently Played, feeds recommendations,
 * and — critically — gets the artist their royalty credit. Without this
 * hook, playing here leaves no trace on Tidal's backend.
 *
 * Model: one `playback_session` event per track listen, fired at the
 * *end* of the listen (Tidal's SDKs do the same). Start time and end
 * time are both included in the single event so Tidal reconstructs the
 * duration listened accurately.
 *
 * We mark a listen as "complete enough to report" when it hit
 * 50% of duration or 30 seconds, whichever comes first. Shorter
 * skips aren't reported — matches Spotify/Apple Music norms and
 * avoids skewing royalties from accidental starts.
 */
export function useTidalPlayReporter(): void {
  const { track, source } = usePlayerMeta();
  const { currentTime } = usePlayerTime();
  const { streamingQuality } = useUiPreferences();

  // Session bookkeeping for the currently-playing track. We're careful
  // about the lifecycle:
  //   - On track change, finalize the *previous* track's session if it
  //     met the listen threshold, then open a new session for the new
  //     track.
  //   - `lastKnownPositionRef` is the highest `currentTime` we saw —
  //     we use that as the end position since a fresh useEffect tick
  //     after track-change already has currentTime=0.
  //   - `source` is snapshotted at play-start so if the user navigates
  //     away from the album page mid-track, the stop event still
  //     attributes the play to the album that started it.
  const sessionRef = useRef<{
    sessionId: string;
    trackId: string;
    startTsMs: number;
    startPositionS: number;
    duration: number;
    sourceType: string;
    sourceId: string;
  } | null>(null);
  const lastKnownPositionRef = useRef(0);

  // Track currentTime in a ref so we don't retrigger the
  // session lifecycle effect on every position tick. That effect
  // only cares about track identity.
  //
  // The ref is a high water mark within a session, not a mirror
  // of currentTime. When the backend sends its "ended" snapshot
  // it resets position to 0 right before the track change fires.
  // If we mirrored that naively, the ref would drop to 0 just in
  // time for the `[track?.id]` effect to read it. The listen
  // duration would compute as 0 and the stop event would never
  // meet threshold on a natural track end. Keeping this as a
  // monotonic max preserves the real last position. The session
  // start handler below resets the ref to 0 for the next track.
  useEffect(() => {
    if (currentTime > lastKnownPositionRef.current) {
      lastKnownPositionRef.current = currentTime;
    }
  }, [currentTime]);

  useEffect(() => {
    // Finalize the previous session (if any) before opening a new one.
    const prev = sessionRef.current;
    if (prev) {
      sessionRef.current = null;
      const listened = Math.max(
        0,
        lastKnownPositionRef.current - prev.startPositionS,
      );
      const meetsThreshold =
        listened >= 30 || (prev.duration > 0 && listened >= prev.duration / 2);
      if (meetsThreshold) {
        api.playReport
          .stop({
            session_id: prev.sessionId,
            track_id: prev.trackId,
            quality: toTidalQuality(streamingQuality),
            // Tidal's Recently Played aggregator surfaces
            // container plays (ALBUM / PLAYLIST / MIX), not track
            // events. When the queue was started with container
            // context we report that source so the play attributes
            // to the album/playlist; otherwise we fall back to
            // "TRACK" + track_id which still counts toward
            // aggregates like "My Most Listened."
            source_type: prev.sourceType,
            source_id: prev.sourceId,
            start_ts_ms: prev.startTsMs,
            end_ts_ms: Date.now(),
            start_position_s: prev.startPositionS,
            end_position_s: lastKnownPositionRef.current,
          })
          .catch(() => {
            /* Best-effort. The backend logs the failure. */
          });
      }
    }

    if (!track) return;

    // Open a fresh session for the new track. The server hands us a
    // UUID and start timestamp — using server time keeps events
    // consistent even if the client's clock is skewed.
    //
    // Source is captured from the player state at play-start. A later
    // navigation to a different page doesn't retroactively change
    // what originated this play — the album you clicked Play on is
    // the one this listen should count toward on Recently Played.
    const resolvedSourceType = source?.type ?? "TRACK";
    const resolvedSourceId = source?.id ?? track.id;
    let cancelled = false;
    api.playReport
      .start()
      .then((resp) => {
        if (cancelled) return;
        sessionRef.current = {
          sessionId: resp.session_id,
          trackId: track.id,
          startTsMs: resp.ts_ms,
          startPositionS: 0,
          duration: track.duration,
          sourceType: resolvedSourceType,
          sourceId: resolvedSourceId,
        };
        lastKnownPositionRef.current = 0;
      })
      .catch(() => {
        /* Couldn't allocate a session — we'll skip reporting this
           listen rather than fabricate one client-side. */
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [track?.id]);

  // Tab close / app quit — flush the in-flight session so the user's
  // last play before shutting down isn't lost. `navigator.sendBeacon`
  // would be the idiomatic thing here, but the backend's /stop
  // endpoint expects JSON auth and pydantic validation; using
  // `fetch(..., keepalive: true)` via our existing api wrapper is
  // simpler and good enough for the desktop shell.
  useEffect(() => {
    const flush = () => {
      const prev = sessionRef.current;
      if (!prev) return;
      const listened = Math.max(
        0,
        lastKnownPositionRef.current - prev.startPositionS,
      );
      const meetsThreshold =
        listened >= 30 || (prev.duration > 0 && listened >= prev.duration / 2);
      if (!meetsThreshold) return;
      api.playReport
        .stop({
          session_id: prev.sessionId,
          track_id: prev.trackId,
          quality: toTidalQuality(streamingQuality),
          source_type: prev.sourceType,
          source_id: prev.sourceId,
          start_ts_ms: prev.startTsMs,
          end_ts_ms: Date.now(),
          start_position_s: prev.startPositionS,
          end_position_s: lastKnownPositionRef.current,
        })
        .catch(() => {});
    };
    window.addEventListener("beforeunload", flush);
    return () => window.removeEventListener("beforeunload", flush);
  }, [streamingQuality]);
}

/** Map our internal quality codes to Tidal's event-producer enum. */
function toTidalQuality(q: StreamingQuality): string {
  switch (q) {
    case "low_96k":
      return "LOW";
    case "low_320k":
      return "HIGH";
    case "high_lossless":
      return "LOSSLESS";
    case "hi_res_lossless":
      return "HI_RES_LOSSLESS";
  }
}
