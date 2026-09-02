import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The prefetcher reads live in-flight state from the API client, so
// stub the client rather than the network.
let inFlight = 0;
vi.mock("@/api/client", () => ({
  apiRequestsInFlight: () => inFlight,
}));

async function freshModule() {
  vi.resetModules();
  return await import("./routePrefetch");
}

describe("route chunk prefetch", () => {
  beforeEach(() => {
    inFlight = 0;
    vi.useFakeTimers();
    // Force the setTimeout path so the idle scheduling is deterministic.
    vi.stubGlobal("requestIdleCallback", undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("warms registered chunks once the pool is idle", async () => {
    const mod = await freshModule();
    const a = vi.fn().mockResolvedValue({});
    const b = vi.fn().mockResolvedValue({});
    mod.registerRouteChunk(a);
    mod.registerRouteChunk(b);

    mod.startRoutePrefetch();
    await vi.advanceTimersByTimeAsync(10_000);

    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
  });

  it("does not warm anything while requests are in flight", async () => {
    // The whole point of the delay. Thirty chunk requests during
    // startup would take the connections the first screen needs.
    const mod = await freshModule();
    const chunk = vi.fn().mockResolvedValue({});
    mod.registerRouteChunk(chunk);
    inFlight = 3;

    mod.startRoutePrefetch();
    await vi.advanceTimersByTimeAsync(30_000);

    expect(chunk).not.toHaveBeenCalled();
  });

  it("resumes once the pool goes quiet again", async () => {
    const mod = await freshModule();
    const chunk = vi.fn().mockResolvedValue({});
    mod.registerRouteChunk(chunk);
    inFlight = 2;

    mod.startRoutePrefetch();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(chunk).not.toHaveBeenCalled();

    inFlight = 0;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(chunk).toHaveBeenCalledTimes(1);
  });

  it("warms each chunk at most once", async () => {
    const mod = await freshModule();
    const chunk = vi.fn().mockResolvedValue({});
    mod.registerRouteChunk(chunk);

    mod.startRoutePrefetch();
    await vi.advanceTimersByTimeAsync(30_000);

    expect(chunk).toHaveBeenCalledTimes(1);
  });

  it("a failed prefetch is retryable, not sticky", async () => {
    // A chunk that failed to warm must still be importable on
    // navigation — swallowing the error and marking it done would turn
    // a transient blip into a permanently broken route.
    const mod = await freshModule();
    const chunk = vi.fn().mockRejectedValue(new Error("offline"));

    mod.prefetchRouteChunk(chunk);
    await vi.advanceTimersByTimeAsync(100);
    mod.prefetchRouteChunk(chunk);
    await vi.advanceTimersByTimeAsync(100);

    expect(chunk).toHaveBeenCalledTimes(2);
  });

  it("hover prefetch does not wait for idle", async () => {
    const mod = await freshModule();
    const chunk = vi.fn().mockResolvedValue({});
    inFlight = 5;

    mod.prefetchRouteChunk(chunk);

    expect(chunk).toHaveBeenCalledTimes(1);
  });
});
