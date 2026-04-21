import { useEffect, useState } from "react";

/**
 * How many cards fit in one row of a single-row section at the current
 * viewport width. Matches the Tailwind breakpoints used in
 * `PageView` / `Home` so we can cap item lists to exactly what fits
 * — avoids the ugly "one full row + one trailing card" wrap.
 *
 * Breakpoints:
 *   base  < 640   → 2
 *   sm   >= 640   → 3
 *   md   >= 768   → 4
 *   lg   >= 1024  → 5
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
  if (width >= 1024) return 5;
  if (width >= 768) return 4;
  if (width >= 640) return 3;
  return 2;
}
