import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * AOTY's endpoints can block on a Cloudflare clearance solve. When the
 * webview profile holds no usable `cf_clearance` — a fresh install, or
 * a cookie that expired between launches — the first AOTY request waits
 * for the hidden window to work through the challenge. Measured on
 * Windows: ~5s warm, but 33s, 67s and 91s cold.
 *
 * Under the client's 15s default that request aborted, `useApi` set an
 * error, and the row returned null. `useApi` only re-fetches when its
 * deps change and the AOTY rows pass `[]`, so the rows stayed gone for
 * the whole session — and a fresh install is both the slowest path and
 * the first impression.
 *
 * These pin the longer budget onto the calls that can wait on a solve,
 * and pin that `status` is deliberately left on the default, since it
 * reads local state and never touches the network.
 */

const ok = () =>
  ({
    ok: true,
    status: 200,
    text: async () => "{}",
  }) as unknown as Response;

/** Timeouts observed via AbortSignal.timeout, in call order. */
let observed: number[] = [];

describe("AOTY request timeouts", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    observed = [];
    fetchMock = vi.fn(async () => ok());
    vi.stubGlobal("fetch", fetchMock);
    // Record what each request asks for without changing behaviour.
    const realTimeout = AbortSignal.timeout.bind(AbortSignal);
    vi.stubGlobal("AbortSignal", {
      ...AbortSignal,
      timeout: (ms: number) => {
        observed.push(ms);
        return realTimeout(ms);
      },
      any: AbortSignal.any?.bind(AbortSignal),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("gives the Home rows a budget that outlasts a cold solve", async () => {
    const { api } = await import("./client");
    await api.aoty.recentReleases({ limit: 30 });
    await api.aoty.topOfYear({ limit: 30 });

    // The cold solve is bounded by _CF_CHALLENGE_TIMEOUT_SEC = 90 in
    // desktop.py, with the page fetch and Tidal resolve after it. Any
    // budget at or below that cap would still drop the row.
    for (const ms of observed) {
      expect(ms).toBeGreaterThan(91_000);
    }
    expect(observed).toHaveLength(2);
  });

  it("covers the drill-down calls too", async () => {
    const { api } = await import("./client");
    await api.aoty.genres();
    await api.aoty.genreReleases("26-shoegaze", { limit: 30 });

    // These hit the same scraper and block on the same solve, so a
    // user who opens a drill-down before the clearance lands would
    // otherwise get the same vanishing row.
    for (const ms of observed) {
      expect(ms).toBeGreaterThan(91_000);
    }
    expect(observed).toHaveLength(2);
  });

  it("leaves the status endpoint on the default budget", async () => {
    const { api } = await import("./client");
    await api.aoty.status();

    // status() reads `is_scraper_blocked()` and `solver_available()` —
    // local state, no scraper call, so it can never wait on a solve.
    // Giving it the long budget would only delay the blocked notice
    // when the backend is genuinely wedged.
    expect(observed).toEqual([15_000]);
  });

  it("leaves unrelated endpoints on the default budget", async () => {
    const { api } = await import("./client");
    await api.version();
    expect(observed).toEqual([15_000]);
  });
});
