import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, useEffect, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Track } from "@/api/types";
import type { StreamingQuality } from "@/hooks/useUiPreferences";
import type { PlayerState } from "./usePlayer";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * The persisted snapshot carries the streaming quality so the backend's
 * cold-start prefetch can warm the manifest under the same (track,
 * quality) key the restore's load() asks for.
 */

vi.mock("@/api/client", () => ({
  api: { nowPlayingState: { put: vi.fn() } },
}));

const { api } = await import("@/api/client");
const { usePlayerPersistence } = await import("./usePlayerPersistence");

const putMock = api.nowPlayingState.put as ReturnType<typeof vi.fn>;

const track = { id: "t1", name: "Song" } as unknown as Track;

const state = {
  queue: [track],
  queueIndex: 0,
  currentTime: 42,
  volume: 0.5,
  shuffle: false,
  shuffleOrder: [],
  repeat: "off",
  track,
} as unknown as PlayerState;

let container: HTMLDivElement;
let root: Root;

function Probe({ quality }: { quality: StreamingQuality }) {
  const stateRef = useRef(state);
  const qualityRef = useRef(quality);
  // Same shape as usePlayer, which keeps its ref current in an effect.
  useEffect(() => {
    qualityRef.current = quality;
  }, [quality]);
  usePlayerPersistence(stateRef, qualityRef);
  return null;
}

beforeEach(() => {
  vi.useFakeTimers();
  putMock.mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  localStorage.clear();
});

describe("usePlayerPersistence", () => {
  it("persists the quality the restore will load at", () => {
    act(() => root.render(<Probe quality="hi_res_lossless" />));
    act(() => vi.advanceTimersByTime(2000));

    expect(putMock).toHaveBeenCalled();
    const snapshot = putMock.mock.lastCall![0];
    expect(snapshot).toMatchObject({
      trackId: "t1",
      quality: "hi_res_lossless",
    });
    expect(JSON.parse(localStorage.getItem("tideway:player")!)).toMatchObject({
      quality: "hi_res_lossless",
    });
  });

  it("follows a quality change", () => {
    act(() => root.render(<Probe quality="hi_res_lossless" />));
    act(() => root.render(<Probe quality="low_320k" />));
    act(() => vi.advanceTimersByTime(2000));

    expect(putMock.mock.lastCall![0]).toMatchObject({
      quality: "low_320k",
    });
  });
});
