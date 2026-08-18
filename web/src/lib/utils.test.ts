import { describe, it, expect } from "vitest";
import { darkenForLightText } from "./utils";

// WCAG contrast of an `rgb(r, g, b)` string against white, mirroring the
// math the helper targets — so the test asserts the actual readability
// guarantee, not an implementation detail.
function contrastWithWhite(color: string): number {
  const m = color.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i)!;
  const lin = (v: number) => {
    const c = Number(v) / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const L =
    0.2126 * lin(Number(m[1])) +
    0.7152 * lin(Number(m[2])) +
    0.0722 * lin(Number(m[3]));
  return 1.05 / (L + 0.05);
}

describe("darkenForLightText", () => {
  it("darkens a light color until near-white text is readable", () => {
    const src = "rgb(240, 240, 240)"; // ~1.1:1 against white — unreadable
    expect(contrastWithWhite(src)).toBeLessThan(2);
    const out = darkenForLightText(src);
    expect(contrastWithWhite(out)).toBeGreaterThanOrEqual(4.5);
  });

  it("leaves an already-dark color unchanged", () => {
    const src = "rgb(20, 30, 40)"; // already high contrast with white
    expect(contrastWithWhite(src)).toBeGreaterThanOrEqual(4.5);
    expect(darkenForLightText(src)).toBe(src);
  });

  it("preserves hue when darkening (channel ordering kept)", () => {
    // A warm light color: red > green > blue. Darkening scales all
    // channels together, so the ordering must survive.
    const out = darkenForLightText("rgb(250, 200, 120)");
    const m = out.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i)!;
    const [r, g, b] = [Number(m[1]), Number(m[2]), Number(m[3])];
    expect(r).toBeGreaterThan(g);
    expect(g).toBeGreaterThan(b);
    expect(contrastWithWhite(out)).toBeGreaterThanOrEqual(4.5);
  });

  it("passes an unparseable value through untouched", () => {
    expect(darkenForLightText("transparent")).toBe("transparent");
  });
});
