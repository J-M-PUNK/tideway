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
  const { track } = usePlayerMeta();
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
  const sessionRef = useRef<{
    sessionId: string;
    trackId: string;
    startTsMs: number;
    startPositionS: number;
    duration: number;
  } | null>(null);
  const lastKnownPositionRef = useRef(0);

  // Track currentTime without re-triggering the session-lifecycle
  // effect — that effect only cares about track identity, not the
  // current time cursor. Keeping them separate avoids spurious
  // session rotations every position tick.
  useEffect(() => {
    lastKnownPositionRef.current = currentTime;
  }, [currentTime]);

  useEffect(() => {
    // Finalize the previous session (if any) before opening a new one.
    const prev = sessionRef.current;
    if (prev) {
      sessionRef.current = null;
      const listened = Math.max(0, lastKnownPositionRef.current - prev.startPositionS);
      const meetsThreshold =
        listened >= 30 || (prev.duration > 0 && listened >= prev.duration / 2);
      if (meetsThreshold) {
        api.playReport
          .stop({
            session_id: prev.sessionId,
            track_id: prev.trackId,
            quality: toTidalQuality(streamingQuality),
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
      const listened = Math.max(0, lastKnownPositionRef.current - prev.startPositionS);
      const meetsThreshold =
        listened >= 30 || (prev.duration > 0 && listened >= prev.duration / 2);
      if (!meetsThreshold) return;
      api.playReport
        .stop({
          session_id: prev.sessionId,
          track_id: prev.trackId,
          quality: toTidalQuality(streamingQuality),
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
