import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { RefObject } from "react";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

// Drive navigation type + history key from the test.
let navType = "PUSH";
let locKey = "a";
vi.mock("react-router-dom", () => ({
  useNavigationType: () => navType,
  useLocation: () => ({ key: locKey, pathname: "/" }),
}));

import { useScrollRestoration } from "./useScrollRestoration";

// Every live observer, so a test can simulate the container's content
// growing after mount the way an async page does.
let observers: Array<() => void> = [];

class FakeResizeObserver {
  constructor(private cb: () => void) {
    observers.push(() => this.cb());
  }
  observe() {}
  disconnect() {
    observers = observers.filter((f) => f !== this.cb);
  }
}

class FakeMutationObserver {
  observe() {}
  disconnect() {}
}

/**
 * A stand-in scroll container. The hook only touches these members, so we
 * skip jsdom's layout-less DOM entirely and control everything.
 *
 * It clamps `scrollTop` to what the content can actually scroll, because
 * that clamp is the behaviour the hook has to survive.
 */
function makeEl() {
  let top = 0;
  const listeners: Record<string, Array<() => void>> = {};
  const el = {
    scrollHeight: 1000,
    clientHeight: 500,
    children: [] as Element[],
    fire(type: string) {
      (listeners[type] || []).forEach((f) => f());
    },
    get maxTop() {
      return Math.max(0, el.scrollHeight - el.clientHeight);
    },
    get scrollTop() {
      return top;
    },
    // Both user and programmatic scrolls emit a scroll event.
    set scrollTop(v: number) {
      top = Math.max(0, Math.min(v, el.maxTop));
      el.fire("scroll");
    },
    scrollTo(opts: { top?: number }) {
      el.scrollTop = opts?.top ?? 0;
    },
    /**
     * Content is replaced by something shorter, as when React swaps in
     * the next route during its render phase. The browser clamps
     * scrollTop right away but dispatches the scroll event later, after
     * the commit, so no event fires here.
     */
    shrinkTo(scrollHeight: number) {
      el.scrollHeight = scrollHeight;
      top = Math.min(top, el.maxTop);
    },
    addEventListener(t: string, f: () => void) {
      (listeners[t] = listeners[t] || []).push(f);
    },
    removeEventListener(t: string, f: () => void) {
      listeners[t] = (listeners[t] || []).filter((x) => x !== f);
    },
  };
  return el;
}

function growContent(el: ReturnType<typeof makeEl>, scrollHeight: number) {
  el.scrollHeight = scrollHeight;
  act(() => {
    observers.forEach((fire) => fire());
  });
}

let container: HTMLDivElement;
let root: Root;

function Harness({ elRef }: { elRef: RefObject<HTMLElement | null> }) {
  useScrollRestoration(elRef);
  return null;
}

beforeEach(() => {
  navType = "PUSH";
  locKey = "a";
  observers = [];
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  vi.stubGlobal("MutationObserver", FakeMutationObserver);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
  vi.unstubAllGlobals();
});

it("resets to top on forward nav and restores on back nav", () => {
  const el = makeEl();
  const ref = { current: el as unknown as HTMLElement };
  const render = () => act(() => root.render(<Harness elRef={ref} />));

  // 1. Land on entry "a" via PUSH -> starts at top.
  render();
  expect(el.scrollTop).toBe(0);

  // 2. Scroll down (recorded for "a" when we navigate away).
  act(() => {
    el.scrollTop = 300;
  });

  // 3. Navigate forward to "b" -> resets to top.
  navType = "PUSH";
  locKey = "b";
  render();
  expect(el.scrollTop).toBe(0);

  // 4. Back to "a" (POP) -> restores 300.
  navType = "POP";
  locKey = "a";
  render();
  expect(el.scrollTop).toBe(300);
});

it("restores to top when the entry has no saved position", () => {
  const el = makeEl();
  const ref = { current: el as unknown as HTMLElement };
  navType = "POP";
  locKey = "never-scrolled";
  act(() => root.render(<Harness elRef={ref} />));
  expect(el.scrollTop).toBe(0);
});

it("records where the user was, not where the route swap clamped them", () => {
  // The reopened-issue case. React renders the next route before any
  // layout effect runs, so the container is already short and the
  // browser has clamped scrollTop by the time we record it. Reading
  // scrollTop on the way out recorded ~140 for a list left at 8000.
  const el = makeEl();
  el.scrollHeight = 12345;
  const ref = { current: el as unknown as HTMLElement };
  const render = () => act(() => root.render(<Harness elRef={ref} />));

  render();
  act(() => {
    el.scrollTop = 8000;
  });
  expect(el.scrollTop).toBe(8000);

  // The outgoing list is replaced by the next route's skeleton, which
  // leaves only 320px of scrollable content to clamp into.
  act(() => el.shrinkTo(820));
  expect(el.scrollTop).toBe(320);

  navType = "PUSH";
  locKey = "b";
  render();

  // Back to "a": the list has to load again, so it starts short.
  navType = "POP";
  locKey = "a";
  el.scrollHeight = 500;
  render();
  growContent(el, 12345);

  expect(el.scrollTop).toBe(8000);
});

it("reaches a deep offset when the page's content arrives late", () => {
  const el = makeEl();
  el.scrollHeight = 4000;
  const ref = { current: el as unknown as HTMLElement };
  const render = () => act(() => root.render(<Harness elRef={ref} />));

  render();
  act(() => {
    el.scrollTop = 2000;
  });

  navType = "PUSH";
  locKey = "b";
  render();

  // Back to "a", but the list is empty so far: nothing to scroll.
  navType = "POP";
  locKey = "a";
  el.scrollHeight = 500;
  render();
  expect(el.scrollTop).toBe(0);

  // Data lands in stages, well beyond any fixed grace window.
  growContent(el, 1200);
  expect(el.scrollTop).toBe(700);

  growContent(el, 3000);
  expect(el.scrollTop).toBe(2000);
});

it("stops restoring once the user scrolls for themselves", () => {
  const el = makeEl();
  el.scrollHeight = 4000;
  const ref = { current: el as unknown as HTMLElement };
  const render = () => act(() => root.render(<Harness elRef={ref} />));

  render();
  act(() => {
    el.scrollTop = 2000;
  });

  navType = "PUSH";
  locKey = "b";
  render();

  navType = "POP";
  locKey = "a";
  el.scrollHeight = 500;
  render();

  // The user gives up waiting and scrolls somewhere themselves.
  act(() => el.fire("wheel"));
  growContent(el, 3000);
  act(() => {
    el.scrollTop = 120;
  });

  // Late content must not yank them back to the old offset.
  growContent(el, 6000);
  expect(el.scrollTop).toBe(120);
});
