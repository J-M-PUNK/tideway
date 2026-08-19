import type { RecAlbum, RecSection } from "@/components/RecShelf";

// Mirrors the server's `_blend_top_row` (#307). The "Recommended For You"
// top row draws the strongest remaining pick from every row in turn; the
// picks are then removed from their source rows so nothing shows twice,
// and a row left too thin is dropped (its albums live on in the blend one
// shelf up). We recompute it on the client so the page can stream each
// row in independently instead of waiting for one build-everything call.
const BLEND_SIZE = 18;
// The server declines to blend fewer than four picks — one or two cards
// read as a broken row rather than a curated mix.
const BLEND_MIN_PICKS = 4;
const BLEND_MIN_REMAINDER = 3;

export type BlendResult = { blend: RecSection | null; rows: RecSection[] };

/**
 * Build the "Recommended For You" round-robin from the loaded rows and
 * trim the picks out of their source rows. With fewer than two non-empty
 * rows, or fewer than four picks, there's nothing worth blending and the
 * rows come back untouched.
 */
export function blendTopRow(sections: RecSection[]): BlendResult {
  const pools = sections
    .filter((s) => s.albums.length > 0)
    .map((s) => ({ section: s, pool: [...s.albums] }));
  if (pools.length < 2) return { blend: null, rows: sections };

  const picked: RecAlbum[] = [];
  const taken = new Set<string>();
  while (picked.length < BLEND_SIZE) {
    let progressed = false;
    for (const { section, pool } of pools) {
      while (pool.length) {
        const album = pool.shift() as RecAlbum;
        const aid = String(album.id ?? "");
        if (!aid || taken.has(aid)) continue;
        picked.push({ ...album, reason: album.reason ?? section.title });
        taken.add(aid);
        progressed = true;
        break;
      }
      if (picked.length >= BLEND_SIZE) break;
    }
    if (!progressed) break;
  }
  if (picked.length < BLEND_MIN_PICKS) return { blend: null, rows: sections };

  const blend: RecSection = {
    key: "recommended",
    title: "Recommended For You",
    subtitle: "The best of every source, side by side",
    albums: picked,
  };
  const rows = sections
    .map((s) => ({
      ...s,
      albums: s.albums.filter((a) => !taken.has(String(a.id ?? ""))),
    }))
    .filter((s) => s.albums.length >= BLEND_MIN_REMAINDER);
  return { blend, rows };
}
