import { createContext, useContext, useMemo, type ReactNode } from "react";
import { usePlayer, type Player, type RepeatMode } from "./usePlayer";
import { usePlayerNative } from "./usePlayerNative";
import { useUiPreferences } from "./useUiPreferences";
import type { Track } from "@/api/types";

/**
 * Splits the unified `Player` value into three contexts so consumers only
 * re-render when the slice they care about changes. In particular:
 *
 *  - Meta changes when the user plays/pauses, skips, reorders, etc. — i.e.
 *    on deliberate actions, not continuously.
 *  - Time changes every ~250ms from `timeupdate`.
 *  - Actions are stable function references for the lifetime of the player.
 *
 * Without this split, the whole app re-rendered at 4Hz during playback
 * because Shell called `usePlayer()` and passed the returned object down
 * as a prop.
 */

interface PlayerMeta {
  track: Track | null;
  playing: boolean;
  loading: boolean;
  error: string | null;
  volume: number;
  shuffle: boolean;
  repeat: RepeatMode;
  queue: Track[];
  queueIndex: number;
  hasNext: boolean;
  hasPrev: boolean;
}

interface PlayerSleep {
  /** Remaining sleep-timer time in ms. -1 = "stop at end of track". null = off. */
  sleepRemaining: number | null;
}

interface PlayerTime {
  currentTime: number;
  duration: number;
}

// Actions = every method the player exposes. Never changes reference-wise
// beyond the brief window when the audio element is first instantiated.
type PlayerActions = Pick<
  Player,
  | "play"
  | "toggle"
  | "next"
  | "prev"
  | "seek"
  | "stop"
  | "setVolume"
  | "toggleShuffle"
  | "cycleRepeat"
  | "setSleepTimer"
  | "clearSleepTimer"
  | "playNext"
  | "jumpTo"
  | "removeFromQueue"
  | "clearQueue"
>;

const MetaCtx = createContext<PlayerMeta | null>(null);
const TimeCtx = createContext<PlayerTime | null>(null);
const ActionsCtx = createContext<PlayerActions | null>(null);
// Split out so the 1 Hz countdown tick during an active sleep timer
// doesn't force every PlayerMeta consumer (hundreds of track rows) to
// re-render. Only the SleepTimerButton subscribes here.
const SleepCtx = createContext<PlayerSleep | null>(null);

export function PlayerProvider({ children }: { children: ReactNode }) {
  // Pick the engine based on the user's preference. React remounts the
  // child provider when this flag flips, which tears down the inactive
  // engine's resources (audio elements or SSE subscription) cleanly.
  const { nativeEngine } = useUiPreferences();
  return nativeEngine ? (
    <NativePlayerProvider>{children}</NativePlayerProvider>
  ) : (
    <HtmlPlayerProvider>{children}</HtmlPlayerProvider>
  );
}

function HtmlPlayerProvider({ children }: { children: ReactNode }) {
  const player = usePlayer();
  return <PlayerProviderInner player={player}>{children}</PlayerProviderInner>;
}

function NativePlayerProvider({ children }: { children: ReactNode }) {
  const player = usePlayerNative();
  return <PlayerProviderInner player={player}>{children}</PlayerProviderInner>;
}

function PlayerProviderInner({
  player,
  children,
}: {
  player: Player;
  children: ReactNode;
}) {

  // Each memo's deps list is *exactly* what should trigger its consumers.
  const meta = useMemo<PlayerMeta>(
    () => ({
      track: player.track,
      playing: player.playing,
      loading: player.loading,
      error: player.error,
      volume: player.volume,
      shuffle: player.shuffle,
      repeat: player.repeat,
      queue: player.queue,
      queueIndex: player.queueIndex,
      hasNext: player.hasNext,
      hasPrev: player.hasPrev,
    }),
    [
      player.track,
      player.playing,
      player.loading,
      player.error,
      player.volume,
      player.shuffle,
      player.repeat,
      player.queue,
      player.queueIndex,
      player.hasNext,
      player.hasPrev,
    ],
  );

  const sleep = useMemo<PlayerSleep>(
    () => ({ sleepRemaining: player.sleepRemaining }),
    [player.sleepRemaining],
  );

  const time = useMemo<PlayerTime>(
    () => ({ currentTime: player.currentTime, duration: player.duration }),
    [player.currentTime, player.duration],
  );

  const actions = useMemo<PlayerActions>(
    () => ({
      play: player.play,
      toggle: player.toggle,
      next: player.next,
      prev: player.prev,
      seek: player.seek,
      stop: player.stop,
      setVolume: player.setVolume,
      toggleShuffle: player.toggleShuffle,
      cycleRepeat: player.cycleRepeat,
      setSleepTimer: player.setSleepTimer,
      clearSleepTimer: player.clearSleepTimer,
      playNext: player.playNext,
      jumpTo: player.jumpTo,
      removeFromQueue: player.removeFromQueue,
      clearQueue: player.clearQueue,
    }),
    [
      player.play,
      player.toggle,
      player.next,
      player.prev,
      player.seek,
      player.stop,
      player.setVolume,
      player.toggleShuffle,
      player.cycleRepeat,
      player.setSleepTimer,
      player.clearSleepTimer,
      player.playNext,
      player.jumpTo,
      player.removeFromQueue,
      player.clearQueue,
    ],
  );

  return (
    <MetaCtx.Provider value={meta}>
      <TimeCtx.Provider value={time}>
        <SleepCtx.Provider value={sleep}>
          <ActionsCtx.Provider value={actions}>{children}</ActionsCtx.Provider>
        </SleepCtx.Provider>
      </TimeCtx.Provider>
    </MetaCtx.Provider>
  );
}

function assertProvider<T>(value: T | null, name: string): T {
  if (value === null)
    throw new Error(`${name} must be used inside <PlayerProvider>`);
  return value;
}

export function usePlayerMeta(): PlayerMeta {
  return assertProvider(useContext(MetaCtx), "usePlayerMeta");
}

export function usePlayerTime(): PlayerTime {
  return assertProvider(useContext(TimeCtx), "usePlayerTime");
}

export function usePlayerActions(): PlayerActions {
  return assertProvider(useContext(ActionsCtx), "usePlayerActions");
}

export function usePlayerSleep(): PlayerSleep {
  return assertProvider(useContext(SleepCtx), "usePlayerSleep");
}
