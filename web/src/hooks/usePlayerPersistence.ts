import { useEffect, type MutableRefObject } from "react";
import { api } from "@/api/client";
import type { Track } from "@/api/types";
import type { PlayerState, RepeatMode } from "./usePlayer";

const PERSIST_KEY = "tideway:player";
// Tightened from 5 s to 2 s so the worst-case "you quit between
// two ticks" loss window is smaller. Persist work is a single
// JSON.stringify + setItem on a state we already have in memory;
// extra cost is negligible compared to the UX cost of forgetting
// what the user was just listening to.
const PERSIST_INTERVAL_MS = 2000;

export interface PersistedState {
  queue: Track[];
  queueIndex: number;
  currentTime: number;
  volume: number;
  shuffle: boolean;
  repeat: RepeatMode;
  /** Id of the track that was loaded when the user closed the app.
   *  Used as a sanity-check against the persisted queue so that on
   *  boot we only restore "now playing" when queue[queueIndex] still
   *  matches what was loaded (queue shape may have drifted between
   *  versions). */
  trackId: string | null;
  /** The full Track object the user was listening to. Persisted
   *  alongside `trackId` so single-track plays (where the queue
   *  is empty and `queueIndex` is -1) can still restore on relaunch.
   *  Without this field, tapping a search result and quitting would
   *  lose the now-playing on reopen, since the queue-based restore
   *  path requires queueIndex >= 0. */
  track: Track | null;
}

/** Read the localStorage snapshot. Returns an empty object on miss
 *  / parse error — the caller treats missing fields as "no
 *  persisted data" and falls through to defaults. */
export function loadPersisted(): Partial<PersistedState> {
  try {
    const raw = localStorage.getItem(PERSIST_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed;
  } catch {
    return {};
  }
}

/** Decide what to seed `state.track`, `state.queueIndex`, and
 *  `state.currentTime` with based on a persisted snapshot.
 *  Extracted so both the localStorage path (synchronous initializer)
 *  and the backend-backstop path (`api.nowPlayingState.get()`) apply
 *  the same shape-validation. */
export function pickRestoreFromPersisted(
  persisted: Partial<PersistedState>,
  queue: Track[],
): {
  restoreIndex: number;
  restoreTrack: Track | null;
  restoreCurrentTime: number;
} {
  let restoreIndex = -1;
  let restoreTrack: Track | null = null;
  let restoreCurrentTime = 0;
  if (
    typeof persisted.queueIndex === "number" &&
    persisted.queueIndex >= 0 &&
    persisted.queueIndex < queue.length &&
    typeof persisted.trackId === "string" &&
    queue[persisted.queueIndex] &&
    queue[persisted.queueIndex].id === persisted.trackId
  ) {
    restoreIndex = persisted.queueIndex;
    restoreTrack = queue[persisted.queueIndex];
  } else if (
    persisted.track &&
    typeof persisted.track === "object" &&
    typeof persisted.trackId === "string" &&
    persisted.track.id === persisted.trackId
  ) {
    restoreTrack = persisted.track;
  }
  if (
    restoreTrack &&
    typeof persisted.currentTime === "number" &&
    persisted.currentTime > 0
  ) {
    restoreCurrentTime = persisted.currentTime;
  }
  return { restoreIndex, restoreTrack, restoreCurrentTime };
}

/**
 * Persist queue + prefs so a relaunch picks up where the user left
 * off. Skip writes when nothing's loaded so we don't clobber a real
 * prior session.
 *
 * Three lifecycle hooks listen for "the user is leaving":
 *
 *   - `beforeunload`: classic page-close. Fires reliably on most
 *     browser exits and on pywebview window-close.
 *   - `pagehide`: fires in cases where `beforeunload` doesn't —
 *     Safari mobile, BFCache navigations, and some embedded-
 *     webview paths. Most reliable last-chance hook in the spec.
 *   - `visibilitychange` to "hidden": fires when the app loses
 *     focus or is sent to background. macOS Cmd-Q sometimes hides
 *     the window before tearing it down without firing
 *     beforeunload, so a flush here catches it.
 *
 * All three call the same `flush()` so a triple-hit on quit just
 * writes the same JSON three times — cheap and idempotent.
 *
 * Two storage targets:
 *
 *   - localStorage for the fast read path on next launch (the
 *     synchronous initializer in `usePlayer` reads it).
 *   - The backend's `now_playing.json` as a backstop. Pywebview's
 *     WKWebView on macOS doesn't always preserve localStorage
 *     between launches — depending on the data-store mode the OS
 *     may discard the database on app quit — so the backend keeps
 *     a parallel copy. `usePlayer`'s `backendHydratedRef` effect
 *     reads it when localStorage came up empty.
 */
export function usePlayerPersistence(
  stateRef: MutableRefObject<PlayerState>,
): void {
  useEffect(() => {
    const flush = () => {
      const s = stateRef.current;
      // Skip writes when nothing's loaded — don't clobber a real
      // prior session. Allow currentTime === 0 because a paused-
      // at-start track is still worth persisting (user explicitly
      // queued it).
      if (s.queue.length === 0 && s.queueIndex === -1 && !s.track) {
        return;
      }
      const snapshot: PersistedState = {
        queue: s.queue,
        queueIndex: s.queueIndex,
        currentTime: s.currentTime,
        volume: s.volume,
        shuffle: s.shuffle,
        repeat: s.repeat,
        trackId: s.track?.id ?? null,
        track: s.track,
      };
      try {
        localStorage.setItem(PERSIST_KEY, JSON.stringify(snapshot));
      } catch {
        /* storage full or disabled */
      }
      // Backend backstop. Fire-and-forget; the api method's own
      // .catch swallows the failure.
      api.nowPlayingState.put(
        snapshot as unknown as Record<string, unknown>,
      );
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") flush();
    };
    const id = window.setInterval(flush, PERSIST_INTERVAL_MS);
    window.addEventListener("beforeunload", flush);
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("beforeunload", flush);
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [stateRef]);
}
