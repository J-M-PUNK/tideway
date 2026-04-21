import { useCallback, useEffect, useRef, useState } from "react";
import type { PlayerSnapshot, Track } from "@/api/types";
import { api } from "@/api/client";
import { useUiPreferences, type StreamingQuality } from "./useUiPreferences";
import {
  pickNextIndex,
  pickPrevIndex,
  type PlayerState,
  type RepeatMode,
} from "./usePlayer";

/**
 * Remote-control player hook for the libvlc backend engine.
 *
 * Mirrors the public surface of `usePlayer` (the HTML `<audio>` engine)
 * so downstream consumers don't care which engine is active — but
 * instead of driving an `<audio>` element in the browser, every
 * play/pause/seek/volume call fires an HTTP request at
 * `/api/player/*` and playback state flows back through an SSE stream.
 *
 * What this hook owns:
 *  - Queue / shuffle / repeat state (identical semantics to usePlayer)
 *  - Sleep timer
 *  - Volume (in the 0..1 linear space consumers expect)
 *  - localStorage persistence (same key, same shape)
 *
 * What the backend owns:
 *  - Stream resolution (DASH manifest from tidalapi)
 *  - Decoder / audio output (libvlc)
 *  - Actual position / duration while playing
 *
 * Gapless: not yet. The backend briefly idles between tracks while the
 * next manifest resolves — noticeable but not jarring. A future
 * optimization could pre-resolve the next track mid-playback.
 */
const PERSIST_KEY = "tidal-downloader:player";
const PERSIST_INTERVAL_MS = 5000;

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

export function usePlayerNative() {
  const { streamingQuality } = useUiPreferences();
  const qualityRef = useRef<StreamingQuality>(streamingQuality);
  useEffect(() => {
    qualityRef.current = streamingQuality;
  }, [streamingQuality]);

  const [state, setState] = useState<PlayerState>(() => {
    const persisted = loadPersisted();
    const repeat: RepeatMode =
      persisted.repeat === "all" || persisted.repeat === "one"
        ? persisted.repeat
        : "off";
    return {
      ...INITIAL,
      queue: Array.isArray(persisted.queue) ? persisted.queue : [],
      queueIndex: -1,
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
      if (s.queue.length === 0 && s.queueIndex === -1 && s.currentTime === 0) {
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
  // We reconcile into React state; the backend's `seq` is our monotonic
  // clock so we can ignore out-of-order frames.
  const lastSeqRef = useRef(-1);
  const expectedTrackIdRef = useRef<string | null>(null);
  const endOfTrackPendingRef = useRef(false);
  // The SSE "ended" handler needs to look up pickNextIndex on the
  // *latest* state; we already mirror to stateRef so that's fine. We
  // only need a stable ref for the "advance on end" callback itself.
  const advanceRef = useRef<() => void>(() => {});

  useEffect(() => {
    const url = "/api/player/events";
    let es: EventSource | null = null;
    let cancelled = false;
    let retryTimer: number | null = null;

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
          /* malformed frame, skip */
        }
      };
      es.onerror = () => {
        // Browser will auto-reconnect; if it closes us out entirely,
        // try a manual reconnect after a short delay.
        if (es && es.readyState === EventSource.CLOSED) {
          es.close();
          es = null;
          if (cancelled) return;
          retryTimer = window.setTimeout(connect, 1500);
        }
      };
    };

    const applySnapshot = (snap: PlayerSnapshot) => {
      // Ignore frames that don't match the track we think is current —
      // could be a late echo from a track we already replaced.
      if (
        snap.track_id !== null &&
        expectedTrackIdRef.current !== null &&
        snap.track_id !== expectedTrackIdRef.current
      ) {
        return;
      }
      setState((s) => {
        const currentTime = snap.position_ms / 1000;
        const duration =
          snap.duration_ms > 0 ? snap.duration_ms / 1000 : s.duration;
        if (snap.state === "ended") {
          // Don't mutate track immediately — the advance effect will
          // load the next one. Just mark not-playing so the UI
          // reflects the pause while the next track resolves.
          return {
            ...s,
            playing: false,
            loading: false,
            currentTime: 0,
          };
        }
        return {
          ...s,
          playing: snap.state === "playing",
          loading: snap.state === "loading",
          error: snap.error ?? (snap.state === "error" ? "Playback failed" : null),
          currentTime,
          duration,
        };
      });
      if (snap.state === "ended") {
        advanceRef.current();
      }
    };

    connect();
    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      if (es) es.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Apply the restored volume to the backend once.
  const initialVolumeApplied = useRef(false);
  useEffect(() => {
    if (initialVolumeApplied.current) return;
    initialVolumeApplied.current = true;
    // Fire and forget — volume is a hint; don't block on it.
    api.player.volume(Math.round(state.volume * 100)).catch(() => {});
  }, [state.volume]);

  // -- core action: play a specific queue index ---------------------------
  const playAtIndex = useCallback(
    (index: number, queueOverride?: Track[]) => {
      setState((s) => {
        const queue = queueOverride ?? s.queue;
        if (index < 0 || index >= queue.length) return s;
        const track = queue[index];
        expectedTrackIdRef.current = track.id;
        endOfTrackPendingRef.current = false;
        // Optimistic UI: track/loading/queueIndex update immediately
        // so the now-playing bar reflects the selection even before
        // the backend answers.
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
        };
        // Fire the backend commands after committing state.
        void (async () => {
          try {
            await api.player.load(track.id, qualityRef.current);
            await api.player.play();
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

  // -- advance on natural end --------------------------------------------
  useEffect(() => {
    advanceRef.current = () => {
      if (endOfTrackPendingRef.current) {
        endOfTrackPendingRef.current = false;
        return;
      }
      const n = pickNextIndex(stateRef.current, true);
      if (n !== null) {
        playAtIndex(n);
      } else {
        // Queue exhausted — clear to idle state.
        api.player.stop().catch(() => {});
        setState((s) => ({
          ...s,
          playing: false,
          loading: false,
          currentTime: 0,
          queueIndex: -1,
          track: null,
        }));
      }
    };
  }, [playAtIndex]);

  // -- public actions -----------------------------------------------------
  const play = useCallback(
    (track: Track, contextTracks?: Track[]) => {
      let queue =
        contextTracks && contextTracks.length > 0 ? contextTracks : [track];
      let index = queue.findIndex((t) => t.id === track.id);
      if (index < 0) {
        queue = [track, ...queue];
        index = 0;
      }
      playAtIndex(index, queue);
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

  // -- sleep timer --------------------------------------------------------
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

  // -- queue mutation actions --------------------------------------------
  const playNext = useCallback((track: Track) => {
    setState((s) => {
      // "Play next" before anything is playing just parks in the queue.
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
    hasNext:
      state.queueIndex >= 0 && state.queueIndex < state.queue.length - 1,
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
