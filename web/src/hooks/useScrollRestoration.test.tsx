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

// A stand-in scroll container — the hook only touches these members, so
// we skip jsdom's layout-less DOM entirely and control everything.
function makeEl() {
  let top = 0;
  const listeners: Record<string, Array<() => void>> = {};
  return {
    fire(type: string) {
      (listeners[type] || []).forEach((f) => f());
    },
    get scrollTop() {
      return top;
    },
    set scrollTop(v: number) {
      top = v;
    },
    scrollHeight: 1000,
    clientHeight: 500,
    scrollTo(opts: { top?: number }) {
      top = opts?.top ?? 0;
    },
    addEventListener(t: string, f: () => void) {
      (listeners[t] = listeners[t] || []).push(f);
    },
    removeEventListener(t: string, f: () => void) {
      listeners[t] = (listeners[t] || []).filter((x) => x !== f);
    },
  };
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
  // Run rAF synchronously so the throttled save + restore apply run inline.
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.stubGlobal("performance", { now: () => 0 });
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

  // 2. Scroll down (captured for "a" when we navigate away).
  el.scrollTop = 300;

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
  el.scrollTop = 400;
  const ref = { current: el as unknown as HTMLElement };
  navType = "POP";
  locKey = "never-scrolled";
  act(() => root.render(<Harness elRef={ref} />));
  expect(el.scrollTop).toBe(0);
});
