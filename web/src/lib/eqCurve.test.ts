import { describe, expect, it } from "vitest";
import { computeEqCurve, logFrequencyGrid } from "./eqCurve";
import type { ParametricBand } from "@/api/types";

function band(over: Partial<ParametricBand> = {}): ParametricBand {
  return { type: "PK", freq: 1000, gain: 0, q: 1, enabled: true, ...over };
}

const GRID = logFrequencyGrid(512);

function dbAt(curve: number[], freqHz: number): number {
  let best = 0;
  let bestDist = Infinity;
  for (let i = 0; i < GRID.length; i++) {
    const d = Math.abs(GRID[i] - freqHz);
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return curve[best];
}

describe("computeEqCurve", () => {
  it("is flat at 0 dB with no active bands", () => {
    const curve = computeEqCurve([], null, GRID);
    expect(Math.max(...curve.map(Math.abs))).toBeLessThan(1e-9);
  });

  it("applies the preamp as a whole-curve offset", () => {
    const curve = computeEqCurve([], -6, GRID);
    for (const v of curve) expect(v).toBeCloseTo(-6, 6);
  });

  it("skips flat and disabled bands (bit-perfect)", () => {
    const curve = computeEqCurve(
      [band({ gain: 0 }), band({ gain: 6, enabled: false })],
      null,
      GRID,
    );
    expect(Math.max(...curve.map(Math.abs))).toBeLessThan(1e-9);
  });

  it("peaks at the band frequency with the band's gain", () => {
    const curve = computeEqCurve(
      [band({ freq: 1000, gain: 6, q: 1 })],
      null,
      GRID,
    );
    let peakIdx = 0;
    for (let i = 1; i < curve.length; i++)
      if (curve[i] > curve[peakIdx]) peakIdx = i;
    expect(curve[peakIdx]).toBeCloseTo(6, 1);
    expect(GRID[peakIdx]).toBeGreaterThan(800);
    expect(GRID[peakIdx]).toBeLessThan(1250);
  });

  it("low shelf lifts the lows and settles to 0 up high", () => {
    const curve = computeEqCurve(
      [band({ type: "LSC", freq: 120, gain: 6, q: 0.7 })],
      null,
      GRID,
    );
    expect(dbAt(curve, 20)).toBeCloseTo(6, 0);
    expect(dbAt(curve, 15000)).toBeCloseTo(0, 0);
  });

  it("high shelf lifts the highs and settles to 0 down low", () => {
    const curve = computeEqCurve(
      [band({ type: "HSC", freq: 8000, gain: 6, q: 0.7 })],
      null,
      GRID,
    );
    expect(dbAt(curve, 20000)).toBeCloseTo(6, 0);
    expect(dbAt(curve, 30)).toBeCloseTo(0, 0);
  });
});

/**
 * Golden fixture: dB values computed by the Python implementation
 * (app/audio/eq.py build_parametric_sos + cascade_magnitude_db at
 * 48 kHz). This is the cross-language parity guard — the shape tests
 * above tolerate ~1 dB, which would let a subtle coefficient drift
 * (wrong shelf-Q convention, changed alpha clamp) slip through while
 * the drawn curve and the audible response diverge. If this fails
 * after an intentional eq.py change, regenerate the values from
 * Python rather than loosening the tolerance.
 */
describe("parity with app/audio/eq.py", () => {
  const FREQS = [20, 100, 1000, 5000, 20000];
  const GOLDEN: { bands: ParametricBand[]; expected: number[] }[] = [
    {
      bands: [band({ type: "PK", freq: 1000, gain: 6, q: 1 })],
      expected: [0.0025, 0.0652, 6.0, 0.2486, 0.002],
    },
    {
      bands: [band({ type: "LSC", freq: 120, gain: 6, q: 0.7 })],
      expected: [5.9222, 3.7211, 0.0407, 0.0015, 0.0],
    },
    {
      bands: [band({ type: "HSC", freq: 8000, gain: -4, q: 0.7 })],
      expected: [-0.0, -0.0002, -0.0233, -0.7695, -3.9561],
    },
    {
      // Hits the shelf stability floor (alpha sqrt-arg clamp) — pins
      // that both implementations clamp identically.
      bands: [band({ type: "LSC", freq: 1000, gain: 24, q: 10 })],
      expected: [24.0129, 24.3284, 12.0, -1.3053, -0.0099],
    },
    {
      bands: [
        band({ type: "LSC", freq: 105, gain: 4, q: 0.7 }),
        band({ type: "PK", freq: 2500, gain: -3, q: 2 }),
        band({ type: "HSC", freq: 10000, gain: 5, q: 0.7 }),
      ],
      expected: [3.9317, 2.1324, -0.124, 0.2468, 4.8976],
    },
  ];

  it.each(GOLDEN.map((g, i) => [i, g] as const))(
    "case %i matches the Python curve within 0.05 dB",
    (_i, g) => {
      const curve = computeEqCurve(g.bands, null, FREQS);
      for (let j = 0; j < FREQS.length; j++) {
        expect(Math.abs(curve[j] - g.expected[j])).toBeLessThan(0.05);
      }
    },
  );
});
