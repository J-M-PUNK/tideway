import type { QualityOption, Track } from "@/api/types";

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


/**
 * Union the album's media_tags with the per-track media_tags so the
 * download menu's quality filter sees the truth. Tidal's
 * album.media_tags is unreliable — it sometimes comes back empty even
 * when individual tracks carry HIRES_LOSSLESS / LOSSLESS — and using
 * just the album-level field caused the album-page download menu to
 * offer Max on releases where Max doesn't deliver a different file.
 *
 * The union semantics are right for the "should we offer Max?"
 * question:
 *   - If ANY track is HIRES_LOSSLESS, Max is meaningful (you get a
 *     real hi-res FLAC for that track; the rest fall back to
 *     Lossless's CD-res FLAC).
 *   - If every track has only LOSSLESS, Max is the same file as
 *     Lossless and should be hidden.
 *   - If nothing has any tags (truly lossy-only release), we return
 *     an empty array so filterAvailableQualities falls through to its
 *     existing fail-open behaviour — which preserves the surface area
 *     of older fixes (e.g. Thriller's Atmos-only tagging where the
 *     stereo lossless downmix is still real).
 *
 * Tags are normalised to upper-case in the output so callers don't
 * have to.
 */
export function unionTrackMediaTags(
  albumTags: string[] | undefined,
  tracks: Pick<Track, "media_tags">[] | undefined,
): string[] {
  const out = new Set<string>();
  for (const tag of albumTags ?? []) {
    if (tag) out.add(tag.toUpperCase());
  }
  for (const t of tracks ?? []) {
    for (const tag of t.media_tags ?? []) {
      if (tag) out.add(tag.toUpperCase());
    }
  }
  return Array.from(out);
}
