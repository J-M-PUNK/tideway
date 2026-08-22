import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import {
  MAX_HISTORY,
  addToHistory,
  useSearchHistory,
} from "./useSearchHistory";

// React nags when act() is used without this flag on the global.
(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

/**
 * The list transform carries all the behavior worth pinning: what
 * counts as a duplicate, which entries the typing stems are allowed to
 * eat, and where the cap bites. It's pure, so it tests without a DOM.
 */
describe("addToHistory", () => {
  it("puts the newest query first", () => {
    expect(addToHistory(["b"], "a")).toEqual(["a", "b"]);
  });

  it("trims whitespace off the stored entry", () => {
    expect(addToHistory([], "  daft punk  ")).toEqual(["daft punk"]);
  });

  it("ignores an empty or whitespace-only query", () => {
    expect(addToHistory(["a"], "")).toEqual(["a"]);
    expect(addToHistory(["a"], "   ")).toEqual(["a"]);
  });

  it("moves a repeated query back to the top instead of duplicating", () => {
    expect(addToHistory(["a", "b", "c"], "c")).toEqual(["c", "a", "b"]);
  });

  it("treats a differently-cased repeat as the same query", () => {
    expect(addToHistory(["Daft Punk"], "daft punk")).toEqual(["daft punk"]);
  });

  it("swallows the stems a query was typed through", () => {
    // What the debounce actually produces when someone pauses twice on
    // the way to a full query.
    let h: string[] = [];
    h = addToHistory(h, "daft");
    h = addToHistory(h, "daft pu");
    h = addToHistory(h, "daft punk");
    expect(h).toEqual(["daft punk"]);
  });

  it("leaves an older unrelated prefix alone", () => {
    // "radiohead" was a real search from a while back, not a stem of
    // today's query, so it survives — stems are only ever consecutive.
    const h = addToHistory(["nirvana", "radiohead"], "radiohead in rainbows");
    expect(h).toEqual(["radiohead in rainbows", "nirvana", "radiohead"]);
  });

  it("does not treat an unrelated query as a stem", () => {
    expect(addToHistory(["daft punk"], "justice")).toEqual([
      "justice",
      "daft punk",
    ]);
  });

  it("caps the list", () => {
    const full = Array.from({ length: MAX_HISTORY }, (_, i) => `q${i}`);
    const next = addToHistory(full, "newest");
    expect(next).toHaveLength(MAX_HISTORY);
    expect(next[0]).toBe("newest");
    expect(next).not.toContain(`q${MAX_HISTORY - 1}`);
  });
});

/**
 * The hook's own job is persistence: read what the last session left
 * behind, write every change straight back.
 */
describe("useSearchHistory", () => {
  const KEY = "tideway:search-history";

  let container: HTMLDivElement;
  let root: Root;
  let latest: ReturnType<typeof useSearchHistory>;

  function Probe() {
    latest = useSearchHistory();
    return null;
  }

  function mount() {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<Probe />));
  }

  beforeEach(() => localStorage.clear());

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    localStorage.clear();
  });

  it("loads what the previous session stored", () => {
    localStorage.setItem(KEY, JSON.stringify(["daft punk", "justice"]));
    mount();
    expect(latest.history).toEqual(["daft punk", "justice"]);
  });

  it("persists a recorded query", () => {
    mount();
    act(() => latest.record("boards of canada"));
    expect(latest.history).toEqual(["boards of canada"]);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual([
      "boards of canada",
    ]);
  });

  it("persists a removal", () => {
    localStorage.setItem(KEY, JSON.stringify(["a", "b"]));
    mount();
    act(() => latest.remove("a"));
    expect(latest.history).toEqual(["b"]);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(["b"]);
  });

  it("persists a clear", () => {
    localStorage.setItem(KEY, JSON.stringify(["a", "b"]));
    mount();
    act(() => latest.clear());
    expect(latest.history).toEqual([]);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual([]);
  });

  it("survives storage disappearing mid-session without throwing", () => {
    // installStorageShim only substitutes a working Storage at boot.
    // Its own header documents the WebKitGTK web process crashing and
    // reloading into a context with no storage binding — after that
    // getItem throws, and read() runs during render, so anything that
    // escapes takes the ErrorBoundary and blanks the whole app.
    const real = Object.getOwnPropertyDescriptor(
      window,
      "localStorage",
    ) as PropertyDescriptor;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException(
          "localStorage is not available",
          "SecurityError",
        );
      },
    });
    try {
      expect(() => mount()).not.toThrow();
      expect(latest.history).toEqual([]);
    } finally {
      Object.defineProperty(window, "localStorage", real);
    }
  });

  it("never writes on mount", () => {
    // The persist effect skips its first pass on purpose. `history`
    // is seeded from read(), and read() falls back to [] whenever
    // storage is unreadable — so a mount that persisted would turn
    // one transient read failure into permanent data loss.
    localStorage.setItem(KEY, JSON.stringify(["daft punk", "justice"]));
    const real = window.localStorage;
    const descriptor = Object.getOwnPropertyDescriptor(
      window,
      "localStorage",
    ) as PropertyDescriptor;
    let writes = 0;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get: () => ({
        getItem: (k: string) => real.getItem(k),
        setItem: (k: string, v: string) => {
          writes++;
          real.setItem(k, v);
        },
        removeItem: (k: string) => real.removeItem(k),
      }),
    });
    try {
      mount();
    } finally {
      Object.defineProperty(window, "localStorage", descriptor);
    }

    expect(latest.history).toEqual(["daft punk", "justice"]);
    expect(writes).toBe(0);
  });

  it("starts empty when the stored value is corrupt", () => {
    localStorage.setItem(KEY, "{not json");
    mount();
    expect(latest.history).toEqual([]);
  });

  it("starts empty when the stored value is the wrong shape", () => {
    localStorage.setItem(KEY, JSON.stringify({ queries: ["a"] }));
    mount();
    expect(latest.history).toEqual([]);
  });

  it("drops non-string entries from a hand-edited file", () => {
    localStorage.setItem(KEY, JSON.stringify(["a", 7, null, "  ", "b"]));
    mount();
    expect(latest.history).toEqual(["a", "b"]);
  });
});
