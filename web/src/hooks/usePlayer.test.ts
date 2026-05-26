/**
 * Tests for the pure pickPrevIndex helper used by the prev (back-skip)
 * button. The actual button-disabled wiring (`hasPrev`) and the
 * "restart this track when there's no previous" branch in the `prev`
 * callback both depend on this function returning `null` cleanly on
 * the first track of a queue.
 */
import { describe, expect, it } from "vitest";
import { pickPrevIndex } from "./usePlayer";
import type { PlayerState } from "./usePlayer";

function _state(overrides: Partial<PlayerState>): PlayerState {
  return {
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
    pausedByDevice: null,
    ...overrides,
  };
}

describe("pickPrevIndex", () => {
  it("returns null on an empty queue", () => {
    expect(pickPrevIndex(_state({ queue: [], queueIndex: -1 }))).toBeNull();
  });

  it("returns null on the first track with shuffle off", () => {
    // This is the case that drove the back-button fix: previously the
    // button was disabled (`hasPrev: queueIndex > 0`), so pressing it
    // never reached the `prev` callback's restart-the-track branch.
    // The function STILL returns null here; the callback now treats a
    // null return as "restart current track" rather than "no-op".
    const s = _state({
      queue: [
        { id: "1" } as unknown as PlayerState["queue"][number],
        { id: "2" } as unknown as PlayerState["queue"][number],
      ],
      queueIndex: 0,
      shuffle: false,
    });
    expect(pickPrevIndex(s)).toBeNull();
  });

  it("returns the previous index in the queue with shuffle off", () => {
    const s = _state({
      queue: [
        { id: "1" } as unknown as PlayerState["queue"][number],
        { id: "2" } as unknown as PlayerState["queue"][number],
        { id: "3" } as unknown as PlayerState["queue"][number],
      ],
      queueIndex: 2,
      shuffle: false,
    });
    expect(pickPrevIndex(s)).toBe(1);
  });

  it("returns a non-current index when shuffle is on and queue has multiple tracks", () => {
    const s = _state({
      queue: [
        { id: "1" } as unknown as PlayerState["queue"][number],
        { id: "2" } as unknown as PlayerState["queue"][number],
        { id: "3" } as unknown as PlayerState["queue"][number],
      ],
      queueIndex: 1,
      shuffle: true,
    });
    const result = pickPrevIndex(s);
    expect(result).not.toBeNull();
    expect(result).not.toBe(1);
  });

  it("returns 0 when shuffle is on but the queue has only one track", () => {
    const s = _state({
      queue: [{ id: "1" } as unknown as PlayerState["queue"][number]],
      queueIndex: 0,
      shuffle: true,
    });
    expect(pickPrevIndex(s)).toBe(0);
  });
});
