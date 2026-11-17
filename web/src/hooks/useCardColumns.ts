import { useCallback, useEffect, useRef, useState } from "react";

/** Narrowest a card may get. Rows fit as many cards of at least this
 *  width as they can, so a wider window adds cards instead of growing
 *  them: a card is never wider than about this plus one card's worth
 *  shared out across the row. */
export const CARD_MIN_WIDTH_PX = 180;
/** Space between cards. Matches Tailwind's `gap-4`. */
export const CARD_GAP_PX = 16;

/** How many cards of at least `minWidth` fit across `width` pixels,
 *  never fewer than two. Zero when nothing has been measured yet. */
export function cardColumnsFor(
  width: number,
  minWidth: number = CARD_MIN_WIDTH_PX,
): number {
  if (width <= 0) return 0;
  return Math.max(
    2,
    Math.floor((width + CARD_GAP_PX) / (minWidth + CARD_GAP_PX)),
  );
}

/** `grid-template-columns` for a card grid. With `columns` it is that
 *  many equal columns (a single-row section that has sliced its items
 *  to fit); without, the browser fits as many `minWidth` columns as it
 *  can, keeping at least two. */
export function cardGridTemplate(
  columns?: number,
  minWidth: number = CARD_MIN_WIDTH_PX,
): string {
  if (columns) return `repeat(${columns}, minmax(0, 1fr))`;
  return `repeat(auto-fill, minmax(min(${minWidth}px, calc(50% - ${CARD_GAP_PX / 2}px)), 1fr))`;
}

/**
 * How many cards fit across the element the returned ref is attached
 * to, kept current as it resizes.
 *
 * Measures the row itself rather than the window. The window says
 * nothing about the sidebar, the queue panel or page padding, and the
 * old window-width table also had to be kept in step with a separate
 * set of CSS breakpoints. When the two disagreed (Tailwind sorted the
 * 1920px and 2400px rules before `2xl`, so wide windows stayed at six
 * CSS columns while the table said seven or eight), the extra cards
 * wrapped onto a second row. A single-row section now renders exactly
 * this many cards in exactly this many columns.
 */
export function useCardColumns<T extends HTMLElement = HTMLDivElement>(
  minWidth: number = CARD_MIN_WIDTH_PX,
): [(node: T | null) => void, number] {
  const [width, setWidth] = useState(0);
  const observer = useRef<ResizeObserver | null>(null);

  const ref = useCallback((node: T | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!node) return;
    setWidth(node.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const entry = entries[entries.length - 1];
      if (entry) setWidth(entry.contentRect.width);
    });
    ro.observe(node);
    observer.current = ro;
  }, []);

  useEffect(() => () => observer.current?.disconnect(), []);

  return [ref, cardColumnsFor(width, minWidth)];
}
