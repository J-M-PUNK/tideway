import { useCallback, useEffect, useRef, useState } from "react";
import type { PlayerSnapshot, StreamInfo, Track } from "@/api/types";
import { api } from "@/api/client";
import { useUiPreferences, type StreamingQuality } from "./useUiPreferences";

/**
 * Player hook. Remote-controls the backend PyAV + sounddevice
 * engine: every play/pause/seek/volume call fires an HTTP request
 * at /api/player/*, and playback state flows back through an SSE
 * stream.
 *
 * What this hook owns:
 *   - Queue / shuffle / repeat state
 *   - Sleep timer
 *   - Volume (in the 0..1 linear space consumers expect)
 *   - localStorage persistence
 *
 * What the backend owns:
 *   - Stream resolution (local file lookup or Tidal DASH manifest)
 *   - Decoder / audio output (PyAV → sounddevice)
 *   - Reported position / duration while playing
 *
 * Gapless: when the current track crosses the 15s-remaining mark
 * (see the preload trigger in `applySnapshot`), we fire
 * /api/player/preload for the next track in the queue. The backend
 * pre-decodes it into a PCM buffer. On natural track-end the audio
 * callback splices the preloaded buffer into the live OutputStream
 * at the sample boundary — true gapless for same-rate transitions,
 * ~50ms reopen blip for cross-rate.
 */

const PERSIST_KEY = "tideway:player";
// Tightened from 5s to 2s so the worst-case "you quit between two
// ticks" loss window is smaller. Persist work is a single
// JSON.stringify + setItem on a state we already have in memory; the
// extra cost is negligible compared to the UX cost of forgetting what
// the user was just listening to.
const PERSIST_INTERVAL_MS = 2000;

export type RepeatMode = "off" | "all" | "one";

/** What the user clicked to start this queue. Drives Tidal's play-log
 *  sourceType/sourceId so Recently Played shows the container (album /
 *  playlist / mix) rather than a sourceless track event that gets
 *  filtered from Recently Played aggregation.
 *
 *  - ALBUM / PLAYLIST / MIX / ARTIST surface in Tidal's Recently
 *    Played.
 *  - TRACK is the fallback for queues started from a single-track
 *    tap (search result, radio seed, etc.). Track plays count for
 *    aggregates like "My Most Listened" but do not appear in
 *    Recently Played.
 */
export type PlaySourceType = "ALBUM" | "PLAYLIST" | "MIX" | "ARTIST" | "TRACK";

export interface PlaySource {
  type: PlaySourceType;
  id: string;
}

export interface PlayerState {
  track: Track | null;
  playing: boolean;
  currentTime: number;
  duration: number;
  loading: boolean;
  error: string | null;
  volume: number;
  queue: Track[];
  queueIndex: number;
  shuffle: boolean;
  repeat: RepeatMode;
  /** What's actually audible — codec + sample rate. Null while
   *  loading, idle, or when the backend couldn't determine it. */
  streamInfo: StreamInfo | null;
  /** Source container for the active queue. Used by the Tidal
   *  play-log reporter so plays are attributed to the album /
   *  playlist / mix that started them. Null when the queue was
   *  started without container context. */
  source: PlaySource | null;
  /** Mirror of the backend's Force Volume setting. When true the
   *  volume slider should render disabled and the backend rejects
   *  set_volume calls; the user attenuates via their DAC / OS. */
  forceVolume: boolean;
}

interface PersistedState {
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

function loadPersisted(): Partial<PersistedState> {
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
 *  `state.currentTime` with based on a persisted snapshot — extracted
 *  so the backend backstop hydrate path (`api.nowPlayingState.get()`)
 *  can apply the same shape-validation as the localStorage path. */
function pickRestoreFromPersisted(
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

const INITIAL: PlayerState = {
  track: null,
  playing: false,
  currentTime: 0,
  duration: 0,
  loading: false,
  error: null,
  volume: 1,
  queue: [],
  queueIndex: -1,
  shuffle: false,
  repeat: "off",
  streamInfo: null,
  source: null,
  forceVolume: false,
};

/**
 * Pick the next queue index given the current state.
 *
 * `onEnded` is true when the track just finished naturally — in that
 * case `repeat: "one"` loops the same index. When the user hits Next
 * manually we always advance regardless of repeat mode.
 */
export function pickNextIndex(
  state: PlayerState,
  onEnded = false,
): number | null {
  if (state.queue.length === 0) return null;
  if (onEnded && state.repeat === "one") return state.queueIndex;
  // Single-track queue: a natural end shouldn't loop unless the user
  // asked for repeat. Manual Next still replays (matches Spotify).
  if (state.queue.length === 1) {
    if (onEnded && state.repeat !== "all") return null;
    return 0;
  }
  if (state.shuffle) {
    let next = state.queueIndex;
    while (next === state.queueIndex) {
      next = Math.floor(Math.random() * state.queue.length);
    }
    return next;
  }
  const next = state.queueIndex + 1;
  if (next < state.queue.length) return next;
  if (state.repeat === "all") return 0;
  return null;
}

export function pickPrevIndex(state: PlayerState): number | null {
  if (state.queue.length === 0) return null;
  if (state.shuffle) {
    if (state.queue.length === 1) return 0;
    let prev = state.queueIndex;
    while (prev === state.queueIndex) {
      prev = Math.floor(Math.random() * state.queue.length);
    }
    return prev;
  }
  const prev = state.queueIndex - 1;
  return prev >= 0 ? prev : null;
}

export function usePlayer() {
  const { streamingQuality } = useUiPreferences();
  const qualityRef = useRef<StreamingQuality>(streamingQuality);
  useEffect(() => {
    qualityRef.current = streamingQuality;
    // The preload cache is keyed on (track_id, quality). When the
    // user flips quality we invalidate it server-side — otherwise
    // the next auto-advance would consume the old-quality MPD. This
    // is fire-and-forget; if the request fails we just eat one
    // un-gapless transition until the next preload fires.
    api.player.preloadClear().catch(() => {
      /* ignore — falls back to slow-path load() */
    });
  }, [streamingQuality]);

  const [state, setState] = useState<PlayerState>(() => {
    const persisted = loadPersisted();
    const repeat: RepeatMode =
      persisted.repeat === "all" || persisted.repeat === "one"
        ? persisted.repeat
        : "off";
    const queue: Track[] = Array.isArray(persisted.queue)
      ? persisted.queue
      : [];

    // Restore "now playing" with two paths:
    //   1. Queue-based: the persisted queueIndex points at a track
    //      whose id still matches what was loaded at quit. Used by
    //      album / playlist / mix sessions.
    //   2. Standalone-track: queueIndex is -1 (single-track play
    //      from a search result, etc.) but the persisted Track
    //      object's id matches the persisted trackId. The track was
    //      never in a queue context but we still want to restore it.
    //
    // Bails to a clean (track: null, queueIndex: -1) state if neither
    // path is satisfied — better to show an empty player than the
    // wrong track. The same parser handles the backend backstop
    // hydration in the effect below (when WKWebView dropped
    // localStorage between launches).
    const { restoreIndex, restoreTrack, restoreCurrentTime } =
      pickRestoreFromPersisted(persisted, queue);

    return {
      ...INITIAL,
      queue,
      queueIndex: restoreIndex,
      track: restoreTrack,
      currentTime: restoreCurrentTime,
      // duration becomes accurate once the backend's first snapshot
      // arrives after the restore load(). Seed from the track's known
      // duration so the progress bar renders something useful in the
      // brief window before that snapshot.
      duration: restoreTrack?.duration ?? 0,
      volume: typeof persisted.volume === "number" ? persisted.volume : 1,
      shuffle: !!persisted.shuffle,
      repeat,
    };
  });

  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  // Backend backstop hydration. The useState initializer above seeds
  // from localStorage, which is the fast path. But pywebview's
  // WKWebView on macOS sometimes loses localStorage between launches
  // (the OS may discard the data store on app quit depending on the
  // mode pywebview opens it in), so the localStorage path can come
  // up empty even though the user expected their track back. Server
  // keeps a parallel copy of the persisted snapshot in
  // `user_data_dir/now_playing.json`; if localStorage was empty,
  // pull from there and apply the same shape-validated restore as
  // the synchronous initializer. Idempotent via a ref so re-runs
  // don't re-trigger.
  const backendHydratedRef = useRef(false);
  useEffect(() => {
    if (backendHydratedRef.current) return;
    backendHydratedRef.current = true;
    if (stateRef.current.track) return; // already hydrated from localStorage
    void (async () => {
      const { state: backendState } = await api.nowPlayingState.get();
      if (!backendState || typeof backendState !== "object") return;
      // The backend just round-trips the JSON we wrote, so the
      // shape matches PersistedState. Cast through unknown to
      // satisfy TS since the API method types it as a generic dict.
      const persisted = backendState as unknown as Partial<PersistedState>;
      const queue: Track[] = Array.isArray(persisted.queue)
        ? persisted.queue
        : [];
      const repeat: RepeatMode =
        persisted.repeat === "all" || persisted.repeat === "one"
          ? persisted.repeat
          : "off";
      const { restoreIndex, restoreTrack, restoreCurrentTime } =
        pickRestoreFromPersisted(persisted, queue);
      if (!restoreTrack) return;
      setState((s) => ({
        ...s,
        queue,
        queueIndex: restoreIndex,
        track: restoreTrack,
        currentTime: restoreCurrentTime,
        duration: restoreTrack.duration ?? s.duration,
        volume:
          typeof persisted.volume === "number" ? persisted.volume : s.volume,
        shuffle: !!persisted.shuffle,
        repeat,
      }));
    })();
  }, []);

  // Persist queue + prefs so a relaunch picks up where the user left
  // off. Skip writes when nothing's loaded so we don't clobber a real
  // prior session.
  //
  // Three lifecycle hooks listen for "the user is leaving":
  //   - `beforeunload`: classic page-close. Fires reliably on most
  //     browser exits and on pywebview window-close.
  //   - `pagehide`: fires in cases where `beforeunload` doesn't —
  //     Safari mobile, BFCache navigations, and some embedded-webview
  //     paths. Most reliable last-chance hook in the spec.
  //   - `visibilitychange` to "hidden": fires when the app loses focus
  //     or is sent to background. macOS Cmd-Q sometimes hides the
  //     window before tearing it down without firing beforeunload, so
  //     a flush here catches it.
  // All three call the same `flush()` so a triple-hit on quit just
  // writes the same JSON three times — cheap and idempotent.
  useEffect(() => {
    const flush = () => {
      const s = stateRef.current;
      // Skip writes when nothing's loaded — don't clobber a real prior
      // session. We allow currentTime === 0 here because a paused-at-
      // start track is still worth persisting (the user explicitly
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
      // Backend backstop. Pywebview's WKWebView on macOS doesn't
      // always preserve localStorage between launches — depending
      // on the data-store mode the OS may discard the database on
      // app quit. The server keeps the most-recently-pushed
      // snapshot in user_data_dir/now_playing.json, so a quit that
      // wipes localStorage still restores cleanly. Fire-and-forget;
      // the .catch lives on the api method itself.
      api.nowPlayingState.put(snapshot as unknown as Record<string, unknown>);
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
  }, []);

  // SSE subscription — backend pushes state changes + position updates.
  // `seq` is our monotonic clock so we can ignore out-of-order frames.
  const lastSeqRef = useRef(-1);
  const expectedTrackIdRef = useRef<string | null>(null);
  const endOfTrackPendingRef = useRef(false);
  // advanceRef is set by a later effect so the SSE "ended" handler can
  // call pickNextIndex against the freshest state without reinstalling
  // the subscription every queue change.
  const advanceRef = useRef<() => void>(() => {});
  // Tracks which track_id we've already preloaded-next-for so we
  // don't fire /api/player/preload once per position tick after
  // crossing the 15s-remaining threshold. Resets when track_id
  // changes.
  const preloadedForTrackIdRef = useRef<string | null>(null);
  // Track ids we've already kicked off a rehydrate fetch for. Keeps
  // us from firing the same GET /api/track/{id} on every position
  // tick when the frontend has no cached Track for the current id.
  // Happens after a page reload: the backend keeps playing, the
  // frontend boots with an empty queue, and the first snapshot
  // arrives with a track id we cannot render without metadata.
  const rehydratingTrackIdRef = useRef<string | null>(null);

  useEffect(() => {
    const url = "/api/player/events";
    let es: EventSource | null = null;
    let cancelled = false;
    let retryTimer: number | null = null;

    const applySnapshot = (snap: PlayerSnapshot) => {
      // Ignore frames that don't match the track we think is current —
      // a late echo from a track we already replaced.
      if (
        snap.track_id !== null &&
        expectedTrackIdRef.current !== null &&
        snap.track_id !== expectedTrackIdRef.current
      ) {
        return;
      }
      // Sync the now-playing bar to whatever the backend says is
      // playing. Two sources, in priority order:
      //
      //   1. Queue-sourced (fast path): if the queue contains the
      //      track_id, point state.track at it directly. This is
      //      the source of truth for autoplay / radio takeover —
      //      `playAtIndex` already pushed the full Track object into
      //      the queue, we just hand it back to the bar. No HTTP.
      //
      //   2. API-sourced (cold-boot rehydrate): the backend is
      //      playing something we don't have in the queue at all
      //      — happens after a reload while the backend session is
      //      still live. Fetch the Track dict and splice it in.
      //
      // Why path 1 exists: relying on `playAtIndex`'s optimistic
      // setState alone has a race. The SSE "playing" snapshot can
      // arrive before stateRef catches up (useEffect commits stateRef
      // *after* the React render, so a snapshot processed during the
      // render window sees stale stateRef.current.track), at which
      // point the rehydrate API fetch returns the same track we just
      // put in the queue — extra round trip for nothing, and during
      // the in-flight window the bar shows the previous track.
      const cur = stateRef.current;
      const trackOutOfSync =
        snap.track_id !== null &&
        snap.state !== "idle" &&
        snap.state !== "ended" &&
        (!cur.track || cur.track.id !== snap.track_id);
      if (trackOutOfSync && snap.track_id) {
        const id = snap.track_id;
        const queueIdx = cur.queue.findIndex((q) => q.id === id);
        if (queueIdx >= 0) {
          // Path 1: queue has it. Sync state.track from queue.
          expectedTrackIdRef.current = id;
          setState((s) => {
            // Re-check inside the reducer: another setState may have
            // already synced track (e.g. playAtIndex's optimistic
            // update commits between the outer check and here).
            if (s.track?.id === id) return s;
            const idx = s.queue.findIndex((q) => q.id === id);
            if (idx < 0) return s;
            return { ...s, track: s.queue[idx], queueIndex: idx };
          });
        } else if (rehydratingTrackIdRef.current !== id) {
          // Path 2: cold-boot rehydrate. Queue is empty / doesn't
          // contain this track. Fetch from API.
          rehydratingTrackIdRef.current = id;
          expectedTrackIdRef.current = id;
          api
            .track(id)
            .then((t) => {
              setState((s) => {
                // Race guard: if the backend has since moved on to
                // a different track, drop this late response.
                if (expectedTrackIdRef.current !== id) return s;
                // If something else (e.g. playAtIndex on a manual
                // queue change) put the track into the queue while
                // our fetch was in flight, prefer the queue's index
                // over a single-track replacement.
                const existingIdx = s.queue.findIndex((q) => q.id === id);
                if (existingIdx >= 0) {
                  return { ...s, track: t, queueIndex: existingIdx };
                }
                return {
                  ...s,
                  track: t,
                  queue: [t],
                  queueIndex: 0,
                };
              });
            })
            .catch(() => {
              // Leave the bar in its pre-rehydrate state if the
              // fetch fails. Next snapshot retries via the same gate.
              rehydratingTrackIdRef.current = null;
            });
        }
      }
      setState((s) => {
        const currentTime = snap.position_ms / 1000;
        const duration =
          snap.duration_ms > 0 ? snap.duration_ms / 1000 : s.duration;
        if (snap.state === "ended") {
          // Don't mutate track immediately — the advance ref will
          // load the next one. Just reflect the brief pause.
          return {
            ...s,
            playing: false,
            loading: false,
            currentTime: 0,
            // Keep streamInfo so the quality badge doesn't flicker off
            // in the brief window before the next track loads.
          };
        }
        return {
          ...s,
          playing: snap.state === "playing",
          loading: snap.state === "loading",
          error:
            snap.error ?? (snap.state === "error" ? "Playback failed" : null),
          currentTime,
          duration,
          streamInfo: snap.stream_info,
          forceVolume: !!snap.force_volume,
        };
      });
      if (snap.state === "ended") advanceRef.current();
      // Skip past tracks the backend can't play. Without this,
      // autoplay halts the moment radio hands us a region-locked or
      // otherwise unstreamable id — the user sees a "Playback failed"
      // banner and the music stops mid-session. The expected-track
      // guard above naturally dedupes repeat error frames for the
      // same track, so we only advance once per failure.
      if (snap.state === "error" && continuePlayingRef.current) {
        advanceRef.current();
      }

      // Preload the next queue item about ten seconds into the
      // current track. The old rule was fifteen seconds from the
      // end. That was fine for natural gapless transitions but
      // left mid track skips cold. If you hit Next forty five
      // seconds into a four minute track the backend had to fetch
      // the manifest and prime a decoder from scratch, which took
      // three to five hundred milliseconds before any audio came
      // out. Spotify and Apple Music both fire preload early for
      // exactly this reason.
      //
      // This only runs when playback is actually in the playing
      // state, when we haven't already preloaded for this track,
      // when there is a next track in the queue, and when the
      // next track isn't the same as the current one (which
      // happens under repeat one and wouldn't be worth the work).
      //
      // The memory cost is one pre decoded PCM buffer held for
      // the rest of the current track. A five minute FLAC at
      // 96 kHz and 32 bit runs about 180 MB. Shorter or lower
      // resolution tracks cost proportionally less. The buffer is
      // freed as soon as the swap completes or the queue changes.
      if (
        snap.state === "playing" &&
        snap.track_id !== null &&
        preloadedForTrackIdRef.current !== snap.track_id
      ) {
        const currentTime = snap.position_ms / 1000;
        if (currentTime >= 10) {
          const s = stateRef.current;
          const nextIdx = pickNextIndex(s, true);
          if (nextIdx !== null && nextIdx !== s.queueIndex) {
            const nextTrack = s.queue[nextIdx];
            if (nextTrack && nextTrack.id !== snap.track_id) {
              preloadedForTrackIdRef.current = snap.track_id;
              api.player.preload(nextTrack.id, qualityRef.current).catch(() => {
                /* fire-and-forget; failure falls back to the
                     normal slow-path load on track-end. */
              });
            }
          }
        }
      }
      // Reset the preload guard when track_id changes so the next
      // track's own preload fires at its own trigger point.
      if (
        snap.track_id !== null &&
        preloadedForTrackIdRef.current !== null &&
        preloadedForTrackIdRef.current !== snap.track_id
      ) {
        // Only reset when we've moved past the track we preloaded
        // FOR — i.e. the current track_id is different from the one
        // whose end triggered the preload. Avoids re-firing mid-
        // track.
        preloadedForTrackIdRef.current = null;
      }
    };

    const connect = () => {
      if (cancelled) return;
      es = new EventSource(url);
      es.onmessage = (event) => {
        try {
          const snap = JSON.parse(event.data) as PlayerSnapshot;
          if (snap.seq <= lastSeqRef.current) return;
          lastSeqRef.current = snap.seq;
          applySnapshot(snap);
        } catch {
          /* malformed frame */
        }
      };
      es.onerror = () => {
        // Browser auto-reconnects; if it closes us out entirely, try a
        // manual reconnect after a short delay.
        if (es && es.readyState === EventSource.CLOSED) {
          es.close();
          es = null;
          if (cancelled) return;
          retryTimer = window.setTimeout(connect, 1500);
        }
      };
    };

    connect();
    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      if (es) es.close();
    };
  }, []);

  // Apply the restored volume to the backend once on mount.
  const initialVolumeApplied = useRef(false);
  useEffect(() => {
    if (initialVolumeApplied.current) return;
    initialVolumeApplied.current = true;
    api.player.volume(Math.round(state.volume * 100)).catch(() => {});
  }, [state.volume]);

  // macOS Now Playing metadata push. The backend's PlayerSnapshot has
  // track_id but no title / artist / album / cover, and on macOS we
  // need that data on Tideway's MPNowPlayingInfo dict for the menu-
  // bar widget and Control Center to render anything useful (and to
  // keep media keys routed to us — without a populated entry, macOS
  // can fall back to Apple Music). Fire on every track change with
  // the data the frontend already has. Fire-and-forget; the bridge
  // no-ops on non-macOS so this is safe to ship unconditionally.
  const lastNowPlayingIdRef = useRef<string | null>(null);
  useEffect(() => {
    const track = state.track;
    if (!track) {
      lastNowPlayingIdRef.current = null;
      return;
    }
    if (lastNowPlayingIdRef.current === track.id) return;
    lastNowPlayingIdRef.current = track.id;
    const artistName =
      Array.isArray(track.artists) && track.artists.length > 0
        ? track.artists
            .map((a) => a.name)
            .filter(Boolean)
            .join(", ")
        : "";
    api.player
      .nowPlaying({
        title: track.name || "",
        artist: artistName,
        album: track.album?.name ?? "",
        duration_ms: Math.round((track.duration ?? 0) * 1000),
      })
      .catch(() => {
        /* fire-and-forget */
      });
  }, [state.track]);

  // "Continue playing after queue ends" preference, mirrored from
  // the backend Settings dataclass. Read on mount and refreshed
  // whenever the SettingsPage dispatches the `tidal-settings-updated`
  // event (it does this after a successful PUT). Held in a ref so
  // the queue-end branch in advanceRef can read the latest value
  // without taking it as a dep — the advance code path is set up
  // once and would otherwise need to redefine on every settings
  // change.
  //
  // Default true here mirrors the backend default. If the settings
  // fetch fails (no auth, network), keep the default-on behavior so
  // the user gets the same experience the backend ships.
  const continuePlayingRef = useRef(true);
  useEffect(() => {
    let cancelled = false;
    api.settings
      .get()
      .then((s) => {
        if (!cancelled) {
          continuePlayingRef.current = !!s.continue_playing_after_queue_ends;
        }
      })
      .catch(() => {
        /* default: true (set above) */
      });
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail as {
        continue_playing_after_queue_ends?: boolean;
      } | null;
      if (
        detail &&
        typeof detail.continue_playing_after_queue_ends === "boolean"
      ) {
        continuePlayingRef.current = detail.continue_playing_after_queue_ends;
      }
    };
    window.addEventListener("tidal-settings-updated", handler);
    return () => {
      cancelled = true;
      window.removeEventListener("tidal-settings-updated", handler);
    };
  }, []);

  // Restore the persisted "now playing" track on app launch. We seed
  // the track + queueIndex + currentTime synchronously in the state
  // initializer so the UI shows it immediately; this effect catches
  // up the backend by loading the manifest and seeking to the saved
  // position — paused, never auto-resumed.
  //
  // Skipped when the backend is already playing something else (a
  // refresh during active playback): the existing rehydrate path
  // inside applySnapshot wins, and we don't want to overwrite that
  // session with our stale persisted track.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    const cur = stateRef.current;
    // Standalone-track plays restore with `queueIndex = -1`, so the
    // restore is keyed solely on whether a track was hydrated by the
    // initializer — not on whether a queue position was hydrated.
    if (!cur.track) return;
    restoredRef.current = true;

    void (async () => {
      let backendIdle = true;
      try {
        const snap = await api.player.state();
        backendIdle = snap.state === "idle";
      } catch {
        // Backend unreachable. Treat as idle so we still seed the UI;
        // the load() below will surface the actual error if the
        // backend really is down.
      }
      if (!backendIdle) {
        // Backend is playing or paused on its own session — defer to
        // the rehydrate path in applySnapshot. Don't stomp on it.
        return;
      }

      const track = cur.track;
      if (!track) return;
      expectedTrackIdRef.current = track.id;
      try {
        const loaded = await api.player.load(track.id, qualityRef.current);
        const durationSec = loaded.duration_ms / 1000;
        if (durationSec > 0 && cur.currentTime > 0) {
          // Clamp into a valid fraction. Past-end positions can show
          // up after a track was edited / replaced server-side; clamp
          // them to 0 rather than landing at end-of-track and
          // immediately firing `ended`.
          const fraction = cur.currentTime / durationSec;
          if (fraction > 0 && fraction < 0.999) {
            await api.player.seek(fraction);
          }
        }
        // No setState for `loading` here. PCMPlayer.load()
        // transitions to "paused" when it returns; the SSE pushes
        // that snapshot, and applySnapshot updates state.loading
        // accordingly. The backend snapshot is the source of truth
        // for transport state — duplicating it here would just
        // create two ways for it to disagree.
      } catch {
        // Track is no longer streamable (region / license change /
        // stale id). Clear the persisted now-playing so the UI
        // doesn't show a track that won't play.
        setState((s) => ({
          ...s,
          track: null,
          queueIndex: -1,
          currentTime: 0,
          duration: 0,
        }));
        expectedTrackIdRef.current = null;
      }
    })();
    // Depend on state.track: when the backend backstop hydration
    // populates it asynchronously (localStorage was empty), this
    // effect re-runs and the ref-gated body fires for the first
    // time. With an empty deps array the effect only runs once on
    // mount, missing the backend-hydrated case.
  }, [state.track]);

  const playAtIndex = useCallback(
    (
      index: number,
      queueOverride?: Track[],
      sourceOverride?: PlaySource | null,
    ) => {
      // Bounds-check against the queue before the optimistic state
      // update so we can set refs up front without worrying about
      // rolling them back on a no-op. setState reducers should be
      // pure; ref writes belong outside the updater.
      const resolvedQueue = queueOverride ?? stateRef.current.queue;
      if (index < 0 || index >= resolvedQueue.length) return;
      const track = resolvedQueue[index];
      expectedTrackIdRef.current = track.id;
      endOfTrackPendingRef.current = false;
      setState((s) => {
        const queue = queueOverride ?? s.queue;
        if (index < 0 || index >= queue.length) return s;
        // Optimistic UI: track/loading/queueIndex update immediately
        // so the now-playing bar reflects the selection before the
        // backend answers.
        // Source override only applies when a new queue is supplied —
        // advancing within an existing queue (Next button, natural
        // track-end) keeps whatever source started the queue.
        const next = {
          ...s,
          track,
          queue,
          queueIndex: index,
          playing: false,
          loading: true,
          error: null,
          currentTime: 0,
          duration: track.duration ?? 0,
          source:
            sourceOverride !== undefined
              ? sourceOverride
              : queueOverride
                ? null
                : s.source,
        };
        void (async () => {
          try {
            // Single round-trip for load+play — saves ~20-40ms of
            // await gap between the two HTTP calls and lets the
            // backend start priming the decoder immediately after
            // set_media, which is where the audible delay lives.
            await api.player.playTrack(track.id, qualityRef.current);
          } catch (err) {
            setState((cur) => ({
              ...cur,
              playing: false,
              loading: false,
              error: err instanceof Error ? err.message : String(err),
            }));
          }
        })();
        return next;
      });
    },
    [],
  );

  // Load a queue position into the backend without auto-playing —
  // mirrors playAtIndex but stops at the paused-with-decoder-primed
  // state. Used at album-end so we can show track 0 ready-to-play
  // without immediately starting it (Spotify / Apple Music default).
  const loadAtIndexPaused = useCallback(
    (
      index: number,
      queueOverride?: Track[],
      sourceOverride?: PlaySource | null,
    ) => {
      const resolvedQueue = queueOverride ?? stateRef.current.queue;
      if (index < 0 || index >= resolvedQueue.length) return;
      const track = resolvedQueue[index];
      expectedTrackIdRef.current = track.id;
      endOfTrackPendingRef.current = false;
      setState((s) => {
        const queue = queueOverride ?? s.queue;
        if (index < 0 || index >= queue.length) return s;
        const next: PlayerState = {
          ...s,
          track,
          queue,
          queueIndex: index,
          playing: false,
          loading: true,
          error: null,
          currentTime: 0,
          duration: track.duration ?? 0,
          source:
            sourceOverride !== undefined
              ? sourceOverride
              : queueOverride
                ? null
                : s.source,
        };
        void (async () => {
          try {
            await api.player.load(track.id, qualityRef.current);
          } catch (err) {
            setState((cur) => ({
              ...cur,
              playing: false,
              loading: false,
              error: err instanceof Error ? err.message : String(err),
            }));
          }
        })();
        return next;
      });
    },
    [],
  );

  useEffect(() => {
    advanceRef.current = () => {
      if (endOfTrackPendingRef.current) {
        endOfTrackPendingRef.current = false;
        return;
      }
      const n = pickNextIndex(stateRef.current, true);
      if (n !== null) {
        playAtIndex(n);
        return;
      }
      // Queue ended with no next index. Behavior tree:
      //
      //   "Continue playing" toggle ON (default):
      //     - Append an Artist Radio mix seeded from the LAST played
      //       track's primary artist and auto-play the first new
      //       track. Source-agnostic — works for albums, playlists,
      //       mixes, single-track plays, anything where the queue
      //       can run out.
      //     - On any failure (no artist on the last track, radio
      //       fetch error, empty results) fall back to the toggle-
      //       OFF branch so the user still gets a sensible end-state.
      //
      //   "Continue playing" toggle OFF:
      //     - Album source: prime track 0 paused so one tap of Play
      //       repeats the album. The Spotify / Apple Music "queue
      //       finished but stay on the album" pattern.
      //     - Everything else: stop and clear.
      const cur = stateRef.current;
      const stopAndClear = () => {
        api.player.stop().catch(() => {});
        setState((s) => ({
          ...s,
          playing: false,
          loading: false,
          currentTime: 0,
          queueIndex: -1,
          track: null,
        }));
      };
      const fallbackOff = () => {
        if (cur.source?.type === "ALBUM" && cur.queue.length > 0) {
          loadAtIndexPaused(0);
        } else {
          stopAndClear();
        }
      };

      if (continuePlayingRef.current && cur.queue.length > 0) {
        const lastTrack = cur.queue[cur.queueIndex];
        const artistId = lastTrack?.artists?.[0]?.id;
        if (artistId) {
          void (async () => {
            try {
              const radio = await api.artistRadio(String(artistId));
              if (!radio || radio.length === 0) {
                fallbackOff();
                return;
              }
              // De-dupe against what just finished so we don't
              // immediately replay one of the tracks the user was
              // just listening to.
              const playedIds = new Set(cur.queue.map((t) => t.id));
              const fresh = radio.filter((t) => !playedIds.has(t.id));
              const radioTail = fresh.length > 0 ? fresh : radio;
              const newQueue = [...cur.queue, ...radioTail];
              playAtIndex(cur.queueIndex + 1, newQueue, {
                type: "ARTIST",
                id: String(artistId),
              });
            } catch {
              fallbackOff();
            }
          })();
          return;
        }
        // No artist id on the last track — fall through.
      }
      fallbackOff();
    };
  }, [playAtIndex, loadAtIndexPaused]);

  const play = useCallback(
    (track: Track, contextTracks?: Track[], source?: PlaySource | null) => {
      let queue =
        contextTracks && contextTracks.length > 0 ? contextTracks : [track];
      let index = queue.findIndex((t) => t.id === track.id);
      if (index < 0) {
        queue = [track, ...queue];
        index = 0;
      }
      playAtIndex(index, queue, source ?? null);
    },
    [playAtIndex],
  );

  const toggle = useCallback(() => {
    const s = stateRef.current;
    if (!s.track) return;
    if (s.playing) {
      void api.player.pause().catch(() => {});
    } else {
      void api.player.resume().catch(() => {});
    }
  }, []);

  const next = useCallback(() => {
    // The naive answer is `pickNextIndex(stateRef.current)`, but
    // stateRef updates through a useEffect that runs *after* React
    // commits — so a quick second Next click before that commit
    // sees stale `queueIndex`, computes the same next index we
    // already asked for, and the user sees the skip as the song
    // restarting from the beginning. `expectedTrackIdRef` is set
    // synchronously inside `playAtIndex`, so it's the truth-of-
    // what-we-just-asked-for; resolve it back to an index here.
    const s = stateRef.current;
    const intendedId = expectedTrackIdRef.current;
    const intendedIdx = intendedId
      ? s.queue.findIndex((t) => t.id === intendedId)
      : -1;
    const fromIdx = intendedIdx >= 0 ? intendedIdx : s.queueIndex;
    const n = pickNextIndex({ ...s, queueIndex: fromIdx });
    if (n !== null) playAtIndex(n);
  }, [playAtIndex]);

  const prev = useCallback(() => {
    // >3s in: restart the current track, matching Spotify/Apple Music.
    if (stateRef.current.currentTime > 3) {
      void api.player.seek(0).catch(() => {});
      return;
    }
    // Same stale-stateRef guard as `next`; see comment there.
    const s = stateRef.current;
    const intendedId = expectedTrackIdRef.current;
    const intendedIdx = intendedId
      ? s.queue.findIndex((t) => t.id === intendedId)
      : -1;
    const fromIdx = intendedIdx >= 0 ? intendedIdx : s.queueIndex;
    const p = pickPrevIndex({ ...s, queueIndex: fromIdx });
    if (p !== null) playAtIndex(p);
  }, [playAtIndex]);

  const seek = useCallback((t: number) => {
    const s = stateRef.current;
    const cap = s.duration || Infinity;
    const clamped = Math.max(0, Math.min(cap, t));
    const fraction = cap && cap > 0 && cap !== Infinity ? clamped / cap : 0;
    setState((cur) => ({ ...cur, currentTime: clamped }));
    void api.player.seek(fraction).catch(() => {});
  }, []);

  const stop = useCallback(() => {
    expectedTrackIdRef.current = null;
    void api.player.stop().catch(() => {});
    setState(INITIAL);
  }, []);

  const setVolume = useCallback((v: number) => {
    const clamped = Math.max(0, Math.min(1, v));
    setState((s) => ({ ...s, volume: clamped }));
    void api.player.volume(Math.round(clamped * 100)).catch(() => {});
  }, []);

  const toggleShuffle = useCallback(() => {
    setState((s) => ({ ...s, shuffle: !s.shuffle }));
  }, []);

  const cycleRepeat = useCallback(() => {
    setState((s) => ({
      ...s,
      repeat: s.repeat === "off" ? "all" : s.repeat === "all" ? "one" : "off",
    }));
  }, []);

  // Global media-key bridge. The backend's pynput listener publishes
  // hotkey actions (play_pause / next / previous) to /api/hotkey/events
  // — we subscribe once and map each onto the local hook action.
  // Queue advance logic stays here; the backend is just the courier.
  // Mirror the three actions into refs so the subscription effect is
  // install-once and never re-runs when the callbacks change identity.
  const toggleRef = useRef(toggle);
  const nextRef = useRef(next);
  const prevRef = useRef(prev);
  useEffect(() => {
    toggleRef.current = toggle;
    nextRef.current = next;
    prevRef.current = prev;
  }, [toggle, next, prev]);

  useEffect(() => {
    let es: EventSource | null = null;
    let cancelled = false;
    let retryTimer: number | null = null;

    const connect = () => {
      if (cancelled) return;
      es = new EventSource("/api/hotkey/events");
      es.onmessage = (event) => {
        try {
          const { action } = JSON.parse(event.data) as { action?: string };
          if (action === "play_pause") toggleRef.current?.();
          else if (action === "next") nextRef.current?.();
          else if (action === "previous") prevRef.current?.();
        } catch {
          /* malformed frame */
        }
      };
      es.onerror = () => {
        // Same pattern the /api/player/events subscription uses: if
        // the browser decides the connection is truly closed (not
        // just reconnecting), tear down and retry after a short
        // delay. Keeps the listener alive across transient backend
        // restarts without piling up dangling connections.
        if (es && es.readyState === EventSource.CLOSED) {
          es.close();
          es = null;
          if (cancelled) return;
          retryTimer = window.setTimeout(connect, 1500);
        }
      };
    };

    connect();
    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      if (es) es.close();
    };
  }, []);

  const sleepTimeoutRef = useRef<number | null>(null);
  const sleepTickRef = useRef<number | null>(null);
  const [sleepRemaining, setSleepRemaining] = useState<number | null>(null);

  const clearSleepTimer = useCallback(() => {
    if (sleepTimeoutRef.current !== null) {
      window.clearTimeout(sleepTimeoutRef.current);
      sleepTimeoutRef.current = null;
    }
    if (sleepTickRef.current !== null) {
      window.clearTimeout(sleepTickRef.current);
      sleepTickRef.current = null;
    }
    endOfTrackPendingRef.current = false;
    setSleepRemaining(null);
  }, []);

  useEffect(() => clearSleepTimer, [clearSleepTimer]);

  const setSleepTimer = useCallback(
    (minutes: number | "end-of-track" | null) => {
      clearSleepTimer();
      if (minutes === null) return;
      if (minutes === "end-of-track") {
        endOfTrackPendingRef.current = true;
        setSleepRemaining(-1);
        return;
      }
      const ms = minutes * 60 * 1000;
      const fireAt = Date.now() + ms;
      setSleepRemaining(ms);
      sleepTimeoutRef.current = window.setTimeout(() => {
        void api.player.pause().catch(() => {});
        clearSleepTimer();
      }, ms);
      const tick = () => {
        const left = fireAt - Date.now();
        if (left <= 0) {
          sleepTickRef.current = null;
          return;
        }
        setSleepRemaining(left);
        sleepTickRef.current = window.setTimeout(tick, 1000);
      };
      sleepTickRef.current = window.setTimeout(tick, 1000);
    },
    [clearSleepTimer],
  );

  // Append to the end of the queue. Always appends — duplicates pass
  // through so a user who queues the same track twice gets two
  // entries (matches the no-dedupe ask). When the queue is empty,
  // this seeds a single-track queue without auto-playing; the user
  // hits Play to start.
  const addToQueue = useCallback((track: Track) => {
    setState((s) => ({ ...s, queue: [...s.queue, track] }));
  }, []);

  const jumpTo = useCallback(
    (index: number) => {
      playAtIndex(index);
    },
    [playAtIndex],
  );

  const removeFromQueue = useCallback((index: number) => {
    setState((s) => {
      if (index < 0 || index >= s.queue.length) return s;
      if (index === s.queueIndex) return s;
      const nextQueue = [...s.queue];
      nextQueue.splice(index, 1);
      const nextIdx = index < s.queueIndex ? s.queueIndex - 1 : s.queueIndex;
      return { ...s, queue: nextQueue, queueIndex: nextIdx };
    });
  }, []);

  const clearQueue = useCallback(() => {
    setState((s) => {
      if (s.queueIndex < 0) return { ...s, queue: [] };
      return { ...s, queue: [s.queue[s.queueIndex]], queueIndex: 0 };
    });
  }, []);

  return {
    ...state,
    hasNext: state.queueIndex >= 0 && state.queueIndex < state.queue.length - 1,
    hasPrev: state.queueIndex > 0,
    sleepRemaining,
    play,
    toggle,
    next,
    prev,
    seek,
    stop,
    setVolume,
    toggleShuffle,
    cycleRepeat,
    setSleepTimer,
    clearSleepTimer,
    addToQueue,
    jumpTo,
    removeFromQueue,
    clearQueue,
  };
}

export type Player = ReturnType<typeof usePlayer>;
