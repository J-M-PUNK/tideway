import { describe, expect, it } from "vitest";

import { effectiveFormatLabel } from "./quality";

describe("effectiveFormatLabel", () => {
  it("returns null for non-Max qualities regardless of tags", () => {
    expect(effectiveFormatLabel("low_96k", ["HIRES_LOSSLESS"])).toBeNull();
    expect(effectiveFormatLabel("low_320k", ["HIRES_LOSSLESS"])).toBeNull();
    expect(effectiveFormatLabel("high_lossless", ["HIRES_LOSSLESS"])).toBeNull();
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
    expect(
      effectiveFormatLabel("hi_res_lossless", ["DOLBY_ATMOS"]),
    ).toBeNull();
  });
});
