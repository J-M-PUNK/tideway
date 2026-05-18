import type { QualityOption } from "@/api/types";

/**
 * Does this track/album benefit from the Max tier? Returns a short
 * tag to surface on the Max entry in a download-quality menu:
 *   "Hi-Res"       track actually ships at 24-bit → Max gives you it
 *   "Same as High" CD-res only → picking Max wastes bandwidth
 * null for all other quality tiers.
 *
 * Immersive audio (Dolby Atmos / Sony 360 / MQA) isn't surfaced:
 * Tidal only streams those to authorized-partner client_ids, and our
 * PKCE session always gets a stereo FLAC downmix. Exposing the
 * distinction would mislead users.
 */
export function effectiveFormatLabel(
  quality: string,
  tags: string[] | undefined,
): string | null {
  if (quality !== "hi_res_lossless") return null;
  if (!tags || tags.length === 0) return null;
  const T = new Set(tags.map((x) => x.toUpperCase()));
  if (T.has("HIRES_LOSSLESS")) return "Hi-Res";
  if (T.has("LOSSLESS")) return "Same as High";
  return null;
}

/**
 * Filter the quality catalog down to tiers that are actually
 * deliverable for the given track or album. Used by the download-
 * menu surfaces so a Hi-Fi-only release doesn't list Max, which
 * would just download the same FLAC file Tidal serves at High.
 *
 * The only tag pattern that is real evidence a tier is missing is
 * "LOSSLESS present, HIRES_LOSSLESS absent": Tidal is telling us the
 * best master is CD-res, so Max is pointless there. Every other tag
 * set is NOT evidence the stereo lossless stream is gone. In
 * particular an immersive-only tag like DOLBY_ATMOS or SONY_360RA is
 * the album's spatial master, not a statement about the stereo
 * downmix. Tidal still serves a CD or hi-res FLAC stereo for those,
 * and our PKCE session always receives that downmix, so hiding the
 * lossless tiers there is wrong. That was the Thriller bug: its
 * canonical record is the Atmos master, tagged DOLBY_ATMOS only, and
 * the old filter capped it at Medium.
 *
 * Truly lossy-only releases carry no media_tags at all, so the
 * empty-tags fail-open branch already covers them. We never hide
 * High on tag evidence: anything Tidal streams at all is available
 * as at least a CD-res FLAC.
 */
export function filterAvailableQualities(
  qualities: QualityOption[],
  tags: string[] | undefined,
): QualityOption[] {
  if (!tags || tags.length === 0) return qualities;
  const T = new Set(tags.map((x) => x.toUpperCase()));
  const cdOnly = T.has("LOSSLESS") && !T.has("HIRES_LOSSLESS");
  return qualities.filter((q) =>
    q.value === "hi_res_lossless" ? !cdOnly : true,
  );
}
