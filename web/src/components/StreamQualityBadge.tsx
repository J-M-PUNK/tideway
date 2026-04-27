import type { StreamInfo } from "@/api/types";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Quality pill in the now-playing bar. Pill text is the four-tier
 * label the rest of the UI uses (Low / Medium / High / Max), so
 * settings, the streaming-quality picker, the download menus, and
 * the now-playing bar all read the same way.
 *
 * The tooltip on hover keeps the full technical readout — codec +
 * sample rate + bit depth (e.g. "FLAC · 96kHz · 24-bit") — so users
 * who want to confirm they're getting bit-perfect output can still
 * see it without having to dig.
 *
 * Tone is still tier-coded for at-a-glance distinction:
 *   Max   → primary tone (hi-res streams: > 48 kHz or ≥ 24-bit)
 *   High  → foreground   (CD-res lossless: FLAC / ALAC 16/44.1)
 *   Low / Medium → muted (lossy AAC at 96 / 320 kbps)
 *
 * Renders nothing when we don't have enough info (still loading,
 * codec unknown, etc.) — better to omit than show a placeholder.
 */
export function StreamQualityBadge({
  info,
  className,
}: {
  info: StreamInfo | null | undefined;
  className?: string;
}) {
  if (!info || !info.codec) return null;
  const codec = info.codec.toUpperCase();
  const rate = formatRate(info.sample_rate_hz);
  const depth = info.bit_depth ? `${info.bit_depth}-bit` : null;
  const tier = tierLabel(info);
  // Tone matches tier without rendering the tier name in the pill —
  // the pill is just the user-facing label, the color carries the
  // hi-res / lossless / lossy distinction.
  const tone =
    tier === "Max"
      ? "bg-primary/15 text-primary"
      : tier === "High"
        ? "bg-foreground/10 text-foreground"
        : "bg-muted-foreground/15 text-muted-foreground";

  // Tooltip stays as a full technical readout — codec + rate + depth.
  // The pill carries the tier label; the tooltip carries the specs
  // for users who care about exact rate and bit depth.
  const fullLabel = [codec, rate ? `${rate}kHz` : null, depth]
    .filter(Boolean)
    .join(" · ");

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            // flex-shrink-0 so a truncating parent (e.g. the
            // artist/metadata row in NowPlaying) can't clip the pill
            // out of existence — the label is already small and the
            // tooltip carries the rest.
            "flex-shrink-0 whitespace-nowrap rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider cursor-default",
            tone,
            className,
          )}
        >
          {tier}
        </span>
      </TooltipTrigger>
      <TooltipContent align="center">{fullLabel}</TooltipContent>
    </Tooltip>
  );
}

function formatRate(hz: number | null): string | null {
  if (!hz || hz <= 0) return null;
  // 44100 → "44.1", 96000 → "96", 192000 → "192". Trim the ".0"
  // off whole-number kHz values.
  const khz = hz / 1000;
  return khz % 1 === 0 ? String(khz) : khz.toFixed(1);
}

/**
 * Map a StreamInfo to one of the four user-facing labels. Tidal
 * streams carry the tier in `audio_quality` directly, so prefer
 * that. Local files don't have it (Tidal isn't involved at all),
 * so we fall back to deriving from codec + sample rate / bit
 * depth — local files can never be "Low" since that's a Tidal-
 * specific 96k AAC tier.
 */
function tierLabel(info: StreamInfo): "Low" | "Medium" | "High" | "Max" {
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
