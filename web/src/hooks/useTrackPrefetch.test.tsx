import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * Prefetch de-dupe has to forget a warm before the backend does. The
 * backend drops a warmed manifest after 3 minutes; when this remembered
 * warms for the whole session, a track warmed earlier never warmed
 * again and its click paid the full resolve.
 */

vi.mock("@/api/client", () => ({
  api: { player: { prefetch: vi.fn() } },
}));

vi.mock("@/hooks/useUiPreferences", () => ({
  useUiPreferences: () => ({ streamingQuality: "hi_res_lossless" }),
}));

const { api } = await import("@/api/client");
const { useTrackPrefetch } = await import("./useTrackPrefetch");

const prefetchMock = api.player.prefetch as ReturnType<typeof vi.fn>;

let container: HTMLDivElement;
let root: Root;
let hook: ReturnType<typeof useTrackPrefetch>;

function Probe({
  onReady,
}: {
  onReady: (h: ReturnType<typeof useTrackPrefetch>) => void;
}) {
  const h = useTrackPrefetch();
  useEffect(() => onReady(h), [h, onReady]);
  return null;
}

beforeEach(() => {
  vi.useFakeTimers();
  prefetchMock.mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() =>
    root.render(
      <Probe
        onReady={(h) => {
          hook = h;
        }}
      />,
    ),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

// The de-dupe map is module state, so each test uses its own ids.
describe("useTrackPrefetch", () => {
  it("sends a repeat hover only once inside the window", () => {
    hook.prefetchOne("a1");
    vi.advanceTimersByTime(30_000);
    hook.prefetchOne("a1");
    expect(prefetchMock).toHaveBeenCalledTimes(1);
    expect(prefetchMock).toHaveBeenCalledWith(["a1"], "hi_res_lossless");
  });

  it("warms again once the backend's copy may have expired", () => {
    hook.prefetchOne("b1");
    // Well before the backend's 3-minute manifest TTL runs out.
    vi.advanceTimersByTime(61_000);
    hook.prefetchOne("b1");
    expect(prefetchMock).toHaveBeenCalledTimes(2);
  });

  it("drops recently warmed ids from a batch and re-sends stale ones", () => {
    hook.prefetchMany(["c1", "c2"]);
    vi.advanceTimersByTime(61_000);
    hook.prefetchOne("c3");
    hook.prefetchMany(["c1", "c2", "c3", "c4"]);
    expect(prefetchMock.mock.calls).toEqual([
      [["c1", "c2"], "hi_res_lossless"],
      [["c3"], "hi_res_lossless"],
      [["c1", "c2", "c4"], "hi_res_lossless"],
    ]);
  });

  it("skips empty ids and an all-duplicate batch", () => {
    hook.prefetchOne("");
    hook.prefetchMany(["d1"]);
    hook.prefetchMany(["d1", ""]);
    expect(prefetchMock.mock.calls).toEqual([[["d1"], "hi_res_lossless"]]);
  });
});
