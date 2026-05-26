import { describe, expect, it } from "vitest";

import type { QualityOption, Track } from "@/api/types";
import {
  effectiveFormatLabel,
  filterAvailableQualities,
  unionTrackMediaTags,
} from "./quality";

const ALL_QUALITIES: QualityOption[] = [
  {
    value: "low_96k",
    label: "Low",
    codec: "AAC",
    bitrate: "96 kbps",
    description: "",
  },
  {
    value: "low_320k",
    label: "Medium",
    codec: "AAC",
    bitrate: "320 kbps",
    description: "",
  },
  {
    value: "high_lossless",
    label: "High",
    codec: "FLAC",
    bitrate: "1411 kbps",
    description: "",
  },
  {
    value: "hi_res_lossless",
    label: "Max",
    codec: "FLAC",
    bitrate: "up to 9216 kbps",
    description: "",
  },
];

const values = (qs: QualityOption[]) => qs.map((q) => q.value);

describe("effectiveFormatLabel", () => {
  it("returns null for non-Max qualities regardless of tags", () => {
    expect(effectiveFormatLabel("low_96k", ["HIRES_LOSSLESS"])).toBeNull();
    expect(effectiveFormatLabel("low_320k", ["HIRES_LOSSLESS"])).toBeNull();
    expect(
      effectiveFormatLabel("high_lossless", ["HIRES_LOSSLESS"]),
    ).toBeNull();
  });

  it("returns null when tags list is missing or empty", () => {
    expect(effectiveFormatLabel("hi_res_lossless", undefined)).toBeNull();
    expect(effectiveFormatLabel("hi_res_lossless", [])).toBeNull();
  });

  it("returns Hi-Res for HIRES_LOSSLESS tag on Max", () => {
    expect(effectiveFormatLabel("hi_res_lossless", ["HIRES_LOSSLESS"])).toBe(
      "Hi-Res",
    );
  });

  it("returns Same as High for LOSSLESS-only tag on Max", () => {
    expect(effectiveFormatLabel("hi_res_lossless", ["LOSSLESS"])).toBe(
      "Same as High",
    );
  });

  it("prefers HIRES_LOSSLESS when both are present", () => {
    expect(
      effectiveFormatLabel("hi_res_lossless", ["LOSSLESS", "HIRES_LOSSLESS"]),
    ).toBe("Hi-Res");
  });

  it("normalizes tag case", () => {
    expect(effectiveFormatLabel("hi_res_lossless", ["hires_lossless"])).toBe(
      "Hi-Res",
    );
    expect(effectiveFormatLabel("hi_res_lossless", ["lossless"])).toBe(
      "Same as High",
    );
  });

  it("returns null for an unrecognized tag on Max", () => {
    expect(effectiveFormatLabel("hi_res_lossless", ["DOLBY_ATMOS"])).toBeNull();
  });
});

describe("filterAvailableQualities", () => {
  it("returns the full list when tags are missing", () => {
    expect(values(filterAvailableQualities(ALL_QUALITIES, undefined))).toEqual([
      "low_96k",
      "low_320k",
      "high_lossless",
      "hi_res_lossless",
    ]);
  });

  it("returns the full list when tags are empty (no signal, fail open)", () => {
    expect(values(filterAvailableQualities(ALL_QUALITIES, []))).toEqual([
      "low_96k",
      "low_320k",
      "high_lossless",
      "hi_res_lossless",
    ]);
  });

  it("hides hi_res_lossless when only LOSSLESS is tagged", () => {
    expect(
      values(filterAvailableQualities(ALL_QUALITIES, ["LOSSLESS"])),
    ).toEqual(["low_96k", "low_320k", "high_lossless"]);
  });

  it("keeps high_lossless when HIRES_LOSSLESS is tagged (hires implies lossless)", () => {
    expect(
      values(filterAvailableQualities(ALL_QUALITIES, ["HIRES_LOSSLESS"])),
    ).toEqual(["low_96k", "low_320k", "high_lossless", "hi_res_lossless"]);
  });

  it("keeps every tier for an immersive-only tag (Thriller Atmos regression)", () => {
    // DOLBY_ATMOS / SONY_360RA is the album's spatial master, not a
    // statement about the stereo downmix. Tidal still serves a FLAC
    // stereo and our PKCE session receives it, so no tier may be
    // hidden. Thriller's canonical record is the Atmos master; the
    // old filter wrongly capped it at Medium.
    expect(
      values(filterAvailableQualities(ALL_QUALITIES, ["DOLBY_ATMOS"])),
    ).toEqual(["low_96k", "low_320k", "high_lossless", "hi_res_lossless"]);
    expect(
      values(filterAvailableQualities(ALL_QUALITIES, ["SONY_360RA"])),
    ).toEqual(["low_96k", "low_320k", "high_lossless", "hi_res_lossless"]);
  });

  it("normalizes tag case", () => {
    expect(
      values(filterAvailableQualities(ALL_QUALITIES, ["lossless"])),
    ).toEqual(["low_96k", "low_320k", "high_lossless"]);
  });
});

// Helper: build a stub Track with only the field unionTrackMediaTags
// reads. Casting through unknown so we don't have to spell out every
// required Track field for a unit test that touches one.
function _track(tags: string[] | undefined): Pick<Track, "media_tags"> {
  return { media_tags: tags } as Pick<Track, "media_tags">;
}

describe("unionTrackMediaTags", () => {
  it("returns an empty array when nothing has any tags", () => {
    // Truly lossy-only release: filterAvailableQualities will then
    // fall through its empty-tags fail-open path, preserving the
    // existing behaviour for those.
    expect(unionTrackMediaTags(undefined, undefined)).toEqual([]);
    expect(unionTrackMediaTags([], [])).toEqual([]);
    expect(
      unionTrackMediaTags([], [_track(undefined), _track([])]),
    ).toEqual([]);
  });

  it("normalises every tag to upper case", () => {
    expect(
      unionTrackMediaTags(["lossless"], [_track(["hires_lossless"])]).sort(),
    ).toEqual(["HIRES_LOSSLESS", "LOSSLESS"]);
  });

  it("includes a HIRES_LOSSLESS tag from any track in the album union", () => {
    // The motivating case for the union path: album.media_tags is
    // empty (Tidal didn't surface anything at the album level) but
    // one track is hi-res, so Max IS meaningful and shouldn't be
    // hidden.
    expect(
      unionTrackMediaTags(
        [],
        [_track(["LOSSLESS"]), _track(["HIRES_LOSSLESS"])],
      ).sort(),
    ).toEqual(["HIRES_LOSSLESS", "LOSSLESS"]);
  });

  it("returns only LOSSLESS when every track and the album are CD-only", () => {
    // This is the bug fix: previously album.media_tags=[] left the
    // filter fail-open, showing Max. Now the per-track LOSSLESS
    // shows up in the union and filterAvailableQualities hides Max.
    expect(
      unionTrackMediaTags(
        [],
        [_track(["LOSSLESS"]), _track(["LOSSLESS"])],
      ),
    ).toEqual(["LOSSLESS"]);
  });

  it("de-duplicates tags across album and per-track sources", () => {
    expect(
      unionTrackMediaTags(
        ["LOSSLESS"],
        [_track(["LOSSLESS"]), _track(["LOSSLESS"])],
      ),
    ).toEqual(["LOSSLESS"]);
  });

  it("filters falsy tag entries out (defensive against bad upstream data)", () => {
    expect(
      unionTrackMediaTags(
        ["", "LOSSLESS"],
        [_track(["", "HIRES_LOSSLESS"])],
      ).sort(),
    ).toEqual(["HIRES_LOSSLESS", "LOSSLESS"]);
  });
});
