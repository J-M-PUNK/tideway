import { useLayoutEffect, useRef, useState } from "react";
import type React from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

/**
 * Generic windowed list. Only the rows intersecting the scroll
 * viewport (plus overscan) are mounted, so a 200- or 500-row stats
 * list mounts ~20 rows instead of all of them — and, just as
 * importantly, only those rows run their per-row art-lookup hooks.
 *
 * Mirrors the virtualization `TrackList` already uses: it latches
 * onto the nearest `[data-scroll-container]` (the main element from
 * App.tsx) and self-corrects row heights via `measureElement`, so
 * `estimateSize` only needs to be approximately right.
 */
export function VirtualList({
  count,
  estimateSize,
  rowKey,
  renderRow,
}: {
  count: number;
  estimateSize: number;
  rowKey: (index: number) => string;
  renderRow: (index: number) => React.ReactNode;
}) {
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const [scrollEl, setScrollEl] = useState<HTMLElement | null>(null);

  // useLayoutEffect so the scroll-container lookup lands before
  // paint — otherwise the first pass renders nothing and flashes.
  useLayoutEffect(() => {
    const el = anchorRef.current?.closest(
      "[data-scroll-container]",
    ) as HTMLElement | null;
    setScrollEl(el);
  }, []);

  const virtualizer = useVirtualizer({
    count,
    getScrollElement: () => scrollEl,
    estimateSize: () => estimateSize,
    overscan: 10,
  });

  // Until the scroll element is latched, render only the anchor —
  // getScrollElement returning null logs a virtualizer warning. The
  // pages show their own skeleton while data is still loading.
  if (!scrollEl) {
    return <div ref={anchorRef} />;
  }

  const items = virtualizer.getVirtualItems();
  const totalHeight = virtualizer.getTotalSize();

  return (
    <div ref={anchorRef} className="relative" style={{ height: totalHeight }}>
      {items.map((vi) => (
        <div
          key={rowKey(vi.index)}
          data-index={vi.index}
          ref={virtualizer.measureElement}
          className="absolute inset-x-0"
          style={{ transform: `translateY(${vi.start}px)` }}
        >
          {renderRow(vi.index)}
        </div>
      ))}
    </div>
  );
}
