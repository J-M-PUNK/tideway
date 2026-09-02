import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The desktop shell is a Chromium webview talking to 127.0.0.1, and
 * Chromium allows a host six concurrent HTTP/1.1 connections. Route
 * chunks, cover images and API calls share that pool. Measured: the app
 * never exceeded 6 in flight while the same server driven from outside
 * reached 7 — the ceiling is the client.
 *
 * So the API client keeps itself below the limit, leaving connections
 * for the chunk of the page the user just clicked.
 */

function deferred() {
  let resolve!: (v: Response) => void;
  const promise = new Promise<Response>((r) => (resolve = r));
  return { promise, resolve };
}

const ok = () =>
  ({ ok: true, status: 200, text: async () => "{}" }) as unknown as Response;

describe("API request concurrency", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("issues at most four requests at once", async () => {
    const { api } = await import("./client");
    const pending = Array.from({ length: 8 }, () => deferred());
    let i = 0;
    fetchMock.mockImplementation(() => pending[i++].promise);

    const calls = Array.from({ length: 8 }, () => api.version().catch(() => {}));
    await Promise.resolve();
    await Promise.resolve();

    expect(fetchMock).toHaveBeenCalledTimes(4);

    pending.forEach((p) => p.resolve(ok()));
    await Promise.all(calls);
  });

  it("starts a queued request as soon as one finishes", async () => {
    const { api } = await import("./client");
    const pending = Array.from({ length: 5 }, () => deferred());
    let i = 0;
    fetchMock.mockImplementation(() => pending[i++].promise);

    const calls = Array.from({ length: 5 }, () => api.version().catch(() => {}));
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(4);

    pending[0].resolve(ok());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));

    pending.slice(1).forEach((p) => p.resolve(ok()));
    await Promise.all(calls);
  });

  it("releases the slot when a request fails", async () => {
    // Without the finally, one network error would permanently shrink
    // the pool and eventually deadlock every request.
    const { api } = await import("./client");
    fetchMock.mockRejectedValue(new Error("boom"));

    await Promise.all(
      Array.from({ length: 6 }, () => api.version().catch(() => {})),
    );

    fetchMock.mockResolvedValue(ok());
    await expect(api.version()).resolves.toBeDefined();
  });

  it("reports how many requests hold a slot", async () => {
    const { api, apiRequestsInFlight } = await import("./client");
    expect(apiRequestsInFlight()).toBe(0);

    const d = deferred();
    fetchMock.mockReturnValue(d.promise);
    const call = api.version().catch(() => {});
    await Promise.resolve();

    expect(apiRequestsInFlight()).toBe(1);
    d.resolve(ok());
    await call;
    expect(apiRequestsInFlight()).toBe(0);
  });
});
