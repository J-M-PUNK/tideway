import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

import { useApi, prefetchApi, clearApiCache, __cacheInternals } from "./useApi";

interface State<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
}

function flush() {
  return new Promise<void>((resolve) => setTimeout(resolve, 0));
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  clearApiCache();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
});

function Probe<T>({
  fetcher,
  cacheKey,
  ttlMs,
  onState,
}: {
  fetcher: () => Promise<T>;
  cacheKey?: string;
  ttlMs?: number;
  onState: (s: State<T>) => void;
}) {
  const s = useApi(fetcher, [], { cacheKey, ttlMs });
  onState(s);
  return null;
}

describe("useApi caching", () => {
  it("uncached: shows loading then data on mount", async () => {
    const fetcher = vi.fn().mockResolvedValue("hello");
    const states: State<string>[] = [];
    await act(async () => {
      root.render(
        <Probe<string> fetcher={fetcher} onState={(s) => states.push(s)} />,
      );
    });
    await act(async () => {
      await flush();
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
    const last = states[states.length - 1];
    expect(last).toEqual({ data: "hello", loading: false, error: null });
  });

  it("cached: a second mount with the same cacheKey renders fresh data synchronously", async () => {
    const fetcher = vi.fn().mockResolvedValue({ rows: 3 });
    const firstStates: State<{ rows: number }>[] = [];
    await act(async () => {
      root.render(
        <Probe<{ rows: number }>
          fetcher={fetcher}
          cacheKey="page:home"
          onState={(s) => firstStates.push(s)}
        />,
      );
    });
    await act(async () => {
      await flush();
    });
    // First mount: loading then data, fetcher called once.
    expect(fetcher).toHaveBeenCalledTimes(1);

    // Unmount and remount.
    await act(async () => {
      root.unmount();
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
    });

    const secondStates: State<{ rows: number }>[] = [];
    await act(async () => {
      root.render(
        <Probe<{ rows: number }>
          fetcher={fetcher}
          cacheKey="page:home"
          onState={(s) => secondStates.push(s)}
        />,
      );
    });
    // The first state from the second mount must already be the cached
    // value with loading=false — that's the SWR contract.
    expect(secondStates[0]).toEqual({
      data: { rows: 3 },
      loading: false,
      error: null,
    });
  });

  it("dedupes concurrent fetches by cacheKey", async () => {
    const fetcher = vi
      .fn()
      .mockImplementation(
        () => new Promise((resolve) => setTimeout(() => resolve("once"), 10)),
      );

    const c2 = document.createElement("div");
    document.body.appendChild(c2);
    const root2 = createRoot(c2);

    await act(async () => {
      root.render(
        <Probe<string>
          fetcher={fetcher}
          cacheKey="dup:key"
          onState={() => {}}
        />,
      );
      root2.render(
        <Probe<string>
          fetcher={fetcher}
          cacheKey="dup:key"
          onState={() => {}}
        />,
      );
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(fetcher).toHaveBeenCalledTimes(1);

    act(() => root2.unmount());
    document.body.removeChild(c2);
  });

  it("expired TTL forces a fresh fetch and shows loading", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce("v1")
      .mockResolvedValueOnce("v2");

    const states: State<string>[] = [];
    await act(async () => {
      root.render(
        <Probe<string>
          fetcher={fetcher}
          cacheKey="ttl:key"
          ttlMs={1}
          onState={(s) => states.push(s)}
        />,
      );
    });
    await act(async () => {
      await flush();
    });
    expect(states[states.length - 1].data).toBe("v1");

    // Wait past the 1ms TTL.
    await new Promise((r) => setTimeout(r, 5));

    await act(async () => {
      root.unmount();
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
    });

    const remountStates: State<string>[] = [];
    await act(async () => {
      root.render(
        <Probe<string>
          fetcher={fetcher}
          cacheKey="ttl:key"
          ttlMs={1}
          onState={(s) => remountStates.push(s)}
        />,
      );
    });
    // Stale entry: must show loading first, not the expired value.
    expect(remountStates[0]).toEqual({
      data: null,
      loading: true,
      error: null,
    });
    await act(async () => {
      await flush();
    });
    expect(remountStates[remountStates.length - 1].data).toBe("v2");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("revalidate failure keeps stale data and surfaces the error", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce("good")
      .mockRejectedValueOnce(new Error("boom"));

    // Seed cache.
    await act(async () => {
      root.render(
        <Probe<string>
          fetcher={fetcher}
          cacheKey="err:key"
          onState={() => {}}
        />,
      );
    });
    await act(async () => {
      await flush();
    });

    // Force the cache to be considered stale so the next mount
    // revalidates immediately.
    __cacheInternals.cache.set("err:key", { data: "good", timestamp: 0 });

    await act(async () => {
      root.unmount();
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
    });

    const states: State<string>[] = [];
    await act(async () => {
      root.render(
        <Probe<string>
          fetcher={fetcher}
          cacheKey="err:key"
          ttlMs={1000}
          onState={(s) => states.push(s)}
        />,
      );
    });
    await act(async () => {
      await flush();
    });

    const last = states[states.length - 1];
    expect(last.data).toBe("good"); // stale-on-error
    expect(last.error).toBeInstanceOf(Error);
    expect(last.error?.message).toBe("boom");
  });
});

describe("skip", () => {
  it("skip=true does not invoke the fetcher and renders idle state", async () => {
    const fetcher = vi.fn().mockResolvedValue("never");

    function Probe2() {
      const s = useApi(fetcher, [], { skip: true });
      return (
        <span data-testid="state">
          {String(s.loading)}-{String(s.data)}
        </span>
      );
    }

    await act(async () => {
      root.render(<Probe2 />);
    });
    await act(async () => {
      await flush();
    });

    expect(fetcher).not.toHaveBeenCalled();
    const span = container.querySelector("[data-testid=state]");
    expect(span?.textContent).toBe("false-null");
  });

  it("flipping skip from true to false fires a fresh fetch", async () => {
    const fetcher = vi.fn().mockResolvedValue("found");
    const seen: State<string>[] = [];
    const handle: { setSkip?: (b: boolean) => void } = {};

    function Probe3() {
      const reactMod: typeof import("react") = require("react");
      const [skip, setSkip] = reactMod.useState<boolean>(true);
      handle.setSkip = setSkip;
      const s = useApi<string>(fetcher, ["q"], { skip });
      seen.push(s);
      return null;
    }

    await act(async () => {
      root.render(<Probe3 />);
    });
    await act(async () => {
      await flush();
    });
    expect(fetcher).not.toHaveBeenCalled();

    await act(async () => {
      handle.setSkip?.(false);
    });
    await act(async () => {
      await flush();
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(seen[seen.length - 1].data).toBe("found");
  });
});

describe("prefetchApi", () => {
  it("populates the cache without rendering", async () => {
    const fetcher = vi.fn().mockResolvedValue("warmed");
    prefetchApi("warm:key", fetcher);
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(1);

    // A subsequent useApi mount with the same key sees the cached value
    // synchronously.
    const states: State<string>[] = [];
    await act(async () => {
      root.render(
        <Probe<string>
          fetcher={fetcher}
          cacheKey="warm:key"
          onState={(s) => states.push(s)}
        />,
      );
    });
    expect(states[0]).toEqual({
      data: "warmed",
      loading: false,
      error: null,
    });
  });

  it("is a no-op when the cache is already fresh", async () => {
    const fetcher = vi.fn().mockResolvedValue("once");
    prefetchApi("dedup:key", fetcher);
    await flush();
    prefetchApi("dedup:key", fetcher);
    prefetchApi("dedup:key", fetcher);
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
