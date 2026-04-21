import { useCallback, useEffect, useRef, useState } from "react";
import type { Track } from "@/api/types";
import { useUiPreferences, type StreamingQuality } from "./useUiPreferences";

/** Build the audio element's `src` for a track, honoring the current
 *  streaming-quality preference. The backend serves local files straight
 *  from disk regardless of `quality`, so this parameter only matters for
 *  non-downloaded tracks. */
function buildTrackSrc(track: Track, quality: StreamingQuality): string {
  return `/api/play/${track.id}?quality=${quality}`;
}

/** Turn the browser's generic playback failure into something a user can
 *  actually act on. Max streaming has the most failure modes (needs PKCE
 *  login, Max-tier entitlement, and Chrome has to natively decode the
 *  hi-res FLAC) so we special-case it to point the user at the quality
 *  picker. For other tiers we fall back to the generic message. */
function playbackErrorMessage(
  quality: StreamingQuality,
  mediaError: MediaError | null,
): string {
  if (quality === "hi_res_lossless") {
    return "Max streaming failed for this track — try switching to Lossless in the quality picker.";
  }
  if (mediaError?.code === MediaError.MEDIA_ERR_DECODE) {
    return "The audio format isn't supported in this browser.";
  }
  if (mediaError?.code === MediaError.MEDIA_ERR_NETWORK) {
    return "Network error while loading the track.";
  }
  return "Playback blocked or unsupported.";
}

const PERSIST_KEY = "tidal-downloader:player";
// How often we flush the player state snapshot to localStorage. The queue,
// index, and volume are cheap to write; we also piggyback the current time
// so a reload resumes near where you left off.
const PERSIST_INTERVAL_MS = 5000;

export type RepeatMode = "off" | "all" | "one";

interface PersistedState {
  queue: Track[];
  queueIndex: number;
  currentTime: number;
  volume: number;
  shuffle: boolean;
  repeat: RepeatMode;
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
};

/**
 * Pick the next queue index given the current state.
 *
 * `onEnded` is true when the track just finished naturally — in that case
 * `repeat: "one"` loops the same index. When the user hits Next manually
 * we always advance regardless of repeat mode.
 */
export function pickNextIndex(state: PlayerState, onEnded = false): number | null {
  if (state.queue.length === 0) return null;
  if (onEnded && state.repeat === "one") return state.queueIndex;
  // Single-track queue gets its own branch: a natural end shouldn't loop
  // the only track unless the user asked for repeat. Manual Next still
  // replays it (matches what Spotify does).
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

// When current track is this close to the end, start loading the next one
// into the preload slot so the transition is gapless.
const PRELOAD_AT_RATIO = 0.85;

export function usePlayer() {
  // Two audio elements let us achieve gapless playback: we preload the
  // next track into the inactive slot while the current one plays out,
  // then instantly swap slots on `ended`. Without this, swapping `src`
  // on a single element always leaves a ~200ms decoder-reset gap.
  const [slots, setSlots] = useState<[HTMLAudioElement | null, HTMLAudioElement | null]>([
    null,
    null,
  ]);
  const [activeIdx, setActiveIdx] = useState<0 | 1>(0);
  const audio = slots[activeIdx];
  const preload = slots[activeIdx === 0 ? 1 : 0];

  // Streaming-quality preference. Read through a ref inside callbacks
  // so the rest of the hook's memoized actions don't churn when the
  // user picks a new quality — only the reload effect below cares.
  const { streamingQuality } = useUiPreferences();
  const qualityRef = useRef<StreamingQuality>(streamingQuality);
  useEffect(() => {
    qualityRef.current = streamingQuality;
  }, [streamingQuality]);

  useEffect(() => {
    if (typeof Audio === "undefined") return;
    const a = new Audio();
    a.preload = "auto";
    const b = new Audio();
    b.preload = "auto";
    setSlots([a, b]);
    return () => {
      a.pause();
      a.removeAttribute("src");
      b.pause();
      b.removeAttribute("src");
    };
  }, []);

  const [state, setState] = useState<PlayerState>(() => {
    const persisted = loadPersisted();
    const repeat: RepeatMode =
      persisted.repeat === "all" || persisted.repeat === "one" ? persisted.repeat : "off";
    return {
      ...INITIAL,
      queue: Array.isArray(persisted.queue) ? persisted.queue : [],
      // queueIndex stays at -1 until the user actively presses play; we stage
      // the persisted queue so they can resume from the queue panel.
      queueIndex: -1,
      volume: typeof persisted.volume === "number" ? persisted.volume : 1,
      shuffle: !!persisted.shuffle,
      repeat,
    };
  });

  // Apply the restored volume to both elements once they exist, and keep
  // them in lockstep so the gapless swap doesn't introduce a volume jump.
  useEffect(() => {
    const [a, b] = slots;
    if (a) a.volume = state.volume;
    if (b) b.volume = state.volume;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slots, state.volume]);

  // Mirror state into a ref so stable callbacks (event handlers) can read
  // the latest without being in their dep array.
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  // Persist on an interval rather than on every state change — writes that
  // fire from `timeupdate` (~4/sec) would otherwise thrash localStorage.
  // Deliberately NOT flushing on effect cleanup: StrictMode's synthetic
  // unmount would overwrite the stored queue with an empty one before the
  // user has played anything.
  useEffect(() => {
    const flush = () => {
      const s = stateRef.current;
      // Skip flushing the empty initial state; that would clobber whatever
      // the last session persisted before the user started interacting.
      if (s.queue.length === 0 && s.queueIndex === -1 && s.currentTime === 0) return;
      try {
        const snapshot: PersistedState = {
          queue: s.queue,
          queueIndex: s.queueIndex,
          currentTime: s.currentTime,
          volume: s.volume,
          shuffle: s.shuffle,
          repeat: s.repeat,
        };
        localStorage.setItem(PERSIST_KEY, JSON.stringify(snapshot));
      } catch {
        /* storage full or disabled — ignore */
      }
    };
    const id = window.setInterval(flush, PERSIST_INTERVAL_MS);
    window.addEventListener("beforeunload", flush);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("beforeunload", flush);
    };
  }, []);

  // Plays a specific queue index on the currently-active audio slot. If
  // the preload slot already has the requested track buffered (gapless
  // advance), we hot-swap slots instead of restarting a decoder.
  //
  // Any call to playAtIndex that isn't from `onEnded` represents the user
  // manually navigating. Cancel the "end of current track" sleep mode if
  // it's active — otherwise the timer would survive the skip and stop
  // playback at the end of the *new* track, which isn't what the user meant.
  // Reaching playAtIndex from onEnded is harmless: onEnded already bails
  // when endOfTrackPendingRef is true, so the flag is always false here.
  const playAtIndex = useCallback(
    (index: number, queue?: Track[]) => {
      const [a, b] = slots;
      if (!a || !b) return;
      const effectiveQueue = queue ?? stateRef.current.queue;
      const track = effectiveQueue[index];
      if (!track) return;

      if (endOfTrackPendingRef.current) {
        endOfTrackPendingRef.current = false;
        setSleepRemaining(null);
      }

      const expectedSrc = buildTrackSrc(track, qualityRef.current);
      const preloadEl = activeIdx === 0 ? b : a;
      const activeEl = activeIdx === 0 ? a : b;

      // Gapless swap path: preload already holds this track at the
      // current quality and is ready.
      if (
        preloadEl.src &&
        preloadEl.src.endsWith(expectedSrc) &&
        preloadEl.readyState >= 3 // HAVE_FUTURE_DATA
      ) {
        activeEl.pause();
        activeEl.removeAttribute("src");
        preloadEl.currentTime = 0;
        setActiveIdx((i) => (i === 0 ? 1 : 0));
        // Explicitly set playing: true. Without this, the pause event
        // from activeEl.pause() above can briefly flip state.playing to
        // false between the swap and the new element's play event —
        // producing a visible control flicker.
        setState((prev) => ({
          ...prev,
          queue: effectiveQueue,
          track,
          queueIndex: index,
          playing: true,
          loading: false,
          error: null,
          currentTime: 0,
          duration: track.duration || 0,
        }));
        preloadEl.play().catch((err: DOMException) => {
          if (err?.name === "AbortError") return;
          setState((prev) => ({
            ...prev,
            playing: false,
            loading: false,
            error: playbackErrorMessage(qualityRef.current, preloadEl.error),
          }));
        });
        return;
      }

      // Normal path: load into the active slot.
      activeEl.src = expectedSrc;
      setState((prev) => ({
        ...prev,
        queue: effectiveQueue,
        track,
        queueIndex: index,
        loading: true,
        error: null,
        currentTime: 0,
        duration: track.duration || 0,
      }));
      activeEl.play().catch((err: DOMException) => {
        if (err?.name === "AbortError") return;
        setState((prev) => ({
          ...prev,
          loading: false,
          error: playbackErrorMessage(qualityRef.current, activeEl.error),
        }));
      });
    },
    [slots, activeIdx],
  );

  // Wire native audio events → state. Attached once; `playAtIndex` is stable
  // because `audio` is stable, so this effect never re-runs during normal
  // playback and we avoid tearing down the element mid-song.
  useEffect(() => {
    if (!audio) return;
    const onPlay = () => setState((s) => ({ ...s, playing: true, loading: false }));
    const onPause = () => setState((s) => ({ ...s, playing: false }));
    const onTime = () => setState((s) => ({ ...s, currentTime: audio.currentTime }));
    const onMeta = () =>
      setState((s) => {
        // Tidal's DASH FLAC first segment carries a STREAMINFO whose
        // total_samples refers to that fragment, not the whole track —
        // so <audio>.duration from a live stream can come back as a
        // wrong, small finite number (e.g. 10s for a 4-min track) and
        // make the scrub bar fill up long before the song ends. The
        // Tidal-metadata `track.duration` seeded into `s.duration` is
        // always accurate, so only accept audio.duration when we don't
        // already have one OR when it's within 5% of the seeded value
        // (local files / cached stream files, where STREAMINFO is
        // correct, land in this case).
        if (!isFinite(audio.duration) || audio.duration <= 0) return s;
        if (s.duration <= 0) return { ...s, duration: audio.duration };
        const ratio = audio.duration / s.duration;
        if (ratio >= 0.95 && ratio <= 1.05) {
          return { ...s, duration: audio.duration };
        }
        return s;
      });
    const onWaiting = () => setState((s) => ({ ...s, loading: true }));
    const onCanPlay = () => setState((s) => ({ ...s, loading: false }));
    const onError = () => {
      // Log the MediaError details to the console so we can diagnose
      // format-vs-network failures when the user reports playback bugs.
      // (MediaError.code: 1=aborted, 2=network, 3=decode, 4=src-not-supported.)
      const me = audio.error;
      if (me) {
        console.error(
          `[audio] error code=${me.code} message=${me.message || "(none)"} src=${audio.src}`,
        );
      }
      setState((s) => ({
        ...s,
        loading: false,
        playing: false,
        error: playbackErrorMessage(qualityRef.current, me),
      }));
    };
    const onEnded = () => {
      // Sleep timer set to "end of track"? Stop here instead of advancing.
      if (endOfTrackPendingRef.current) {
        endOfTrackPendingRef.current = false;
        setSleepRemaining(null);
        setState((s) => ({ ...s, playing: false }));
        return;
      }
      const next = pickNextIndex(stateRef.current, /* onEnded */ true);
      if (next !== null) playAtIndex(next);
      else setState((s) => ({ ...s, playing: false }));
    };

    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("loadedmetadata", onMeta);
    audio.addEventListener("durationchange", onMeta);
    audio.addEventListener("waiting", onWaiting);
    audio.addEventListener("canplay", onCanPlay);
    audio.addEventListener("error", onError);
    audio.addEventListener("ended", onEnded);

    return () => {
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("loadedmetadata", onMeta);
      audio.removeEventListener("durationchange", onMeta);
      audio.removeEventListener("waiting", onWaiting);
      audio.removeEventListener("canplay", onCanPlay);
      audio.removeEventListener("error", onError);
      audio.removeEventListener("ended", onEnded);
    };
  }, [audio, playAtIndex]);

  /**
   * Gapless preload. Derived once per render into a boolean so the effect
   * only re-runs when we cross the threshold — not on every `timeupdate`
   * (which would be 4+ times per second).
   */
  const shouldPreloadNext =
    state.duration > 0 && state.currentTime >= state.duration * PRELOAD_AT_RATIO;

  useEffect(() => {
    if (!preload || !shouldPreloadNext) return;
    const s = stateRef.current;
    const next = pickNextIndex(s);
    if (next === null) return;
    const nextTrack = s.queue[next];
    if (!nextTrack) return;
    const expected = buildTrackSrc(nextTrack, qualityRef.current);
    if (preload.src && preload.src.endsWith(expected)) return;
    preload.src = expected;
    // Starts buffering without autoplay.
    preload.load();
    // Deps are the dimensions that could change which track comes next AND
    // whether we're in the preload window. Deliberately NOT `state.currentTime`
    // — that would fire 4×/sec.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    preload,
    shouldPreloadNext,
    state.queueIndex,
    state.queue.length,
    state.shuffle,
    state.repeat,
  ]);

  // When the user changes streaming quality mid-playback, reload the
  // current track at the new quality and resume from where they were.
  // Device-code sessions in particular play at ~320k AAC by default; a
  // user flipping to Lossless should hear the change on the track
  // they're listening to, not only on the next one.
  useEffect(() => {
    if (!audio || !state.track) return;
    const expected = buildTrackSrc(state.track, streamingQuality);
    if (audio.src.endsWith(expected)) return;
    const wasPlaying = !audio.paused;
    const resumeAt = audio.currentTime;
    // Setting currentTime synchronously after src change no-ops in most
    // browsers — duration isn't known until loadedmetadata fires. Defer
    // the seek until the new media is ready, otherwise quality changes
    // silently restart playback from 0.
    const onReady = () => {
      audio.currentTime = resumeAt;
      audio.removeEventListener("loadedmetadata", onReady);
    };
    audio.addEventListener("loadedmetadata", onReady);
    audio.src = expected;
    // Drop the preload too — it was buffered at the old quality.
    if (preload) {
      preload.pause();
      preload.removeAttribute("src");
    }
    if (wasPlaying) {
      audio.play().catch(() => {
        /* user can hit play again if the browser blocks it */
      });
    }
    // Only the quality should trigger this effect. `audio` / `state.track`
    // changes are handled by playAtIndex; we don't want to re-fire here
    // and stomp on their state transitions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streamingQuality]);

  /**
   * Start playback. If `contextTracks` is provided, it becomes the queue
   * (starting at the given track). Otherwise the queue is a single-track list.
   */
  const play = useCallback(
    (track: Track, contextTracks?: Track[]) => {
      if (!audio) return;
      let queue = contextTracks && contextTracks.length > 0 ? contextTracks : [track];
      let index = queue.findIndex((t) => t.id === track.id);
      // If the starting track isn't in the context, prepend it — otherwise
      // we'd silently fall through to queue[0] and play the wrong song.
      if (index < 0) {
        queue = [track, ...queue];
        index = 0;
      }
      playAtIndex(index, queue);
    },
    [audio, playAtIndex],
  );

  const toggle = useCallback(() => {
    if (!audio || !stateRef.current.track) return;
    if (audio.paused) {
      audio.play().catch((err: DOMException) => {
        if (err?.name === "AbortError") return;
        setState((s) => ({ ...s, error: "Playback blocked or unsupported." }));
      });
    } else {
      audio.pause();
    }
  }, [audio]);

  const next = useCallback(() => {
    const n = pickNextIndex(stateRef.current);
    if (n !== null) playAtIndex(n);
  }, [playAtIndex]);

  const prev = useCallback(() => {
    // If we're more than 3 seconds into the track, restart it instead of
    // going back — matches Spotify/Apple Music behavior.
    if (audio && audio.currentTime > 3) {
      audio.currentTime = 0;
      return;
    }
    const p = pickPrevIndex(stateRef.current);
    if (p !== null) playAtIndex(p);
  }, [audio, playAtIndex]);

  const seek = useCallback(
    (t: number) => {
      if (!audio) return;
      // Clamp against state.duration (track.duration from Tidal) rather
      // than audio.duration — on a live DASH FLAC stream audio.duration
      // can be wrong (see onMeta above), and clamping against it would
      // make the scrub bar refuse to go past a fake early "end".
      const cap = stateRef.current.duration || audio.duration || Infinity;
      audio.currentTime = Math.max(0, Math.min(cap, t));
    },
    [audio],
  );

  const stop = useCallback(() => {
    const [a, b] = slots;
    // Tear down BOTH slots — otherwise a preloaded-but-not-yet-playing
    // element keeps buffering bytes from the server even after the user
    // closes the player.
    if (a) {
      a.pause();
      a.removeAttribute("src");
    }
    if (b) {
      b.pause();
      b.removeAttribute("src");
    }
    setState(INITIAL);
  }, [slots]);

  const setVolume = useCallback(
    (v: number) => {
      if (!audio) return;
      const clamped = Math.max(0, Math.min(1, v));
      audio.volume = clamped;
      setState((s) => ({ ...s, volume: clamped }));
    },
    [audio],
  );

  const toggleShuffle = useCallback(() => {
    setState((s) => ({ ...s, shuffle: !s.shuffle }));
  }, []);

  const cycleRepeat = useCallback(() => {
    setState((s) => ({
      ...s,
      repeat: s.repeat === "off" ? "all" : s.repeat === "all" ? "one" : "off",
    }));
  }, []);

  /**
   * Sleep timer: pauses playback after a set delay, or at the end of the
   * currently-playing track.
   *
   * `pause` is deliberate — not `stop` — so the queue survives and the user
   * can hit play again later. A second flag (`endOfTrackPendingRef`) is
   * read by the `onEnded` handler to decide whether to advance or stop.
   */
  // Fire timer — the actual "pause playback" callback.
  const sleepTimeoutRef = useRef<number | null>(null);
  // 1 Hz countdown timer that updates sleepRemaining for the UI. Must be
  // tracked separately from sleepTimeoutRef so we can clear it on cancel.
  const sleepTickRef = useRef<number | null>(null);
  const endOfTrackPendingRef = useRef(false);
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

  // Tear down any pending sleep timer on unmount so it can't fire against
  // an orphaned component / stale setState.
  useEffect(() => clearSleepTimer, [clearSleepTimer]);

  const setSleepTimer = useCallback(
    (minutes: number | "end-of-track" | null) => {
      clearSleepTimer();
      if (minutes === null) return;
      if (minutes === "end-of-track") {
        endOfTrackPendingRef.current = true;
        setSleepRemaining(-1); // sentinel: "when current ends"
        return;
      }
      const ms = minutes * 60 * 1000;
      const fireAt = Date.now() + ms;
      setSleepRemaining(ms);
      sleepTimeoutRef.current = window.setTimeout(() => {
        if (audio) audio.pause();
        clearSleepTimer();
      }, ms);
      // Update countdown at 1 Hz so the UI can render it. The actual stop
      // uses setTimeout above — this just keeps the display in sync.
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
    [audio, clearSleepTimer],
  );

  const playNext = useCallback((track: Track) => {
    setState((s) => {
      // queueIndex < 0 means the queue is *staged* — e.g. resumed from
      // localStorage on boot with nothing playing yet. Don't replace it
      // with just `[track]`; append so the user's prior queue survives.
      // They can still reach the new track by starting playback and
      // skipping to it, and the staged queue isn't wiped just because
      // they used the Play-Next context menu before pressing play.
      if (s.queueIndex < 0) {
        const already = s.queue.some((t) => t.id === track.id);
        if (already) return s;
        return { ...s, queue: [...s.queue, track] };
      }
      const nextQueue = [...s.queue];
      nextQueue.splice(s.queueIndex + 1, 0, track);
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
    playNext,
    jumpTo,
    removeFromQueue,
    clearQueue,
  };
}

export type Player = ReturnType<typeof usePlayer>;
