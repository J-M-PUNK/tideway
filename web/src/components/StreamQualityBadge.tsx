import type { StreamInfo } from "@/api/types";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Small pill that surfaces the codec + sample rate / bit depth of the
 * audio the user is actually hearing. Renders nothing when we don't
 * have enough info (still loading, codec unknown, etc.) — better to
 * omit than show "? kHz".
 *
 * Hover reveals the full label via a Radix tooltip — the pill text is
 * intentionally terse (e.g. "FLAC 96/24") so it fits in narrow
 * containers, and the tooltip expands to the full form
 * ("FLAC · 96kHz · 24-bit · HI RES · Tidal stream") for users who
 * want the full picture. Native `title` is unstyled + slow; the Radix
 * variant is instant and visually consistent with other hovers.
 *
 * Tiers are color-coded so high-res is visually distinct from CD-res
 * from lossy:
 *   hi-res (sample_rate > 48k or bit_depth >= 24) → primary tone
 *   lossless (FLAC/ALAC at 16/44.1)                → foreground
 *   lossy (AAC/MP3/Opus)                           → muted
 *
 * Local files keep the same visual treatment — users shouldn't care
 * whether the bits came off disk or over the wire at this level.
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
  const tier = tierOf(info);
  // Label layout: "FLAC 96/24" when we have everything, "FLAC 44.1"
  // when we only have sample rate, "AAC" when we have neither (i.e.
  // lossy where bit depth is meaningless).
  let label = codec;
  if (rate && depth) label = `${codec} ${rate}/${info.bit_depth}`;
  else if (rate) label = `${codec} ${rate}`;
  const tone =
    tier === "hires"
      ? "bg-primary/15 text-primary"
      : tier === "lossless"
        ? "bg-foreground/10 text-foreground"
        : "bg-muted-foreground/15 text-muted-foreground";

  // Full technical readout — codec + rate + depth, nothing else.
  // Tier names and source ("Streaming from Tidal") add no value
  // beyond the audible specs.
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
            // out of existence — the terse label is already small
            // and the tooltip carries the rest.
            "flex-shrink-0 whitespace-nowrap rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider cursor-default",
            tone,
            className,
          )}
        >
          {label}
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

function tierOf(info: StreamInfo): "hires" | "lossless" | "lossy" {
  const codec = (info.codec || "").toLowerCase();
  if (codec === "flac" || codec === "alac") {
    if (
      (info.bit_depth !== null && info.bit_depth >= 24) ||
      (info.sample_rate_hz !== null && info.sample_rate_hz > 48000)
    ) {
      return "hires";
    }
    return "lossless";
  }
  return "lossy";
}
