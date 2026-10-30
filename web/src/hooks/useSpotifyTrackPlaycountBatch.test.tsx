import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * A page that batches its tracks' Spotify playcounts must not also fetch
 * them one row at a time. The page component calls the batch hook and
 * renders the TrackList rows in the same commit; React runs the rows'
 * effects before the page's, so when the batch ran as a passive effect
 * every row had already sent its own request and each playcount went
 * out twice. These mount that exact shape.
 */

vi.mock("@/api/client", () => ({
  api: {
    spotify: {
      trackPlaycount: vi.fn(),
      trackPlaycounts: vi.fn(),
    },
  },
}));

type Mod = typeof import("./useSpotifyEnrichment");
type Api = typeof import("@/api/client").api;

let mod: Mod;
let api: Api;
let container: HTMLDivElement;
let root: Root;

const TRACKS = [
  { isrc: "usaaa0000001", name: "One", artists: [{ name: "A" }] },
  { isrc: "USAAA0000002", name: "Two", artists: [{ name: "A" }] },
  { isrc: "USAAA0000003", name: "Three", artists: [{ name: "A" }] },
];

function Row({ isrc }: { isrc: string }) {
  const plays = mod.useSpotifyTrackPlaycount(isrc);
  return <span data-isrc={isrc.toUpperCase()}>{plays ?? "-"}</span>;
}

function Page({ tracks }: { tracks: typeof TRACKS }) {
  mod.useSpotifyTrackPlaycountBatch(tracks);
  return (
    <>
      {tracks.map((t) => (
        <Row key={t.isrc} isrc={t.isrc} />
      ))}
    </>
  );
}

function shown(): Record<string, string> {
  const out: Record<string, string> = {};
  container.querySelectorAll("span").forEach((el) => {
    out[el.getAttribute("data-isrc") ?? ""] = el.textContent ?? "";
  });
  return out;
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(async () => {
  vi.resetModules();
  mod = await import("./useSpotifyEnrichment");
  api = (await import("@/api/client")).api;
  vi.mocked(api.spotify.trackPlaycount).mockReset();
  vi.mocked(api.spotify.trackPlaycounts).mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("useSpotifyTrackPlaycountBatch", () => {
  it("sends one batch and no per-row requests", async () => {
    const batch = deferred<{ playcounts: Record<string, number | null> }>();
    vi.mocked(api.spotify.trackPlaycounts).mockReturnValue(batch.promise);

    act(() => root.render(<Page tracks={TRACKS} />));

    expect(api.spotify.trackPlaycount).not.toHaveBeenCalled();
    expect(api.spotify.trackPlaycounts).toHaveBeenCalledTimes(1);
    const sent = vi.mocked(api.spotify.trackPlaycounts).mock.lastCall?.[0];
    expect(sent?.map((x) => x.isrc)).toEqual([
      "USAAA0000001",
      "USAAA0000002",
      "USAAA0000003",
    ]);

    await act(async () => {
      batch.resolve({
        playcounts: { USAAA0000001: 10, USAAA0000002: 20, USAAA0000003: null },
      });
      await batch.promise;
    });

    expect(shown()).toEqual({
      USAAA0000001: "10",
      USAAA0000002: "20",
      USAAA0000003: "-",
    });
    expect(api.spotify.trackPlaycount).not.toHaveBeenCalled();
  });

  it("falls back to per-row requests when the batch fails", async () => {
    const batch = deferred<{ playcounts: Record<string, number | null> }>();
    vi.mocked(api.spotify.trackPlaycounts).mockReturnValue(batch.promise);
    vi.mocked(api.spotify.trackPlaycount).mockImplementation((isrc) =>
      Promise.resolve({ playcount: Number(isrc.slice(-1)) * 100 }),
    );

    act(() => root.render(<Page tracks={TRACKS} />));
    expect(api.spotify.trackPlaycount).not.toHaveBeenCalled();

    await act(async () => {
      batch.reject(new Error("502"));
      await batch.promise.catch(() => undefined);
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(api.spotify.trackPlaycount).toHaveBeenCalledTimes(3);
    expect(shown()).toEqual({
      USAAA0000001: "100",
      USAAA0000002: "200",
      USAAA0000003: "300",
    });
  });

  it("fetches on its own any ISRC the batch answer left out", async () => {
    vi.mocked(api.spotify.trackPlaycounts).mockResolvedValue({
      playcounts: { USAAA0000001: 10, USAAA0000002: 20 },
    });
    vi.mocked(api.spotify.trackPlaycount).mockResolvedValue({ playcount: 30 });

    await act(async () => {
      root.render(<Page tracks={TRACKS} />);
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(api.spotify.trackPlaycount).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.spotify.trackPlaycount).mock.lastCall?.[0]).toBe(
      "USAAA0000003",
    );
    expect(shown().USAAA0000003).toBe("30");
  });

  it("leaves cached and in-flight ISRCs out of the request", async () => {
    mod.preseedSpotifyPlaycounts({ USAAA0000001: 10 });
    const first = deferred<{ playcounts: Record<string, number | null> }>();
    vi.mocked(api.spotify.trackPlaycounts).mockReturnValueOnce(first.promise);

    // Track 2 goes in flight from one page...
    act(() => root.render(<Page tracks={TRACKS.slice(0, 2)} />));
    expect(
      vi
        .mocked(api.spotify.trackPlaycounts)
        .mock.lastCall?.[0].map((x) => x.isrc),
    ).toEqual(["USAAA0000002"]);

    // ...and a page listing all three only asks for track 3.
    vi.mocked(api.spotify.trackPlaycounts).mockResolvedValueOnce({
      playcounts: { USAAA0000003: 30 },
    });
    await act(async () => {
      root.render(<Page tracks={TRACKS} />);
    });
    expect(
      vi
        .mocked(api.spotify.trackPlaycounts)
        .mock.lastCall?.[0].map((x) => x.isrc),
    ).toEqual(["USAAA0000003"]);
    expect(api.spotify.trackPlaycount).not.toHaveBeenCalled();
  });
});
