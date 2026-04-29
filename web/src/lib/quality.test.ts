import { describe, expect, it } from "vitest";

import type { QualityOption } from "@/api/types";
import { effectiveFormatLabel, filterAvailableQualities } from "./quality";

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

  it("hides both lossless tiers when only an unsupported tag is present", () => {
    // DOLBY_ATMOS / SONY_360RA aren't deliverable to our PKCE
    // session, so a track tagged only with those is effectively
    // lossy-only — neither lossless tier should show.
    expect(
      values(filterAvailableQualities(ALL_QUALITIES, ["DOLBY_ATMOS"])),
    ).toEqual(["low_96k", "low_320k"]);
  });

  it("normalizes tag case", () => {
    expect(
      values(filterAvailableQualities(ALL_QUALITIES, ["lossless"])),
    ).toEqual(["low_96k", "low_320k", "high_lossless"]);
  });
});
