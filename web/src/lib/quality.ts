import type { StreamInfo } from "@/api/types";
import type { StreamingQuality } from "@/hooks/useUiPreferences";

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

export type QualityTier = "Low" | "Medium" | "High" | "Max";

/**
 * Map a streaming-quality preference to its tier label. Same labels
 * the picker dropdown shows in its options. Used by the now-playing
 * picker as a fallback when nothing's playing yet (no `streamInfo`
 * to derive from).
 */
export function tierFromPreference(q: StreamingQuality): QualityTier {
  switch (q) {
    case "low_96k":
      return "Low";
    case "low_320k":
      return "Medium";
    case "high_lossless":
      return "High";
    case "hi_res_lossless":
      return "Max";
  }
}

/**
 * Map a backend `StreamInfo` to the four user-facing tier labels
 * (Low, Medium, High, Max). Tidal streams carry the tier in
 * `audio_quality` directly, so prefer that. Local files don't have
 * it — Tidal isn't involved at all — so we fall back to deriving
 * from codec + sample rate / bit depth. Local files can never be
 * "Low" since that's a Tidal-specific 96k AAC tier.
 *
 * Shared between the small `StreamQualityBadge` (left of the now-
 * playing bar) and the `StreamingQualityPicker` (right side
 * dropdown). Both render the actual playback tier, not the user's
 * setting, so a Max-preference user playing a track Tidal only has
 * at Lossless sees "High" in both places. The picker's dropdown
 * still presents the four preference options.
 */
export function tierFromStreamInfo(info: StreamInfo): QualityTier {
  const aq = (info.audio_quality || "").toUpperCase();
  if (aq === "LOW") return "Low";
  if (aq === "HIGH") return "Medium";
  if (aq === "LOSSLESS") return "High";
  if (aq === "HI_RES" || aq === "HI_RES_LOSSLESS") return "Max";

  const codec = (info.codec || "").toLowerCase();
  if (codec === "flac" || codec === "alac") {
    const isHiRes =
      (info.bit_depth !== null && info.bit_depth >= 24) ||
      (info.sample_rate_hz !== null && info.sample_rate_hz > 48000);
    return isHiRes ? "Max" : "High";
  }
  // Lossy local file (rare). "Medium" is the closest user-facing
  // bucket; we have no way to distinguish 96 kbps from 320 kbps
  // sources without Tidal's tier string.
  return "Medium";
}
