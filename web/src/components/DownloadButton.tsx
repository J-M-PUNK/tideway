import { useEffect, useState } from "react";
import { Check, ChevronDown, Download } from "lucide-react";
import { api } from "@/api/client";
import type { ContentKind } from "@/api/types";
import { Button, type ButtonProps } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useQualities } from "@/hooks/useQualities";
import { cn, qualityLabel } from "@/lib/utils";

// The user's default quality comes from server settings; we cache it at
// module level so every DownloadButton mount doesn't re-fetch. A
// subscriber list lets SettingsPage push a fresh value after Save so
// open DropdownMenus reflect the new default immediately.
let _cachedDefaultQuality: string | null = null;
// In-flight dedup: without this, 20 DownloadButtons rendering in
// parallel each fire their own api.settings.get() the instant they
// mount (the cache check returns `null` until the first response lands).
let _inflight: Promise<void> | null = null;
type Sub = (q: string | null) => void;
const _subs = new Set<Sub>();

export function publishDefaultQuality(quality: string | null): void {
  _cachedDefaultQuality = quality;
  _subs.forEach((s) => s(quality));
}

/** Reset the cached default quality so the next DownloadButton mount
 * re-pulls it from the server. Used when auth state changes (e.g. PKCE
 * login) — the server may have clamped or unclamped the saved default
 * after the tier was re-detected under the new client_id. */
export function resetDefaultQualityCache(): void {
  _cachedDefaultQuality = null;
  _inflight = null;
  _subs.forEach((s) => s(null));
}

function ensureDefaultQuality(): void {
  if (_cachedDefaultQuality || _inflight) return;
  _inflight = api.settings
    .get()
    .then((s) => {
      publishDefaultQuality(s.quality);
    })
    .catch(() => {
      /* Non-critical — falls back to "Use default (session)" */
    })
    .finally(() => {
      _inflight = null;
    });
}

interface Props {
  kind: Extract<ContentKind, "track" | "album" | "playlist">;
  id: string;
  onPick: (kind: Extract<ContentKind, "track" | "album" | "playlist">, id: string, quality?: string) => void;
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
  label?: string;
  iconOnly?: boolean;
  className?: string;
  onOpenChange?: (open: boolean) => void;
  /** Optional — when supplied, each quality option gets a small
   *  annotation showing what codec the user will actually get for
   *  THIS track at that tier. Removes the "Max" ambiguity (Max could
   *  mean Dolby Atmos, MQA, hi-res FLAC, or plain lossless depending
   *  on the track). */
  audioModes?: string[];
  mediaTags?: string[];
}

/**
 * Map a quality tier + this track's Tidal format tags → what the
 * user will actually receive FROM OUR CLIENT. Returns null when the
 * tier is unambiguous (Low / Normal are always AAC).
 *
 * Immersive-audio tags (Dolby Atmos / Sony 360 RA / MQA) exist on the
 * catalog metadata, but Tidal only serves those streams to client_ids
 * on their authorized-partner list. Our PKCE session gets a stereo
 * FLAC downmix regardless of what the track is tagged as. The labels
 * below reflect what we actually get back, not what Tidal advertises.
 */
function effectiveFormatLabel(
  quality: string,
  modes: string[] | undefined,
  tags: string[] | undefined,
): string | null {
  if (!modes && !tags) return null;
  const M = new Set((modes ?? []).map((x) => x.toUpperCase()));
  const T = new Set((tags ?? []).map((x) => x.toUpperCase()));
  const immersive =
    M.has("DOLBY_ATMOS") || M.has("SONY_360RA") || T.has("MQA");
  if (quality === "hi_res_lossless") {
    if (immersive) return "Stereo downmix";
    if (T.has("HIRES_LOSSLESS")) return "Hi-Res FLAC";
    if (T.has("LOSSLESS")) return "Same as Lossless";
    return null;
  }
  if (quality === "high_lossless") {
    if (T.has("LOSSLESS") || T.has("HIRES_LOSSLESS") || immersive) return "FLAC CD";
    return null;
  }
  return null;
}

export function DownloadButton({
  kind,
  id,
  onPick,
  variant,
  size,
  label,
  iconOnly,
  className,
  onOpenChange,
  audioModes,
  mediaTags,
}: Props) {
  const qualities = useQualities() ?? [];
  const [defaultQuality, setDefaultQuality] = useState<string | null>(_cachedDefaultQuality);

  useEffect(() => {
    _subs.add(setDefaultQuality);
    return () => {
      _subs.delete(setDefaultQuality);
    };
  }, []);

  useEffect(() => {
    ensureDefaultQuality();
  }, []);

  const stop = (e: React.SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  return (
    <DropdownMenu onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild onClick={stop}>
        <Button
          variant={variant}
          size={iconOnly ? "icon" : size}
          className={cn(className)}
          title="Download…"
        >
          <Download className={iconOnly ? "h-4 w-4" : "h-4 w-4"} />
          {!iconOnly && (
            <>
              {label ?? "Download"}
              <ChevronDown className="h-3.5 w-3.5 opacity-70" />
            </>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        onClick={stop}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <DropdownMenuLabel>Download quality</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {qualities.map((q) => {
          const isDefault = defaultQuality === q.value;
          const effective = effectiveFormatLabel(q.value, audioModes, mediaTags);
          return (
            <DropdownMenuItem
              key={q.value}
              onSelect={() => onPick(kind, id, q.value)}
            >
              <div className="mt-0.5 w-4 flex-shrink-0">
                {isDefault && <Check className="h-3.5 w-3.5 text-primary" />}
              </div>
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{q.label}</span>
                  <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    {q.codec}
                  </span>
                  {effective && (
                    <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-primary">
                      {effective}
                    </span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">{q.bitrate}</div>
                <div className="text-[11px] text-muted-foreground/70">{q.description}</div>
              </div>
            </DropdownMenuItem>
          );
        })}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onPick(kind, id)}>
          <div className="w-4 flex-shrink-0" />
          <div className="flex flex-col">
            <span className="text-sm font-semibold">Use default</span>
            <span className="text-xs text-muted-foreground">
              {defaultQuality
                ? qualities.find((q) => q.value === defaultQuality)?.label ??
                  qualityLabel(defaultQuality)
                : "From Settings"}
            </span>
          </div>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
