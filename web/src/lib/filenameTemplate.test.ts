import { describe, expect, it } from "vitest";

import {
  previewFilenameTemplateAsString,
  renderTemplate,
  sanitizeSegment,
  splitTemplatePath,
  templateHasSeparator,
  SAMPLE_TRACK,
} from "./filenameTemplate";

describe("sanitizeSegment", () => {
  it("replaces forbidden chars with underscore", () => {
    expect(sanitizeSegment('A:B/C\\D"E')).toBe("A_B_C_D_E");
  });
  it("strips trailing dots and spaces (Windows-hostile)", () => {
    expect(sanitizeSegment("file...   ")).toBe("file");
  });
  it("prefixes Windows-reserved stems so they don't collide", () => {
    expect(sanitizeSegment("CON")).toBe("_CON");
    expect(sanitizeSegment("CON.txt")).toBe("_CON.txt");
  });
  it("returns underscore for empty / fully-stripped input", () => {
    expect(sanitizeSegment("")).toBe("_");
    expect(sanitizeSegment("...")).toBe("_");
  });
});

describe("renderTemplate", () => {
  it("interpolates known tokens", () => {
    expect(renderTemplate("{artist} - {title}", SAMPLE_TRACK)).toBe(
      "Travis Scott, Drake - Sicko Mode",
    );
  });
  it("keeps unknown tokens as their literal {key}", () => {
    expect(renderTemplate("{album}/{xyz}", SAMPLE_TRACK)).toBe(
      "Astroworld/{xyz}",
    );
  });
  it("sanitizes token values before substituting", () => {
    expect(
      renderTemplate("{artist}", { ...SAMPLE_TRACK, artist: "AC/DC" }),
    ).toBe("AC_DC");
  });
});

describe("templateHasSeparator", () => {
  it("returns false when only tokens, no template-level slash", () => {
    expect(templateHasSeparator("{artist} - {title}")).toBe(false);
  });
  it("returns true when template literal contains /", () => {
    expect(templateHasSeparator("{album}/{title}")).toBe(true);
  });
  it("ignores slashes inside token braces", () => {
    expect(templateHasSeparator("{album/with/slash}")).toBe(false);
  });
});

describe("splitTemplatePath", () => {
  it("splits on / and \\", () => {
    expect(splitTemplatePath("a/b\\c")).toEqual(["a", "b", "c"]);
  });
  it("drops empty segments from leading or doubled slashes", () => {
    expect(splitTemplatePath("/a//b")).toEqual(["a", "b"]);
  });
});

describe("previewFilenameTemplateAsString", () => {
  it("renders the default flat template under output_dir with album folder", () => {
    const out = previewFilenameTemplateAsString(
      "{artist} - {title}",
      "/Music/Tidal",
      true,
    );
    expect(out).toBe(
      "/Music/Tidal/Astroworld/Travis Scott, Drake - Sicko Mode.flac",
    );
  });

  it("uses template-defined structure and skips the album-folder shortcut", () => {
    const out = previewFilenameTemplateAsString(
      "{album_artist}/{album}/{track_num} {title}",
      "/Music/Tidal",
      true,
    );
    expect(out).toBe(
      "/Music/Tidal/Travis Scott/Astroworld/02 Sicko Mode.flac",
    );
  });

  it("appends the explicit marker only where placed in the template", () => {
    const out = previewFilenameTemplateAsString(
      "{album}{album_explicit}/{title}{explicit}",
      "/Music",
      false,
    );
    expect(out).toBe("/Music/Astroworld [E]/Sicko Mode [E].flac");
  });
});
