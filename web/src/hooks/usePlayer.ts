import { useCallback, useEffect, useRef, useState } from "react";
import type { PlayerSnapshot, StreamInfo, Track } from "@/api/types";
import { api } from "@/api/client";
import { useContinuePlayingPref } from "./useContinuePlayingPref";
import { useSleepTimer } from "./useSleepTimer";
import { useUiPreferences, type StreamingQuality } from "./useUiPreferences";
import {
  loadPersisted,
  pickRestoreFromPersisted,
  usePlayerPersistence,
  type PersistedState,
} from "./usePlayerPersistence";

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

/**
 * Compute the queue + jump-target for an Artist Radio takeover at
 * end-of-queue. Pure-ish (only side effect is the
 * `api.artistRadio` HTTP call); returns `null` whenever a
 * takeover can't fire so the caller can fall through to its
 * stop / pause-on-track-0 path.
 *
 * Cases that return null:
 *   - Empty queue (nothing to seed the takeover from).
 *   - The just-played track has no artist (rare — search results
 *     for raw uploads sometimes lack artist metadata).
 *   - Tidal's artist radio endpoint failed or returned empty.
 *
 * Dedupes radio tracks against what's already in the queue so we
 * don't immediately replay one of the tracks the user was just
 * listening to. If the dedupe filters EVERYTHING out (the radio
 * is entirely the album we just played), falls back to the
 * unfiltered radio list — better to repeat a song than to stop
 * playback.
 */
async function fetchRadioTakeover(
  cur: PlayerState,
): Promise<{ newQueue: Track[]; index: number; source: PlaySource } | null> {
  if (cur.queue.length === 0) return null;
  const lastTrack = cur.queue[cur.queueIndex];
  const artistId = lastTrack?.artists?.[0]?.id;
  if (!artistId) return null;
  try {
    const radio = await api.artistRadio(String(artistId));
    if (!radio || radio.length === 0) return null;
    const playedIds = new Set(cur.queue.map((t) => t.id));
    const fresh = radio.filter((t) => !playedIds.has(t.id));
    const radioTail = fresh.length > 0 ? fresh : radio;
    return {
      newQueue: [...cur.queue, ...radioTail],
      index: cur.queueIndex + 1,
      source: { type: "ARTIST", id: String(artistId) },
    };
  } catch {
    return null;
  }
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

  // Persist queue + prefs so a relaunch picks up where the user
  // left off. Owned by usePlayerPersistence — see that file's
  // docstring for the lifecycle-hook trio + backend-backstop story.
  usePlayerPersistence(stateRef);

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

    // Late-echo guard: drop snapshots whose track_id doesn't match
    // the track we last asked the backend to play. Happens during
    // track changes when the previous track's "playing" tail still
    // has frames in flight — without the guard, those would
    // overwrite the new track's UI state.
    const isLateEcho = (snap: PlayerSnapshot): boolean =>
      snap.track_id !== null &&
      expectedTrackIdRef.current !== null &&
      snap.track_id !== expectedTrackIdRef.current;

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
    const syncTrackFromQueueOrApi = (snap: PlayerSnapshot) => {
      const cur = stateRef.current;
      const trackOutOfSync =
        snap.track_id !== null &&
        snap.state !== "idle" &&
        snap.state !== "ended" &&
        (!cur.track || cur.track.id !== snap.track_id);
      if (!trackOutOfSync || !snap.track_id) return;
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
    };

    // Apply the snapshot's transport-state fields to React state.
    // Doesn't touch state.track — that's syncTrackFromQueueOrApi's
    // job and stays separate so the optimistic-update / rehydrate
    // race surface is contained to one place.
    const applyTransportState = (snap: PlayerSnapshot) => {
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
            // Keep streamInfo so the quality badge doesn't flicker
            // off in the brief window before the next track loads.
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
    };

    // Preload the next queue item about ten seconds into the
    // current track. Earlier rule was fifteen seconds from the end;
    // that was fine for natural gapless transitions but left mid-
    // track skips cold. If you hit Next forty-five seconds into a
    // four-minute track the backend had to fetch the manifest and
    // prime a decoder from scratch — three to five hundred ms
    // before any audio came out. Spotify and Apple Music both fire
    // preload early for the same reason.
    //
    // Memory cost is one pre-decoded PCM buffer held for the rest
    // of the current track (~180 MB at 96 kHz / 32-bit FLAC,
    // proportionally less at lower quality / shorter tracks). The
    // buffer is freed as soon as the swap completes or the queue
    // changes.
    const triggerPreloadIfNeeded = (snap: PlayerSnapshot) => {
      if (
        snap.state !== "playing" ||
        snap.track_id === null ||
        preloadedForTrackIdRef.current === snap.track_id
      )
        return;
      const currentTime = snap.position_ms / 1000;
      if (currentTime < 10) return;
      const s = stateRef.current;
      const nextIdx = pickNextIndex(s, true);
      if (nextIdx === null || nextIdx === s.queueIndex) return;
      const nextTrack = s.queue[nextIdx];
      if (!nextTrack || nextTrack.id === snap.track_id) return;
      preloadedForTrackIdRef.current = snap.track_id;
      api.player.preload(nextTrack.id, qualityRef.current).catch(() => {
        /* fire-and-forget; failure falls back to the slow-path
           load on track-end. */
      });
    };

    // Reset the preload guard when track_id changes so the next
    // track's own preload fires at its own 10-second trigger
    // point. Only resets when we've moved PAST the track we
    // preloaded FOR — re-firing mid-track would re-preload the
    // same next track repeatedly.
    const resetPreloadGuardOnTrackChange = (snap: PlayerSnapshot) => {
      if (
        snap.track_id !== null &&
        preloadedForTrackIdRef.current !== null &&
        preloadedForTrackIdRef.current !== snap.track_id
      ) {
        preloadedForTrackIdRef.current = null;
      }
    };

    const applySnapshot = (snap: PlayerSnapshot) => {
      if (isLateEcho(snap)) return;
      syncTrackFromQueueOrApi(snap);
      applyTransportState(snap);
      // Advance triggers fire AFTER the state setState so any
      // ended-branch UI ticks land before the next track's
      // optimistic update overwrites them.
      if (snap.state === "ended") advanceRef.current();
      // Skip past tracks the backend can't play. Without this,
      // autoplay halts the moment radio hands us a region-locked
      // or otherwise unstreamable id. The expected-track guard
      // (isLateEcho) naturally dedupes repeat error frames for the
      // same track, so we only advance once per failure.
      if (snap.state === "error" && continuePlayingRef.current) {
        advanceRef.current();
      }
      triggerPreloadIfNeeded(snap);
      resetPreloadGuardOnTrackChange(snap);
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
  // the backend Settings as a live-updating ref. See
  // useContinuePlayingPref for the mount-fetch + custom-event
  // update mechanism.
  const continuePlayingRef = useContinuePlayingPref();

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

      if (continuePlayingRef.current) {
        void (async () => {
          const takeover = await fetchRadioTakeover(cur);
          if (takeover) {
            playAtIndex(takeover.index, takeover.newQueue, takeover.source);
          } else {
            fallbackOff();
          }
        })();
        return;
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
    if (n !== null) {
      playAtIndex(n);
      return;
    }
    // End of queue with shuffle off + repeat off — explicit stop
    // rather than silent no-op, so clicking Next on the last track
    // of an album produces a predictable result instead of a
    // button that pretends to do nothing. Matches Spotify / Apple
    // Music behaviour, and lets the UI keep the Next button
    // enabled (which a no-op `next` did not justify).
    if (s.queue.length > 0) {
      expectedTrackIdRef.current = null;
      void api.player.stop().catch(() => {});
      setState(INITIAL);
    }
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

  // Sleep-timer state machine. Owned by useSleepTimer; we hand it
  // the endOfTrackPendingRef so the "stop at end of track" mode
  // can flip the flag advanceRef checks.
  const { sleepRemaining, setSleepTimer, clearSleepTimer } =
    useSleepTimer(endOfTrackPendingRef);

  // Append to the end of the queue. Always appends — duplicates pass
  // through so a user who queues the same track twice gets two
  // entries (matches the no-dedupe ask). When the queue is empty,
  // this seeds a single-track queue without auto-playing; the user
  // hits Play to start.
  const addToQueue = useCallback((track: Track) => {
    setState((s) => ({ ...s, queue: [...s.queue, track] }));
  }, []);

  // Insert immediately after the current track. Standard "Play next"
  // pattern from Spotify / Apple Music / official Tidal. Multiple
  // calls in a row stack the inserted tracks in call order, so a
  // user who clicks "Play next" on track A then track B hears A
  // first, B second (matches Apple Music's queue-up semantics).
  //
  // When nothing is playing yet (`queueIndex < 0`), behave like
  // `addToQueue` — there's no "current track" to insert after, so
  // we just seed the queue. Hitting Play starts it.
  const playNext = useCallback((track: Track) => {
    setState((s) => {
      if (s.queueIndex < 0) {
        return { ...s, queue: [...s.queue, track] };
      }
      // Multiple consecutive playNext calls each insert at the slot
      // *after* the previously-inserted track. Tracking the latest
      // play-next slot in state would be over-engineered for this —
      // instead we always insert at queueIndex+1, which means the
      // most recently queued-as-next track plays *first* (LIFO
      // ordering). That's a defensible alternative to Apple Music's
      // FIFO; both are reasonable. Picking LIFO because it's a
      // simpler invariant ("the thing I just clicked plays next")
      // and matches the official Tidal client.
      const insertAt = s.queueIndex + 1;
      const nextQueue = [
        ...s.queue.slice(0, insertAt),
        track,
        ...s.queue.slice(insertAt),
      ];
      return { ...s, queue: nextQueue };
    });
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
    playNext,
    jumpTo,
    removeFromQueue,
    clearQueue,
  };
}

export type Player = ReturnType<typeof usePlayer>;
