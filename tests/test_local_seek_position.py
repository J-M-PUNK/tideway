"""Seeking a local file must land where the caller asked.

Issue #328: seeking ahead on a "Saved" (downloaded) track played the
NEXT track instead. The seek target was never the problem — for a
local file `_current_duration_ms` comes from mutagen's real file
length, so the player computed the right `target_s`. The seek itself
was wrong.

`Container.seek()` takes AV_TIME_BASE microseconds only when seeking
the container as a whole. Pass `stream=` and the offset is in THAT
stream's time_base, which for a 44.1 kHz FLAC is 1/44100. Passing
microseconds alongside a stream over-scaled the target by
`1_000_000 * time_base` = 22.7x, so every seek landed past the end of
the file. FFmpeg's backward seek clamped to the last frame, the
decoder hit EOF within a couple of frames, and the player treated
that as track-finished and advanced.

These tests decode a real FLAC through the real Decoder, because that
is the only thing that would have caught it. Every existing decoder
test builds a skeleton with `Decoder.__new__` and a mocked container,
and a mock will happily accept any integer you hand `seek()`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.audio.decoder import Decoder

RATE = 44100
DURATION_S = 30


@pytest.fixture(scope="module")
def flac_file(tmp_path_factory) -> Path:
    """A real 30-second FLAC. Built with PyAV, which is already a hard
    dependency (it is the decoder the player runs on), so this needs
    nothing beyond `pip install -r requirements.txt`.

    Each 0.1s frame carries a distinct DC level derived from its
    position, so a decoded sample identifies where in the file it came
    from without relying on frame pts.
    """
    av = pytest.importorskip("av")
    import numpy as np

    path = tmp_path_factory.mktemp("seek") / "thirty-seconds.flac"
    with av.open(str(path), mode="w", format="flac") as container:
        stream = container.add_stream("flac", rate=RATE)
        stream.layout = "stereo"
        samples_per_frame = RATE // 10
        pts = 0
        for i in range(DURATION_S * 10):
            frame = av.AudioFrame(format="s16", layout="stereo",
                                  samples=samples_per_frame)
            # Tenth-of-a-second index as a DC level, scaled to stay
            # well inside s16 range.
            level = np.int16(i * 100)
            for plane in frame.planes:
                # s16 stereo is packed: one plane holding interleaved
                # samples for both channels.
                plane.update(
                    np.full(
                        plane.buffer_size // 2, level, dtype=np.int16
                    ).tobytes()
                )
            frame.rate = RATE
            frame.pts = pts
            pts += samples_per_frame
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return path


def _position_after_seek(path: Path, target_s: float) -> float:
    """Seek a real Decoder to `target_s` and report, in seconds, where
    the audio it then produces actually came from."""
    import numpy as np

    dec = Decoder(str(path))
    try:
        dec.request_seek(target_s)
        for _ in range(20):
            pcm = dec.next_pcm()
            if pcm is None:
                raise AssertionError(
                    f"decoder hit EOF after seeking to {target_s}s — this is "
                    f"the #328 failure: the seek landed past the end of a "
                    f"{DURATION_S}s file"
                )
            if pcm.size:
                # Recover the encoded tenth-of-a-second index.
                return float(np.abs(pcm).max()) / 100.0 / 10.0
    finally:
        dec.close()
    raise AssertionError("decoder produced no audio after the seek")


@pytest.mark.parametrize("target_s", [1.0, 5.0, 15.0, 25.0])
def test_seek_lands_at_the_requested_position(flac_file, target_s):
    """The regression. Before the fix every one of these landed at
    ~29.9s, the last frame of the file."""
    landed = _position_after_seek(flac_file, target_s)
    assert landed == pytest.approx(target_s, abs=0.5), (
        f"seek to {target_s}s landed at {landed:.1f}s"
    )


def test_seek_keeps_playing_for_the_rest_of_the_track(flac_file):
    """The user-visible symptom: a mid-track seek must leave most of
    the track still to play, not a couple of frames.

    Asserting merely "not None" would pass against the bug — the
    over-scaled seek clamped to the last frame and still produced a
    chunk or two before EOF. What distinguishes the two is how much
    audio is left: ~15s from the middle of a 30s file, versus a
    fraction of a second from its tail.
    """
    dec = Decoder(str(flac_file))
    try:
        dec.request_seek(15.0)
        frames = 0
        while dec.next_pcm() is not None:
            frames += 1
        seconds_left = frames * 0.1
        assert seconds_left > DURATION_S / 3, (
            f"only {seconds_left:.1f}s of audio left after seeking to the "
            f"middle of a {DURATION_S}s file — the seek overshot the end"
        )
    finally:
        dec.close()


def test_seek_near_the_end_still_yields_audio(flac_file):
    """Seeking close to the end must play the tail, not stop."""
    landed = _position_after_seek(flac_file, DURATION_S - 3)
    assert landed == pytest.approx(DURATION_S - 3, abs=0.5)


def test_seek_backwards_after_reaching_eof(flac_file):
    """`_done` is sticky, so a seek back from EOF has to clear it or
    the track can never be resumed."""
    dec = Decoder(str(flac_file))
    try:
        while dec.next_pcm() is not None:
            pass
        dec.request_seek(5.0)
        assert dec.next_pcm() is not None, "seek back from EOF stayed done"
    finally:
        dec.close()
