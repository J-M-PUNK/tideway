import { describe, expect, it } from "vitest";

import type { StreamInfo } from "@/api/types";
import {
  effectiveFormatLabel,
  tierFromPreference,
  tierFromStreamInfo,
} from "./quality";

function makeInfo(over: Partial<StreamInfo> = {}): StreamInfo {
  return {
    source: "stream",
    codec: "flac",
    bit_depth: 16,
    sample_rate_hz: 44100,
    audio_quality: null,
    audio_mode: null,
    ...over,
  };
}

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

describe("tierFromStreamInfo", () => {
  it("uses Tidal's audio_quality string when present", () => {
    expect(tierFromStreamInfo(makeInfo({ audio_quality: "LOW" }))).toBe("Low");
    expect(tierFromStreamInfo(makeInfo({ audio_quality: "HIGH" }))).toBe(
      "Medium",
    );
    expect(tierFromStreamInfo(makeInfo({ audio_quality: "LOSSLESS" }))).toBe(
      "High",
    );
    expect(
      tierFromStreamInfo(makeInfo({ audio_quality: "HI_RES_LOSSLESS" })),
    ).toBe("Max");
  });

  it("treats HI_RES (legacy MQA tier) as Max", () => {
    expect(tierFromStreamInfo(makeInfo({ audio_quality: "HI_RES" }))).toBe(
      "Max",
    );
  });

  it("normalizes case on the audio_quality string", () => {
    expect(tierFromStreamInfo(makeInfo({ audio_quality: "lossless" }))).toBe(
      "High",
    );
    expect(
      tierFromStreamInfo(makeInfo({ audio_quality: "hi_res_lossless" })),
    ).toBe("Max");
  });

  it("derives from codec + sample rate / bit depth for local files", () => {
    // Local FLAC at CD quality → High
    expect(
      tierFromStreamInfo(
        makeInfo({
          codec: "flac",
          bit_depth: 16,
          sample_rate_hz: 44100,
          audio_quality: null,
        }),
      ),
    ).toBe("High");

    // Local FLAC at 24-bit → Max
    expect(
      tierFromStreamInfo(
        makeInfo({
          codec: "flac",
          bit_depth: 24,
          sample_rate_hz: 96000,
          audio_quality: null,
        }),
      ),
    ).toBe("Max");

    // Local FLAC at 16-bit but >48 kHz → Max
    expect(
      tierFromStreamInfo(
        makeInfo({
          codec: "flac",
          bit_depth: 16,
          sample_rate_hz: 88200,
          audio_quality: null,
        }),
      ),
    ).toBe("Max");

    // Local ALAC at CD quality → High
    expect(
      tierFromStreamInfo(
        makeInfo({
          codec: "alac",
          bit_depth: 16,
          sample_rate_hz: 44100,
          audio_quality: null,
        }),
      ),
    ).toBe("High");

    // Local lossy file (rare; falls back to Medium)
    expect(
      tierFromStreamInfo(
        makeInfo({
          codec: "mp3",
          bit_depth: null,
          sample_rate_hz: 44100,
          audio_quality: null,
        }),
      ),
    ).toBe("Medium");
  });
});

describe("tierFromPreference", () => {
  it("maps each preference to its tier label exhaustively", () => {
    expect(tierFromPreference("low_96k")).toBe("Low");
    expect(tierFromPreference("low_320k")).toBe("Medium");
    expect(tierFromPreference("high_lossless")).toBe("High");
    expect(tierFromPreference("hi_res_lossless")).toBe("Max");
  });
});
