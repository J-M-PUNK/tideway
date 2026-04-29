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
 * menu surfaces so a Hi-Fi-only release doesn't list Max (which
 * would just download the same FLAC file Tidal serves at High),
 * and a lossy-only release doesn't list either lossless tier.
 *
 * The filter is conservative: when `tags` is undefined or empty,
 * we return the full list. Tidal doesn't always populate media_tags
 * on older / niche catalog entries, and stripping legitimate
 * quality options for those would surprise users more than the
 * occasional "you picked Max but got Lossless" badge does. Only
 * when we have positive signal that a tier isn't there do we hide
 * it.
 */
export function filterAvailableQualities(
  qualities: QualityOption[],
  tags: string[] | undefined,
): QualityOption[] {
  if (!tags || tags.length === 0) return qualities;
  const T = new Set(tags.map((x) => x.toUpperCase()));
  const hasHires = T.has("HIRES_LOSSLESS");
  const hasLossless = hasHires || T.has("LOSSLESS");
  return qualities.filter((q) => {
    if (q.value === "hi_res_lossless") return hasHires;
    if (q.value === "high_lossless") return hasLossless;
    // Lossy tiers (low_96k, low_320k) are universally available
    // for anything Tidal will stream at all. Anything else (future
    // tier additions) is shown by default — better to keep an
    // unrecognized option than hide one we should have allowed.
    return true;
  });
}
