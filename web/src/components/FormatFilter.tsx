import type { Album, Track } from "@/api/types";
import { cn } from "@/lib/utils";

/**
 * Audio-format filter chip row for Library / Search results.
 *
 * Tidal's own client shows a tiny quality glyph per row but never
 * lets you filter on it — audiophiles on every forum want this.
 * We read the tags that already come off the serialized payload
 * (`audio_modes`, `media_tags`) and expose a single-select chip row.
 *
 * Auto-hides when the underlying dataset has no tagged items — e.g.
 * an all-stereo library wouldn't benefit from the UI.
 */

export type AudioFormat =
  | "all"
  | "atmos"
  | "hires"
  | "lossless"
  | "stereo";

const LABELS: Record<AudioFormat, string> = {
  all: "All",
  atmos: "Atmos",
  hires: "Max",
  lossless: "Lossless",
  stereo: "Stereo",
};

export function matchesFormat(
  item: Pick<Track | Album, "audio_modes" | "media_tags">,
  format: AudioFormat,
): boolean {
  if (format === "all") return true;
  const modes = new Set((item.audio_modes ?? []).map((s) => s.toUpperCase()));
  const tags = new Set((item.media_tags ?? []).map((s) => s.toUpperCase()));
  switch (format) {
    case "atmos":
      return modes.has("DOLBY_ATMOS");
    case "hires":
      return tags.has("HIRES_LOSSLESS");
    case "lossless":
      // Any lossless — so HIRES_LOSSLESS also counts as lossless,
      // same way Tidal's badge does. MQA is lossy-adjacent but
      // audiophiles group it here.
      return (
        tags.has("LOSSLESS") || tags.has("HIRES_LOSSLESS") || tags.has("MQA")
      );
    case "stereo":
      // Anything that isn't explicitly a spatial mode. Lets users
      // filter out Atmos / 360 tracks when they don't have the gear.
      return !modes.has("DOLBY_ATMOS") && !modes.has("SONY_360RA");
  }
}

export function hasAnyFormatTags(
  items: Array<Pick<Track | Album, "audio_modes" | "media_tags">>,
): boolean {
  // Only show the filter when at least one item carries a usable
  // tag — otherwise it's a dead affordance. Pure stereo items don't
  // count; we need to see at least one Atmos / hi-res / MQA marker.
  for (const it of items) {
    const modes = it.audio_modes ?? [];
    const tags = it.media_tags ?? [];
    if (modes.some((m) => m.toUpperCase() === "DOLBY_ATMOS")) return true;
    if (modes.some((m) => m.toUpperCase() === "SONY_360RA")) return true;
    if (
      tags.some((t) => {
        const u = t.toUpperCase();
        return u === "HIRES_LOSSLESS" || u === "LOSSLESS" || u === "MQA";
      })
    ) {
      return true;
    }
  }
  return false;
}

export function FormatFilter({
  value,
  onChange,
}: {
  value: AudioFormat;
  onChange: (f: AudioFormat) => void;
}) {
  const options: AudioFormat[] = ["all", "atmos", "hires", "lossless", "stereo"];
  return (
    <div className="flex flex-wrap items-center gap-1">
      {options.map((opt) => (
        <button
          key={opt}
          type="button"
          onClick={() => onChange(opt)}
          className={cn(
            "rounded-full px-3 py-1 text-xs font-semibold transition-colors",
            value === opt
              ? "bg-foreground text-background"
              : "bg-secondary text-muted-foreground hover:text-foreground",
          )}
        >
          {LABELS[opt]}
        </button>
      ))}
    </div>
  );
}
