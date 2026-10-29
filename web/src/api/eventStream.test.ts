import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The app holds one EventSource for player, download and hotkey events
 * instead of three. Three permanently held three of the browser's six
 * HTTP/1.1 connections per host, and cover images queued behind
 * ordinary API calls for want of a free one.
 */

class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];

  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private listeners = new Map<string, Set<EventListener>>();

  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, fn: EventListener) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name)!.add(fn);
  }

  removeEventListener(name: string, fn: EventListener) {
    this.listeners.get(name)?.delete(fn);
  }

  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  emit(name: string, data: string) {
    for (const fn of this.listeners.get(name) ?? []) {
      fn(new MessageEvent(name, { data }));
    }
  }

  listenerCount(name: string) {
    return this.listeners.get(name)?.size ?? 0;
  }

  /** What the browser does when it gives up reconnecting. */
  giveUp() {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.();
  }
}

let subscribeEvents: typeof import("./eventStream").subscribeEvents;

beforeEach(async () => {
  vi.useFakeTimers();
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.resetModules();
  ({ subscribeEvents } = await import("./eventStream"));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const live = () => FakeEventSource.instances.filter((s) => !s.closed);

describe("subscribeEvents", () => {
  it("serves every event name over one connection", () => {
    const player: string[] = [];
    const download: string[] = [];
    subscribeEvents("player", (d) => player.push(d));
    subscribeEvents("download", (d) => download.push(d));
    subscribeEvents("hotkey", () => {});

    expect(FakeEventSource.instances).toHaveLength(1);
    const es = FakeEventSource.instances[0];
    expect(es.url).toBe("/api/events");

    es.emit("player", "p1");
    es.emit("download", "d1");
    es.emit("player", "p2");
    expect(player).toEqual(["p1", "p2"]);
    expect(download).toEqual(["d1"]);
  });

  it("closes with the last subscriber and reopens with the next", () => {
    const offA = subscribeEvents("player", () => {});
    const offB = subscribeEvents("download", () => {});
    const first = FakeEventSource.instances[0];

    offA();
    expect(first.closed).toBe(false);
    expect(first.listenerCount("player")).toBe(0);
    offB();
    expect(first.closed).toBe(true);

    subscribeEvents("hotkey", () => {});
    expect(live()).toHaveLength(1);
    expect(live()[0]).not.toBe(first);
  });

  it("rebuilds the connection when the browser gives up, with backoff", () => {
    const got: string[] = [];
    subscribeEvents("player", (d) => got.push(d));
    FakeEventSource.instances[0].giveUp();
    expect(live()).toHaveLength(0);

    vi.advanceTimersByTime(999);
    expect(live()).toHaveLength(0);
    vi.advanceTimersByTime(1);
    expect(live()).toHaveLength(1);

    // The rebuilt connection carries the existing subscriptions.
    live()[0].emit("player", "after");
    expect(got).toEqual(["after"]);

    // A second failure in a row waits twice as long.
    live()[0].giveUp();
    vi.advanceTimersByTime(1999);
    expect(live()).toHaveLength(0);
    vi.advanceTimersByTime(1);
    expect(live()).toHaveLength(1);

    // A successful open resets the delay.
    live()[0].onopen?.();
    live()[0].giveUp();
    vi.advanceTimersByTime(1000);
    expect(live()).toHaveLength(1);
  });

  it("leaves the browser's own reconnects alone", () => {
    subscribeEvents("player", () => {});
    const es = FakeEventSource.instances[0];
    es.readyState = FakeEventSource.CONNECTING;
    es.onerror?.();
    vi.advanceTimersByTime(10_000);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(es.closed).toBe(false);
  });

  it("does not reconnect once nobody is listening", () => {
    const off = subscribeEvents("player", () => {});
    FakeEventSource.instances[0].giveUp();
    off();
    vi.advanceTimersByTime(10_000);
    expect(live()).toHaveLength(0);
  });

  it("keeps dispatching when one handler throws", () => {
    const got: string[] = [];
    subscribeEvents("player", () => {
      throw new Error("bug");
    });
    subscribeEvents("player", (d) => got.push(d));
    FakeEventSource.instances[0].emit("player", "x");
    expect(got).toEqual(["x"]);
  });
});
