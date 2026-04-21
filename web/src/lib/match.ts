import type { Track } from "@/api/types";

/**
 * Pick the best Tidal search hit for a "by name" lookup (Last.fm →
 * Tidal, stats → Tidal). Tidal's search ranks popularity first, which
 * means remix/cover versions sometimes outrank the canonical
 * recording; preferring an exact title + artist match gets the right
 * one when it's in the result set.
 */
export function findBestMatch(
  candidates: Track[] | undefined,
  query: { track: string; artist: string },
): Track | null {
  if (!candidates || candidates.length === 0) return null;
  const wantTitle = query.track.toLowerCase();
  const wantArtist = query.artist.toLowerCase();
  const exact = candidates.find(
    (t) =>
      t.name.toLowerCase() === wantTitle &&
      t.artists.some((a) => a.name.toLowerCase() === wantArtist),
  );
  return exact ?? candidates[0];
}
