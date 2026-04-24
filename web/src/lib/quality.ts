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
