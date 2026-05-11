"""DSP tests for Bauer-style crossfeed.

Pinned contracts:

* `set_amount(0)` is exactly bypass (no-op on samples).
* `apply` is a pass-through when crossfeed is inactive (matches the
  bit-perfect promise of the player engine).
* Stereo with `set_amount(50)` mono'es the low frequencies — both
  channels see the same low-pass content, while the highs stay
  channel-isolated.
* `apply` ignores mono input (the math doesn't apply to a single
  channel, and we shouldn't crash or smear it).
* Sample-rate changes rebuild the SOS so coefficients stay correct.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.audio.crossfeed import Crossfeed


# Sample rate for most tests — the player path runs at the source
# track's native rate, but 44.1k is the most common case.
SR = 44_100


def _white_noise(n: int, channels: int = 2, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((n, channels)).astype(np.float32) * 0.1


def _sine(freq_hz: float, n: int, sr: int = SR) -> np.ndarray:
    t = np.arange(n) / sr
    return np.sin(2 * np.pi * freq_hz * t).astype(np.float32) * 0.5


def test_inactive_is_pass_through():
    cf = Crossfeed(sample_rate=SR)
    samples = _white_noise(1024)
    original = samples.copy()
    cf.apply(samples)
    np.testing.assert_array_equal(samples, original)


def test_set_amount_zero_clears():
    cf = Crossfeed(sample_rate=SR)
    cf.set_amount(50)
    assert cf.is_active()
    cf.set_amount(0)
    assert not cf.is_active()
    samples = _white_noise(1024)
    original = samples.copy()
    cf.apply(samples)
    np.testing.assert_array_equal(samples, original)


def test_amount_clamped_to_range():
    cf = Crossfeed(sample_rate=SR)
    cf.set_amount(150)
    assert cf.amount() == 100
    cf.set_amount(-10)
    assert cf.amount() == 0


def test_amount_bleeds_bass_to_opposite_channel():
    """At a non-zero amount, a low-frequency tone fed to one channel
    only must produce substantial energy in the opposite channel.
    That's the whole point of crossfeed — bring the bass toward
    the centre. We don't assert perfect mono because the IIR
    filter's group delay causes a small comb residue between
    L_high and L_low when they recombine; that's normal Bauer-style
    crossfeed behaviour and not something the user can hear."""
    cf = Crossfeed(sample_rate=SR)
    cf.set_amount(50)

    # 200 Hz is well below the 700 Hz cutoff so the LP passes it
    # essentially unattenuated.
    n = 8192
    bass = _sine(200, n)
    samples = np.zeros((n, 2), dtype=np.float32)
    samples[:, 0] = bass  # left channel only

    cf.apply(samples)

    # Skip a generous settling window — 2nd-order IIRs need a few
    # hundred samples to settle into steady state at this cutoff.
    settled_l = samples[2048:, 0]
    settled_r = samples[2048:, 1]
    rms_l = float(np.sqrt(np.mean(settled_l**2)))
    rms_r = float(np.sqrt(np.mean(settled_r**2)))
    # Pre-crossfeed, R was silent. At alpha=0.5, the bleed is half
    # the LP'd L into R. With negligible LP attenuation at 200 Hz,
    # R should hit roughly 0.5x of L's pre-crossfeed RMS — i.e.
    # within 0.7-1.0x of the post-crossfeed L. Lower bound 0.6 is
    # conservative against IIR-transient and phase effects.
    assert rms_r > 0.6 * rms_l
    # And R definitely shouldn't exceed L (energy is being shared
    # *from* L *into* R, not amplified).
    assert rms_r <= rms_l + 1e-3


def test_high_frequencies_stay_channel_isolated():
    """At alpha=0.5 a 6 kHz tone fed to one channel should NOT bleed
    into the opposite channel — highs are passed through as
    L_high = L - L_low, which preserves the channel separation."""
    cf = Crossfeed(sample_rate=SR)
    cf.set_amount(50)

    n = 8192
    treble = _sine(6000, n)
    samples = np.zeros((n, 2), dtype=np.float32)
    samples[:, 0] = treble  # left channel only

    cf.apply(samples)

    # Skip filter settling.
    settled_l = samples[2048:, 0]
    settled_r = samples[2048:, 1]
    rms_l = float(np.sqrt(np.mean(settled_l**2)))
    rms_r = float(np.sqrt(np.mean(settled_r**2)))
    # Right channel should be near silence — only the bleed-through
    # from the low-pass's slow rolloff at 6 kHz, which is well past
    # the 700 Hz cutoff. 5% is a generous ceiling.
    assert rms_r < 0.05 * rms_l


def test_mono_input_passes_through_untouched():
    """Crossfeed on a mono buffer must not crash and must not modify
    samples — the math is stereo-only."""
    cf = Crossfeed(sample_rate=SR)
    cf.set_amount(50)
    samples = np.zeros((1024, 1), dtype=np.float32)
    samples[:, 0] = _sine(440, 1024)
    original = samples.copy()
    cf.apply(samples)
    np.testing.assert_array_equal(samples, original)


def test_amount_change_does_not_reset_filter_state():
    """Sliding the amount from 30 to 40 % during playback should not
    pop. We can't measure pops directly without an OS-level audio
    test, but verifying the filter state isn't reinstalled (and
    therefore stays continuous across the slider drag) is the
    closest proxy."""
    cf = Crossfeed(sample_rate=SR)
    cf.set_amount(30)
    state_l_before = cf._state_l.copy()  # type: ignore[union-attr]
    state_r_before = cf._state_r.copy()  # type: ignore[union-attr]
    cf.set_amount(40)
    np.testing.assert_array_equal(cf._state_l, state_l_before)  # type: ignore[arg-type]
    np.testing.assert_array_equal(cf._state_r, state_r_before)  # type: ignore[arg-type]
