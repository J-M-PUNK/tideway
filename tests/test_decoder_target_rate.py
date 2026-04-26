"""Tests for the Decoder's target-rate switching ("foobar approach").

`set_target_rate` is the heart of how we dodge intersample-peak
clipping in the OS resampler. When the source rate matches the target
the decoder stays in bit-perfect passthrough; when they differ it
flips to float32 at the target rate with ~1 dB pre-emit headroom so
the resampling step has room for true-peak overshoot.

Constructing a real `Decoder` requires opening an audio container, so
we bypass `__init__` and assemble a minimal skeleton with just the
fields these methods touch. Keeps the tests fast and reduces them to
the actual logic we changed.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.audio.decoder import Decoder, _FORMAT_MAP, _RESAMPLE_HEADROOM


def _make_skeleton(source_rate: int = 48000, source_format: str = "s32"):
    """Build a Decoder with just the state set_target_rate / _emit /
    output_* properties touch. Skips `av.open` entirely."""
    dec = Decoder.__new__(Decoder)
    out_fmt, dtype, sd_dtype, bit_depth = _FORMAT_MAP[source_format]
    dec._sample_rate = source_rate
    dec._source_format = source_format
    dec._source_format_packed = out_fmt
    dec._source_dtype = dtype
    dec._source_sd_dtype = sd_dtype
    dec._bit_depth = bit_depth
    dec._target_rate = None
    dec._output_format = out_fmt
    dec._output_dtype = dtype
    dec._sd_dtype = sd_dtype
    dec._headroom = 1.0
    dec._resampler = MagicMock()
    return dec


def _spy_make_resampler(dec) -> list:
    """Replace `_make_resampler` with a counter so we can assert
    rebuild count. Returns a list whose length equals call count."""
    calls: list = []

    def fake_make():
        m = MagicMock()
        calls.append(m)
        return m

    dec._make_resampler = fake_make
    return calls


# ---------------------------------------------------------------------------
# set_target_rate
# ---------------------------------------------------------------------------


def test_passthrough_when_target_equals_source():
    dec = _make_skeleton(source_rate=48000)
    calls = _spy_make_resampler(dec)
    dec.set_target_rate(48000)
    assert dec._target_rate is None
    assert dec.is_resampling_internally is False
    assert dec.output_sample_rate == 48000
    assert dec.sounddevice_dtype == "int32"
    assert dec.output_dtype is np.int32
    assert dec._headroom == 1.0
    # Already in passthrough, no rebuild needed.
    assert calls == []


def test_passthrough_when_rate_is_zero_or_negative():
    dec = _make_skeleton()
    calls = _spy_make_resampler(dec)
    dec.set_target_rate(0)
    dec.set_target_rate(-1)
    assert dec._target_rate is None
    assert calls == []


def test_switches_to_float32_when_target_differs():
    dec = _make_skeleton(source_rate=96000, source_format="s32")
    calls = _spy_make_resampler(dec)
    dec.set_target_rate(48000)
    assert dec._target_rate == 48000
    assert dec.is_resampling_internally is True
    assert dec.output_sample_rate == 48000
    assert dec.sounddevice_dtype == "float32"
    assert dec.output_dtype is np.float32
    assert dec._output_format == "flt"
    assert dec._headroom == _RESAMPLE_HEADROOM
    assert len(calls) == 1


def test_idempotent_when_called_with_same_target_rate():
    dec = _make_skeleton(source_rate=96000)
    calls = _spy_make_resampler(dec)
    dec.set_target_rate(48000)
    assert len(calls) == 1
    dec.set_target_rate(48000)
    # Rebuild MUST be skipped — the gapless-bridge path adopts a
    # decoder mid-decode and a rebuild would drop libav's buffered
    # samples.
    assert len(calls) == 1


def test_idempotent_when_called_with_source_rate_in_passthrough():
    dec = _make_skeleton(source_rate=44100)
    calls = _spy_make_resampler(dec)
    dec.set_target_rate(44100)
    dec.set_target_rate(44100)
    assert calls == []


def test_back_to_passthrough_restores_source_dtype():
    dec = _make_skeleton(source_rate=44100, source_format="s16")
    calls = _spy_make_resampler(dec)
    dec.set_target_rate(48000)
    assert dec.is_resampling_internally is True
    assert dec.sounddevice_dtype == "float32"
    dec.set_target_rate(44100)  # back to source
    assert dec.is_resampling_internally is False
    assert dec.sounddevice_dtype == "int16"
    assert dec.output_dtype is np.int16
    assert dec._headroom == 1.0
    assert len(calls) == 2  # one to switch out, one to switch back


def test_change_between_two_non_source_targets_rebuilds():
    dec = _make_skeleton(source_rate=96000)
    calls = _spy_make_resampler(dec)
    dec.set_target_rate(48000)
    dec.set_target_rate(44100)
    assert dec._target_rate == 44100
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# _emit (headroom application)
# ---------------------------------------------------------------------------


def _frame(arr: np.ndarray):
    """Wrap a numpy array as a mock PyAV AudioFrame with `to_ndarray`."""
    f = MagicMock()
    f.to_ndarray.return_value = arr
    return [f]


def test_emit_passthrough_preserves_int_samples_exactly():
    dec = _make_skeleton(source_rate=48000, source_format="s32")
    sample = np.array([[2_000_000_000, -2_000_000_000]], dtype=np.int32)
    out = dec._emit(_frame(sample))
    assert out.dtype == np.int32
    assert out[0, 0] == 2_000_000_000
    assert out[0, 1] == -2_000_000_000


def test_emit_applies_headroom_when_resampling():
    dec = _make_skeleton(source_rate=96000)
    _spy_make_resampler(dec)
    dec.set_target_rate(48000)
    # In float32 mode now. Full-scale +/-1.0 input becomes
    # +/- _RESAMPLE_HEADROOM after _emit.
    sample = np.array([[1.0, -1.0]], dtype=np.float32)
    out = dec._emit(_frame(sample))
    assert out.dtype == np.float32
    assert abs(out[0, 0] - _RESAMPLE_HEADROOM) < 1e-6
    assert abs(out[0, 1] + _RESAMPLE_HEADROOM) < 1e-6


def test_emit_skips_multiplication_in_passthrough():
    """Passthrough is a hot path: per-frame multiplication on int
    arrays would force a float roundtrip. Verify the headroom branch
    is taken only when _headroom != 1.0 by checking that int32 max
    survives `_emit` exactly — a float roundtrip would round it down
    by one because float32 only has 24 mantissa bits."""
    dec = _make_skeleton(source_rate=44100, source_format="s32")
    sample = np.array([[2_147_483_647, -2_147_483_648]], dtype=np.int32)
    out = dec._emit(_frame(sample))
    assert out.dtype == np.int32
    assert out[0, 0] == 2_147_483_647
    assert out[0, 1] == -2_147_483_648


# ---------------------------------------------------------------------------
# Property accessors
# ---------------------------------------------------------------------------


def test_sample_rate_property_always_reports_source():
    """sample_rate is what codec_info uses for the UI quality badge.
    It must NOT change when set_target_rate flips the output rate."""
    dec = _make_skeleton(source_rate=192000)
    _spy_make_resampler(dec)
    assert dec.sample_rate == 192000
    dec.set_target_rate(48000)
    assert dec.sample_rate == 192000  # source rate unchanged
    assert dec.output_sample_rate == 48000


def test_output_sample_rate_falls_back_to_source_in_passthrough():
    dec = _make_skeleton(source_rate=44100)
    assert dec.output_sample_rate == 44100
