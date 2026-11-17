import type { PageItem } from "@/api/types";

/** Stable identity for a page item: its catalog id, a page link's
 *  path, or `fallback` for the rare item that has neither. */
export function pageItemId(item: PageItem, fallback: number): string {
  if ("id" in item) return String(item.id);
  if (item.kind === "pagelink") return item.path;
  return String(fallback);
}

/**
 * Up to `max` items for a row: the preview's own items in order, then
 * topped up from `full`, skipping anything the preview already has.
 *
 * Tidal's home feed sends only a short preview for some rows (five
 * tracks for "Recommended new tracks", a handful of albums for
 * "Suggested new albums") and points at the full list through
 * viewAllPath. Its own app shows the full list, and a wide window fits
 * more cards than the preview holds.
 */
export function fillRowItems(
  preview: PageItem[],
  full: PageItem[],
  max: number,
): PageItem[] {
  const out = preview.slice(0, max);
  const seen = new Set(out.map((it, i) => `${it.kind}-${pageItemId(it, i)}`));
  for (const it of full) {
    if (out.length >= max) break;
    const key = `${it.kind}-${pageItemId(it, -1)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(it);
  }
  return out;
}
