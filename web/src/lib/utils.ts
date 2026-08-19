import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDuration(sec: number): string {
  if (!sec || sec < 0) return "";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

export function imageProxy(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  return `/api/image?url=${encodeURIComponent(url)}`;
}

// WCAG relative luminance of a single sRGB channel value (0–255).
function srgbChannelToLinear(value: number): number {
  const c = value / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function relativeLuminance(r: number, g: number, b: number): number {
  return (
    0.2126 * srgbChannelToLinear(r) +
    0.7152 * srgbChannelToLinear(g) +
    0.0722 * srgbChannelToLinear(b)
  );
}

/**
 * Darken an `rgb(...)` color just enough that near-white text stays
 * readable on top of it (#327). The Now Playing / lyrics view paints a
 * cover-derived color as the backdrop and lays `text-foreground`
 * (near-white) over it, so a light album cover produced white-on-white.
 * This blends the color toward black until its contrast ratio against
 * white reaches `minContrast` (WCAG AA for normal text by default),
 * scaling all channels together so the hue survives — the backdrop just
 * gets deeper. Colors already dark enough come back unchanged, and an
 * unparseable value is passed through untouched.
 */
export function darkenForLightText(color: string, minContrast = 4.5): string {
  const m = color.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (!m) return color;
  const r0 = Number(m[1]);
  const g0 = Number(m[2]);
  const b0 = Number(m[3]);
  // Contrast against white (luminance 1.0): (1 + 0.05) / (L + 0.05).
  const contrastAt = (f: number) =>
    1.05 / (relativeLuminance(r0 * f, g0 * f, b0 * f) + 0.05);
  if (contrastAt(1) >= minContrast) {
    return `rgb(${r0}, ${g0}, ${b0})`;
  }
  // Contrast rises monotonically as the scale factor shrinks toward
  // black, so binary-search the largest factor that still clears the
  // bar — the least darkening that keeps the text legible.
  let lo = 0;
  let hi = 1;
  for (let i = 0; i < 16; i++) {
    const mid = (lo + hi) / 2;
    if (contrastAt(mid) >= minContrast) lo = mid;
    else hi = mid;
  }
  // Floor rather than round: the search converges on the boundary, so
  // rounding a channel *up* could dip contrast back under the bar.
  // Flooring only ever darkens, keeping the guarantee intact.
  return `rgb(${Math.floor(r0 * lo)}, ${Math.floor(g0 * lo)}, ${Math.floor(b0 * lo)})`;
}
