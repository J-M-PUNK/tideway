/**
 * Client-side parametric EQ frequency-response curve.
 *
 * Mirrors the RBJ Audio EQ Cookbook biquads in `app/audio/eq.py`
 * (`_peaking_biquad` / `_low_shelf_biquad` / `_high_shelf_biquad`)
 * and evaluates the cascade's magnitude response in the browser, so
 * the editor's graph tracks a node in real time as it's dragged
 * instead of waiting for a server round-trip per edit.
 *
 * Evaluated at a nominal 48 kHz — the curve shape across 20 Hz–20 kHz
 * is visually identical at other rates, and this is a display curve,
 * not the audio path (that runs server-side at the real rate).
 *
 * Keep the coefficient math in sync with eq.py; eqCurve.test.ts pins
 * the shape (peak lands at the band frequency, preamp offsets the
 * whole curve) so a drift shows up as a test failure.
 */
import type { ParametricBand } from "@/api/types";

export const CURVE_SAMPLE_RATE = 48000;

// A band within this of 0 dB is a unity biquad — skip it (matches
// build_parametric_sos' bit-perfect handling).
const UNITY_GAIN_EPS_DB = 1e-6;

/** Normalized biquad coefficients with a0 folded to 1. */
interface Biquad {
  b0: number;
  b1: number;
  b2: number;
  a1: number;
  a2: number;
}

function peaking(freq: number, gainDb: number, q: number, fs: number): Biquad {
  const A = Math.pow(10, gainDb / 40);
  const w0 = (2 * Math.PI * freq) / fs;
  const cos = Math.cos(w0);
  const sin = Math.sin(w0);
  const alpha = sin / (2 * q);
  const a0 = 1 + alpha / A;
  return {
    b0: (1 + alpha * A) / a0,
    b1: (-2 * cos) / a0,
    b2: (1 - alpha * A) / a0,
    a1: (-2 * cos) / a0,
    a2: (1 - alpha / A) / a0,
  };
}

function shelf(
  freq: number,
  gainDb: number,
  q: number,
  fs: number,
  high: boolean,
): Biquad {
  const A = Math.pow(10, gainDb / 40);
  const w0 = (2 * Math.PI * freq) / fs;
  const cos = Math.cos(w0);
  const sin = Math.sin(w0);
  // Slope-Q convention, matching the Python shelf helpers — including
  // the stability floor on the sqrt argument (see _SHELF_ALPHA_FLOOR
  // in eq.py): extreme gain/slope combos would otherwise zero alpha
  // and the drawn curve would diverge from the audio path.
  const alpha =
    (sin / 2) * Math.sqrt(Math.max(0.01, (A + 1 / A) * (1 / q - 1) + 2));
  const sqrtA = Math.sqrt(A);
  const twoSqrtAalpha = 2 * sqrtA * alpha;
  // `s` is +1 for a high shelf, -1 for a low shelf — it flips the
  // cosine-sign terms between the two cookbook forms.
  const s = high ? 1 : -1;
  const a0 = A + 1 - s * (A - 1) * cos + twoSqrtAalpha;
  return {
    b0: (A * (A + 1 + s * (A - 1) * cos + twoSqrtAalpha)) / a0,
    b1: (-2 * s * A * (A - 1 + s * (A + 1) * cos)) / a0,
    b2: (A * (A + 1 + s * (A - 1) * cos - twoSqrtAalpha)) / a0,
    a1: (2 * s * (A - 1 - s * (A + 1) * cos)) / a0,
    a2: (A + 1 - s * (A - 1) * cos - twoSqrtAalpha) / a0,
  };
}

function bandBiquad(band: ParametricBand, fs: number): Biquad {
  switch (band.type) {
    case "LSC":
      return shelf(band.freq, band.gain, band.q, fs, false);
    case "HSC":
      return shelf(band.freq, band.gain, band.q, fs, true);
    default:
      return peaking(band.freq, band.gain, band.q, fs);
  }
}

/** Magnitude of one biquad (dB) at digital frequency w = 2πf/fs. */
function biquadMagnitudeDb(c: Biquad, w: number): number {
  const cos1 = Math.cos(w);
  const cos2 = Math.cos(2 * w);
  const sin1 = Math.sin(w);
  const sin2 = Math.sin(2 * w);
  const numRe = c.b0 + c.b1 * cos1 + c.b2 * cos2;
  const numIm = -(c.b1 * sin1 + c.b2 * sin2);
  const denRe = 1 + c.a1 * cos1 + c.a2 * cos2;
  const denIm = -(c.a1 * sin1 + c.a2 * sin2);
  const num = Math.sqrt(numRe * numRe + numIm * numIm);
  const den = Math.sqrt(denRe * denRe + denIm * denIm);
  return 20 * Math.log10(Math.max(num / den, 1e-12));
}

/** Geometric frequency grid for a log-x response chart. */
export function logFrequencyGrid(
  points = 384,
  fMin = 20,
  fMax = 20000,
): number[] {
  const logMin = Math.log10(fMin);
  const logMax = Math.log10(fMax);
  const out = new Array<number>(points);
  for (let i = 0; i < points; i++) {
    out[i] = Math.pow(10, logMin + ((logMax - logMin) * i) / (points - 1));
  }
  return out;
}

/**
 * Predicted response (dB) of the manual parametric cascade at each
 * frequency in `freqs`. Enabled, non-flat bands contribute; the
 * preamp is a scalar dB offset on the whole curve.
 */
export function computeEqCurve(
  bands: ParametricBand[],
  preamp: number | null,
  freqs: number[],
  fs = CURVE_SAMPLE_RATE,
): number[] {
  const base = preamp ?? 0;
  const out = new Array<number>(freqs.length).fill(base);
  for (const band of bands) {
    if (!band.enabled || Math.abs(band.gain) <= UNITY_GAIN_EPS_DB) continue;
    const c = bandBiquad(band, fs);
    for (let i = 0; i < freqs.length; i++) {
      out[i] += biquadMagnitudeDb(c, (2 * Math.PI * freqs[i]) / fs);
    }
  }
  return out;
}
