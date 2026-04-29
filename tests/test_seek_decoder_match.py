"""Regression test for the seek-time decoder/stream config mismatch.

The OutputStream stays open across a seek; only the decoder is torn
down and rebuilt at the new position. Pre-fix, `_restart_decoder_at`
constructed a fresh `Decoder(new_source)` and never re-applied the
target-rate / dtype configuration the OLD decoder had been carrying.
On a shared-mode WASAPI / CoreAudio device whose mixer rate doesn't
match the source (e.g. 96 kHz FLAC source, device opens at 48 kHz
float32), the new decoder defaulted to source-rate int32 output —
and the audio_callback wrote int32 bytes into a buffer PortAudio
read as float32. The byte-level reinterpretation produced
"completely blown-out, bit-crushed" audio (the user's report).

This test pins the fix: after building the new Decoder, the seek
path MUST call `set_target_rate` with the cached
`_stream_sample_rate` so the new decoder's output configuration
matches what the open OutputStream is consuming.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.audio.player import PCMPlayer


def _bare_player() -> PCMPlayer:
    """Construct a PCMPlayer without invoking any IO. We only exercise
    `_restart_decoder_at`'s reconfiguration logic; the audio engine /
    decoder thread are mocked out so no real PyAV / sounddevice work
    happens during the test."""
    p = PCMPlayer.__new__(PCMPlayer)
    # Minimum state _restart_decoder_at touches:
    p._stop_flag = MagicMock(set=MagicMock())
    p._decoder = None
    p._decoder_thread = None
    p._lock = _DummyLock()
    p._callback_carry = None
    p._stream_sample_rate = None
    p._stream_sd_dtype = None
    p._stream_channels = None
    return p


class _DummyLock:
    """Stand-in for the `threading.RLock` that `_restart_decoder_at`
    enters via `with self._lock:`. The real RLock requires acquire /
    release pairing which is fine, but the dummy is simpler to reason
    about in a synchronous test."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_seek_calls_set_target_rate_with_cached_stream_rate():
    p = _bare_player()
    p._stream_sample_rate = 48000  # device mixer rate
    p._stream_sd_dtype = "float32"
    p._stream_channels = 2

    fake_decoder = MagicMock()
    fake_decoder.sample_rate = 96000  # source rate (e.g. hi-res FLAC)
    fake_decoder.channels = 2
    fake_decoder.sounddevice_dtype = "float32"

    with (
        patch.object(
            p, "_build_source_at", return_value=(["seg1", "seg2"], 0.0)
        ),
        patch("app.audio.player.Decoder", return_value=fake_decoder),
        patch.object(p, "_start_decoder_thread"),
    ):
        p._restart_decoder_at(target_s=12.5)

    fake_decoder.set_target_rate.assert_called_once_with(48000)


def test_seek_skips_set_target_rate_when_no_stream_open():
    """No active stream means we have no rate to match against —
    skip the reconfiguration rather than passing None / 0 down."""
    p = _bare_player()
    # _stream_sample_rate is None by default

    fake_decoder = MagicMock()
    fake_decoder.sample_rate = 44100
    fake_decoder.channels = 2
    fake_decoder.sounddevice_dtype = "int16"

    with (
        patch.object(p, "_build_source_at", return_value=(["seg1"], 0.0)),
        patch("app.audio.player.Decoder", return_value=fake_decoder),
        patch.object(p, "_start_decoder_thread"),
    ):
        p._restart_decoder_at(target_s=0.0)

    fake_decoder.set_target_rate.assert_not_called()


def test_seek_passthrough_when_stream_rate_matches_source():
    """Exclusive mode — the stream was opened at the source rate, so
    set_target_rate(source_rate) is internally a no-op (passthrough).
    The fix still calls it because the bookkeeping (decoder's
    `_target_rate` becomes None, headroom stays 1.0) is the right
    thing for downstream code to read."""
    p = _bare_player()
    p._stream_sample_rate = 96000  # exclusive on hi-res source
    p._stream_sd_dtype = "int32"
    p._stream_channels = 2

    fake_decoder = MagicMock()
    fake_decoder.sample_rate = 96000
    fake_decoder.channels = 2
    fake_decoder.sounddevice_dtype = "int32"

    with (
        patch.object(p, "_build_source_at", return_value=(["seg1"], 0.0)),
        patch("app.audio.player.Decoder", return_value=fake_decoder),
        patch.object(p, "_start_decoder_thread"),
    ):
        p._restart_decoder_at(target_s=30.0)

    fake_decoder.set_target_rate.assert_called_once_with(96000)
