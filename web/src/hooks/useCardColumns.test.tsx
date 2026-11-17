import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

import {
  CARD_GAP_PX,
  CARD_MIN_WIDTH_PX,
  cardColumnsFor,
  cardGridTemplate,
  useCardColumns,
} from "./useCardColumns";

describe("cardColumnsFor", () => {
  it("fits as many minimum-width cards as the width allows", () => {
    const step = CARD_MIN_WIDTH_PX + CARD_GAP_PX;
    // n cards need n widths plus n-1 gaps.
    for (const n of [2, 5, 8, 12, 20]) {
      expect(cardColumnsFor(n * step - CARD_GAP_PX)).toBe(n);
      expect(cardColumnsFor(n * step - CARD_GAP_PX - 1)).toBe(
        Math.max(2, n - 1),
      );
    }
  });

  it("keeps adding cards past the old eight-column ceiling", () => {
    // A 4K window's content area. The old table stopped at eight.
    expect(cardColumnsFor(3500)).toBeGreaterThan(8);
  });

  it("never returns fewer than two once measured", () => {
    expect(cardColumnsFor(100)).toBe(2);
    expect(cardColumnsFor(0)).toBe(0);
  });

  it("takes a wider minimum for wide cards", () => {
    expect(cardColumnsFor(1000, 240)).toBeLessThan(cardColumnsFor(1000));
  });
});

describe("cardGridTemplate", () => {
  it("lays out exactly the given number of columns", () => {
    expect(cardGridTemplate(7)).toBe("repeat(7, minmax(0, 1fr))");
  });

  it("auto-fills from the same minimum width otherwise", () => {
    expect(cardGridTemplate()).toContain(`${CARD_MIN_WIDTH_PX}px`);
    expect(cardGridTemplate()).toContain("auto-fill");
  });
});

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  constructor(public cb: ResizeObserverCallback) {
    FakeResizeObserver.instances.push(this);
  }
  observe() {}
  disconnect() {}
  resize(width: number) {
    this.cb(
      [{ contentRect: { width } } as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
}

let container: HTMLDivElement;
let root: Root;
let seen: number[];

function Probe() {
  const [ref, columns] = useCardColumns();
  seen.push(columns);
  return <div ref={ref} />;
}

beforeEach(() => {
  FakeResizeObserver.instances = [];
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  seen = [];
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useCardColumns", () => {
  it("follows the element's own width as it resizes", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      width: 1000,
    } as DOMRect);
    await act(async () => {
      root.render(<Probe />);
    });
    expect(seen[seen.length - 1]).toBe(cardColumnsFor(1000));

    const ro = FakeResizeObserver.instances[0];
    await act(async () => ro.resize(2600));
    expect(seen[seen.length - 1]).toBe(cardColumnsFor(2600));
    expect(seen[seen.length - 1]).toBeGreaterThan(cardColumnsFor(1000));

    await act(async () => ro.resize(600));
    expect(seen[seen.length - 1]).toBe(cardColumnsFor(600));
  });
});
