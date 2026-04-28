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
const PERSIST_INTERVAL_MS = 5000;

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
   *  versions). The track object itself comes from the queue, not
   *  from this field. */
  trackId: string | null;
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

    // Restore "now playing" only when the persisted queueIndex points
    // at a track whose id still matches what we wrote out at quit.
    // Bails to a clean (track: null, queueIndex: -1) state if anything
    // looks off — better to show an empty player than the wrong track.
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
      if (
        typeof persisted.currentTime === "number" &&
        persisted.currentTime > 0
      ) {
        restoreCurrentTime = persisted.currentTime;
      }
    }

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

  // Persist queue + prefs on an interval so a reload picks up where
  // the user left off. Skip writes when the state is still the empty
  // initial one so we don't clobber a real prior session.
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
      try {
        const snapshot: PersistedState = {
          queue: s.queue,
          queueIndex: s.queueIndex,
          currentTime: s.currentTime,
          volume: s.volume,
          shuffle: s.shuffle,
          repeat: s.repeat,
          trackId: s.track?.id ?? null,
        };
        localStorage.setItem(PERSIST_KEY, JSON.stringify(snapshot));
      } catch {
        /* storage full or disabled */
      }
    };
    const id = window.setInterval(flush, PERSIST_INTERVAL_MS);
    window.addEventListener("beforeunload", flush);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("beforeunload", flush);
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
      // Rehydrate the now-playing bar after a reload. When the
      // backend is already playing something and the frontend has
      // no record of the track (empty queue on boot), fetch the
      // full Track dict and drop it into state so the bar can
      // render name, artist, cover, etc. Only fires once per id.
      const cur = stateRef.current;
      const needsRehydrate =
        snap.track_id !== null &&
        snap.state !== "idle" &&
        snap.state !== "ended" &&
        (!cur.track || cur.track.id !== snap.track_id) &&
        rehydratingTrackIdRef.current !== snap.track_id;
      if (needsRehydrate && snap.track_id) {
        const id = snap.track_id;
        rehydratingTrackIdRef.current = id;
        expectedTrackIdRef.current = id;
        api
          .track(id)
          .then((t) => {
            setState((s) => {
              // Race guard: if the backend has since moved on to a
              // different track, drop this late response.
              if (expectedTrackIdRef.current !== id) return s;
              // Splice the rehydrated track into the queue at a
              // deterministic index so Next/Prev arithmetic works.
              // If the queue already contains this track, use that
              // index; otherwise prepend a single-track queue.
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
            // Leave the bar in its pre-rehydrate state if the fetch
            // fails. Next snapshot will retry via the same gate.
            rehydratingTrackIdRef.current = null;
          });
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

  // Album-end "continue with artist radio" preference, mirrored from
  // the backend Settings dataclass. Read on mount and refreshed
  // whenever the SettingsPage dispatches the `tidal-settings-updated`
  // event (it does this after a successful PUT). Held in a ref so
  // the album-end branch in advanceRef can read the latest value
  // without taking it as a dep — the advance code path is set up
  // once and would otherwise need to redefine on every settings
  // change.
  const continueRadioRef = useRef(false);
  useEffect(() => {
    let cancelled = false;
    api.settings
      .get()
      .then((s) => {
        if (!cancelled) {
          continueRadioRef.current = !!s.continue_with_artist_radio_after_album;
        }
      })
      .catch(() => {
        /* default: false */
      });
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail as {
        continue_with_artist_radio_after_album?: boolean;
      } | null;
      if (
        detail &&
        typeof detail.continue_with_artist_radio_after_album === "boolean"
      ) {
        continueRadioRef.current =
          detail.continue_with_artist_radio_after_album;
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
    if (!cur.track || cur.queueIndex < 0) return;
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
  }, []);

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
      // Queue ended with no next index. Three behaviors:
      //   1. Album source AND user has the "continue with artist
      //      radio" setting on: append an artist radio mix and
      //      auto-play the first new track. Asynchronous (radio
      //      fetch); falls back to behavior 2 on any error.
      //   2. Album source (default): re-prime track 0 of the same
      //      queue, paused. Standard Spotify / Apple Music behavior.
      //      One tap of Play repeats the album.
      //   3. Anything else (playlist, mix, artist, single track,
      //      unknown source): stop and clear, same as before.
      const cur = stateRef.current;
      if (cur.source?.type === "ALBUM" && cur.queue.length > 0) {
        if (continueRadioRef.current) {
          // Behavior 1: artist radio takeover.
          const lastTrack = cur.queue[cur.queueIndex];
          const artistId = lastTrack?.artists?.[0]?.id;
          if (artistId) {
            void (async () => {
              try {
                const radio = await api.artistRadio(String(artistId));
                if (!radio || radio.length === 0) {
                  // No radio results — fall back to pause-on-track-0.
                  loadAtIndexPaused(0);
                  return;
                }
                // Filter out tracks already in the album queue so we
                // don't immediately replay what just finished.
                const albumIds = new Set(cur.queue.map((t) => t.id));
                const fresh = radio.filter((t) => !albumIds.has(t.id));
                const radioTail = fresh.length > 0 ? fresh : radio;
                const newQueue = [...cur.queue, ...radioTail];
                playAtIndex(cur.queueIndex + 1, newQueue, {
                  type: "ARTIST",
                  id: String(artistId),
                });
              } catch {
                // Network / API failure — graceful fallback.
                loadAtIndexPaused(0);
              }
            })();
            return;
          }
          // Missing artist on the last track — fall through to the
          // default album-end behavior.
        }
        // Behavior 2: pause on track 0.
        loadAtIndexPaused(0);
        return;
      }
      // Behavior 3: stop.
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
    const n = pickNextIndex(stateRef.current);
    if (n !== null) playAtIndex(n);
  }, [playAtIndex]);

  const prev = useCallback(() => {
    // >3s in: restart the current track, matching Spotify/Apple Music.
    if (stateRef.current.currentTime > 3) {
      void api.player.seek(0).catch(() => {});
      return;
    }
    const p = pickPrevIndex(stateRef.current);
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
