/**
 * Frontend mirror of the Python filename-template renderer in
 * `app/downloader.py`. Used by the settings UI to show the user a
 * live preview of what their template will produce. Must stay in
 * sync with the backend; the unit tests cover the same shape of
 * cases as `tests/test_filename_template.py`.
 *
 * The preview is illustrative — the actual on-disk path is computed
 * by the backend with real track metadata. We use a fixed sample
 * track here so the user sees a stable example, regardless of what
 * they happen to be browsing.
 */

// eslint-disable-next-line no-control-regex -- mirrors the Python sanitizer's control-byte stripping; matches `_sanitize_segment` in app/downloader.py
const FORBIDDEN_CHARS = /[<>:"/\\|?*\x00-\x1f]/g;
const TRAILING_DOTS_OR_SPACES = /[. ]+$/;
const SEPARATOR = /[/\\]+/;
// Mirror of _WIN_RESERVED in app/downloader.py — Windows refuses
// these stems regardless of extension.
const WIN_RESERVED = new Set<string>([
  "CON",
  "PRN",
  "AUX",
  "NUL",
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
]);

/** Sample track values rendered into the preview. Picked so each
 *  token produces something distinct (rather than e.g. an artist
 *  string that happens to match the album_artist string). */
export const SAMPLE_TRACK = {
  title: "Sicko Mode",
  album: "Astroworld",
  artist: "Travis Scott, Drake",
  album_artist: "Travis Scott",
  track_num: "02",
  disc_num: "01",
  year: "2018",
  explicit: " [E]",
  album_explicit: " [E]",
} as const;

export type TemplateTokens = Record<string, string>;

/** Sanitize a single path segment — same rules as the Python
 *  `_sanitize_segment` so the preview matches what hits disk. */
export function sanitizeSegment(name: string): string {
  if (!name) return "_";
  let out = name.replace(FORBIDDEN_CHARS, "_");
  out = out.replace(TRAILING_DOTS_OR_SPACES, "");
  const stem = out.split(".", 1)[0]!.toUpperCase();
  if (WIN_RESERVED.has(stem)) {
    out = `_${out}`;
  }
  return out || "_";
}

/** Mirror of the backend SafeDict — unknown tokens render as their
 *  literal `{key}` form so the user sees a wonky filename and fixes
 *  the typo. */
export function renderTemplate(
  template: string,
  tokens: TemplateTokens,
): string {
  return template.replace(/\{([^{}]*)\}/g, (_, key: string) => {
    if (Object.prototype.hasOwnProperty.call(tokens, key)) {
      return sanitizeSegment(tokens[key] ?? "");
    }
    return `{${key}}`;
  });
}

/** Does the user's template define its own folder structure?
 *  Slashes inside `{token}` markers don't count — those are user
 *  data, not template structure. */
export function templateHasSeparator(template: string): boolean {
  const stripped = template.replace(/\{[^{}]*\}/g, "");
  return stripped.includes("/") || stripped.includes("\\");
}

export function splitTemplatePath(rendered: string): string[] {
  return rendered.split(SEPARATOR).filter((s) => s.length > 0);
}

/** Build the preview path the user sees under the template input.
 *  Returns the segments AS THEY WOULD APPEAR under output_dir; the
 *  caller decides how to render them (with separator characters and
 *  the output_dir prefix). */
export function previewFilenameTemplate(
  template: string,
  outputDir: string,
  createAlbumFolders: boolean,
): { segments: string[]; usedAlbumFolderShortcut: boolean } {
  const rendered = renderTemplate(template, SAMPLE_TRACK);
  let segments = splitTemplatePath(rendered);
  if (segments.length === 0) {
    segments = ["_"];
  }
  segments = segments.map(sanitizeSegment);

  const usedAlbumFolderShortcut =
    createAlbumFolders &&
    !templateHasSeparator(template) &&
    SAMPLE_TRACK.album.length > 0;
  if (usedAlbumFolderShortcut) {
    segments = [sanitizeSegment(SAMPLE_TRACK.album), ...segments];
  }

  // Append the extension to the final segment so the preview reads
  // like a real path (the backend appends `.flac` for FLAC streams,
  // `.m4a` for AAC — show `.flac` since that's the most common
  // delivery for Lossless / Max which is what most users pick).
  const last = segments.pop()!;
  segments.push(`${last}.flac`);

  // Only the segments — let the caller assemble the full path with
  // the output_dir and the right separator for display. Avoids
  // baking forward-slash-only output into the helper.
  void outputDir;
  return { segments, usedAlbumFolderShortcut };
}

/** Convenience for the UI: render the preview as a single display
 *  string with `/` between segments, prefixed by the output dir if
 *  one was supplied. */
export function previewFilenameTemplateAsString(
  template: string,
  outputDir: string,
  createAlbumFolders: boolean,
): string {
  const { segments } = previewFilenameTemplate(
    template,
    outputDir,
    createAlbumFolders,
  );
  const trimmedDir = outputDir.replace(/[/\\]+$/, "");
  const joined = segments.join("/");
  return trimmedDir ? `${trimmedDir}/${joined}` : joined;
}

/** Tokens the template engine knows about, surfaced so the UI can
 *  render them as a clickable / copy-pasteable list. */
export const TEMPLATE_TOKENS: ReadonlyArray<{
  token: string;
  description: string;
}> = [
  { token: "{title}", description: "Track title" },
  { token: "{track_title}", description: "Alias for {title}" },
  { token: "{artist}", description: "Track artist (joined if multiple)" },
  { token: "{album}", description: "Album title" },
  { token: "{album_title}", description: "Alias for {album}" },
  {
    token: "{album_artist}",
    description: "Album artist (falls back to track artist)",
  },
  { token: "{track_num}", description: "Two-digit track number, e.g. 03" },
  { token: "{disc_num}", description: "Two-digit disc number, e.g. 01" },
  { token: "{year}", description: "Release year (empty if unknown)" },
  {
    token: "{explicit}",
    description: 'Renders " [E]" on explicit tracks, otherwise empty',
  },
  {
    token: "{album_explicit}",
    description: "Same marker, but driven by the album-level flag",
  },
];
