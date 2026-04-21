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

// Friendly labels for quality codes, used when the full quality catalog
// isn't available (e.g. Settings saved `hi_res_lossless` but the user's
// subscription filter hides that row — we still want to show the
// human name rather than the snake_case key). Kept in sync with the
// backend QUALITIES list.
const QUALITY_LABELS: Record<string, string> = {
  low_96k: "Low",
  low_320k: "Medium",
  high_lossless: "High",
  hi_res_lossless: "Max",
};

export function qualityLabel(value: string | null | undefined): string {
  if (!value) return "";
  return QUALITY_LABELS[value] ?? value;
}
