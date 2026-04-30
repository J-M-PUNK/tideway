import { useEffect, useState } from "react";

/**
 * How many cards fit in one row of a single-row section at the current
 * viewport width. **Must stay in lockstep with the column breakpoints
 * in `Grid`** ([components/Grid.tsx]) — the slice this returns caps
 * what we render, and `Grid`'s CSS decides how many fit per row. If
 * the two diverge, a sliced trailing card wraps to a phantom second
 * row, which is exactly the "ugly one full row + one trailing card"
 * this hook exists to prevent.
 *
 * Grid's classes: `grid-cols-2 sm:grid-cols-3 lg:grid-cols-4
 *                  xl:grid-cols-5 2xl:grid-cols-6`. That means:
 *   base  < 640   → 2
 *   sm   >= 640   → 3
 *   lg   >= 1024  → 4
 *   xl   >= 1280  → 5
 *   2xl  >= 1536  → 6
 */
export function useColumnCount(): number {
  const [cols, setCols] = useState(() => compute(safeWindowWidth()));
  useEffect(() => {
    const onResize = () => setCols(compute(window.innerWidth));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return cols;
}

function safeWindowWidth(): number {
  // SSR safety — this app runs exclusively client-side in pywebview,
  // but guarding costs us nothing and lets the hook be used under
  // test harnesses without a global `window`.
  if (typeof window === "undefined") return 1280;
  return window.innerWidth;
}

function compute(width: number): number {
  if (width >= 1536) return 6;
  if (width >= 1280) return 5;
  if (width >= 1024) return 4;
  if (width >= 640) return 3;
  return 2;
}
