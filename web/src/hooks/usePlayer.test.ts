/**
 * Tests for the pure pickPrevIndex helper used by the prev (back-skip)
 * button. The actual button-disabled wiring (`hasPrev`) and the
 * "restart this track when there's no previous" branch in the `prev`
 * callback both depend on this function returning `null` cleanly on
 * the first track of a queue.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import {
  buildShuffleOrder,
  fetchRadioTakeover,
  pickNextIndex,
  pickPrevIndex,
  shuffleOrderAfterInsert,
  shuffleOrderAfterRemove,
  shuffleTracks,
} from "./usePlayer";
import type { PlayerState } from "./usePlayer";
import type { Track } from "@/api/types";

function _track(id: string, artistId = "a1"): Track {
  return {
    kind: "track",
    id,
    name: `Track ${id}`,
    duration: 180,
    track_num: 1,
    explicit: false,
    artists: [{ id: artistId, name: "Artist" }],
    album: null,
  };
}

function _state(overrides: Partial<PlayerState>): PlayerState {
  return {
    track: null,
    playing: false,
    currentTime: 0,
    duration: 0,
    loading: false,
    error: null,
    volume: 1,
    muted: false,
    queue: [],
    queueIndex: -1,
    shuffle: false,
    shuffleOrder: [],
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

describe("pickNextIndex with shuffle", () => {
  const queueOf = (n: number) =>
    Array.from(
      { length: n },
      (_, i) => ({ id: String(i) }) as unknown as PlayerState["queue"][number],
    );

  it("gives the same answer every time it is asked", () => {
    // The bug behind #291. "What plays next?" is asked twice per
    // track — once ~10s in to prime the preload the crossfade will
    // fade into, once when the track actually ends — and shuffle used
    // to answer with a fresh Math.random() each time. The crossfade
    // then faded into one track while the queue advanced to another,
    // which the user hears as an interruption at every song change.
    const s = _state({
      queue: queueOf(8),
      queueIndex: 3,
      shuffle: true,
      shuffleOrder: buildShuffleOrder(8, 3),
    });
    const answers = new Set(
      Array.from({ length: 20 }, () => pickNextIndex(s, true)),
    );
    expect(answers.size).toBe(1);
  });

  it("plays every track once before any repeats", () => {
    const order = buildShuffleOrder(6, 0);
    let s = _state({
      queue: queueOf(6),
      queueIndex: 0,
      shuffle: true,
      shuffleOrder: order,
    });
    const visited = [0];
    for (let i = 0; i < 5; i++) {
      const next = pickNextIndex(s, true);
      expect(next).not.toBeNull();
      visited.push(next as number);
      s = { ...s, queueIndex: next as number };
    }
    expect([...visited].sort((a, b) => a - b)).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it("ends the queue at the end of the order unless repeat is on", () => {
    const order = buildShuffleOrder(4, 0);
    const last = order[order.length - 1];
    const s = _state({
      queue: queueOf(4),
      queueIndex: last,
      shuffle: true,
      shuffleOrder: order,
    });
    expect(pickNextIndex(s, true)).toBeNull();
    expect(pickNextIndex({ ...s, repeat: "all" }, true)).toBe(order[0]);
  });

  it("keeps the current track first so enabling shuffle doesn't skip", () => {
    const order = buildShuffleOrder(10, 7);
    expect(order[0]).toBe(7);
    expect([...order].sort((a, b) => a - b)).toEqual([
      0, 1, 2, 3, 4, 5, 6, 7, 8, 9,
    ]);
  });

  it("plays an appended radio tail after the shuffled queue", () => {
    // End-of-queue autoplay appends to the queue the user is already
    // shuffling through; the new positions aren't in the order yet.
    const s = _state({
      queue: queueOf(5),
      queueIndex: 2,
      shuffle: true,
      shuffleOrder: [2, 0, 1],
    });
    expect(pickNextIndex(s, true)).toBe(0);
    expect(pickNextIndex({ ...s, queueIndex: 1 }, true)).toBe(3);
  });

  it("ignores order entries left over from a shorter queue", () => {
    const s = _state({
      queue: queueOf(2),
      queueIndex: 0,
      shuffle: true,
      shuffleOrder: [0, 9, 1],
    });
    expect(pickNextIndex(s, true)).toBe(1);
  });
});

describe("shuffle order under queue edits", () => {
  it("moves a play-next insert to right after the current track", () => {
    // Queue [A,B,C,D] playing B (index 1), order B,D,A,C. Inserting at
    // index 2 shifts C and D up one.
    const next = shuffleOrderAfterInsert([1, 3, 0, 2], 2, 1);
    expect(next).toEqual([1, 2, 4, 0, 3]);
  });

  it("closes the gap when a track is removed", () => {
    expect(shuffleOrderAfterRemove([1, 3, 0, 2], 2)).toEqual([1, 2, 0]);
  });
});

describe("shuffleTracks", () => {
  it("returns a permutation without mutating the input", () => {
    const input = ["1", "2", "3", "4", "5"].map((id) => _track(id));
    const before = input.map((t) => t.id);
    const out = shuffleTracks(input);
    expect(out).toHaveLength(input.length);
    expect(new Set(out.map((t) => t.id))).toEqual(new Set(before));
    // Input array is untouched (we queue a copy).
    expect(input.map((t) => t.id)).toEqual(before);
  });
});

describe("fetchRadioTakeover", () => {
  afterEach(() => vi.restoreAllMocks());

  it("seeds TRACK radio from the last song and dedupes the played queue", async () => {
    const queue = [_track("1"), _track("2")];
    vi.spyOn(api, "trackRadio").mockResolvedValue([
      _track("2"), // already played — must be deduped out
      _track("3"),
      _track("4"),
    ]);
    const artistSpy = vi.spyOn(api, "artistRadio");

    const res = await fetchRadioTakeover(_state({ queue, queueIndex: 1 }));

    expect(res).not.toBeNull();
    expect(res!.source).toEqual({ type: "TRACK", id: "2" });
    // Original queue preserved, then the deduped radio tail appended.
    const tailIds = new Set(res!.newQueue.slice(2).map((t) => t.id));
    expect(tailIds).toEqual(new Set(["3", "4"]));
    expect(res!.index).toBe(2);
    // Track radio answered, so the artist fallback must not fire.
    expect(artistSpy).not.toHaveBeenCalled();
  });

  it("falls back to ARTIST radio when track radio is empty", async () => {
    vi.spyOn(api, "trackRadio").mockResolvedValue([]);
    vi.spyOn(api, "artistRadio").mockResolvedValue([_track("9", "a1")]);

    const res = await fetchRadioTakeover(
      _state({ queue: [_track("1", "a1")], queueIndex: 0 }),
    );

    expect(res).not.toBeNull();
    expect(res!.source).toEqual({ type: "ARTIST", id: "a1" });
  });

  it("returns null when both radios are empty", async () => {
    vi.spyOn(api, "trackRadio").mockResolvedValue([]);
    vi.spyOn(api, "artistRadio").mockResolvedValue([]);
    const res = await fetchRadioTakeover(
      _state({ queue: [_track("1")], queueIndex: 0 }),
    );
    expect(res).toBeNull();
  });

  it("returns null on an empty queue", async () => {
    const res = await fetchRadioTakeover(_state({ queue: [], queueIndex: -1 }));
    expect(res).toBeNull();
  });
});
