import type { Album, Track } from "@/api/types";
import { cn } from "@/lib/utils";

/**
 * Audio-format filter chip row for Library / Search results.
 *
 * Scope is limited to what we can actually deliver: Max (24-bit FLAC)
 * and Lossless (FLAC CD). Atmos / Sony 360 / MQA tags exist in Tidal's
 * catalog but Tidal only serves the immersive streams to authorized-
 * partner client_ids — our PKCE session gets a stereo FLAC downmix no
 * matter what. Exposing chips for formats we can't deliver would just
 * confuse users.
 */

export type AudioFormat = "all" | "hires" | "lossless";

const LABELS: Record<AudioFormat, string> = {
  all: "All",
  hires: "Max",
  lossless: "Lossless",
};

export function matchesFormat(
  item: Pick<Track | Album, "media_tags">,
  format: AudioFormat,
): boolean {
  if (format === "all") return true;
  const tags = new Set((item.media_tags ?? []).map((s) => s.toUpperCase()));
  if (format === "hires") return tags.has("HIRES_LOSSLESS");
  if (format === "lossless") {
    return tags.has("LOSSLESS") || tags.has("HIRES_LOSSLESS");
  }
  return true;
}

export function hasAnyFormatTags(
  items: Array<Pick<Track | Album, "media_tags">>,
): boolean {
  // Show the filter only when at least one item has a tag worth
  // filtering on — a library of unlabeled 320kbps items wouldn't
  // benefit from a dead chip row.
  for (const it of items) {
    const tags = it.media_tags ?? [];
    if (
      tags.some((t) => {
        const u = t.toUpperCase();
        return u === "HIRES_LOSSLESS" || u === "LOSSLESS";
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
  const options: AudioFormat[] = ["all", "hires", "lossless"];
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
