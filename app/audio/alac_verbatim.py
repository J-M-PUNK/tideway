"""Minimal ALAC "verbatim" (uncompressed) frame encoder.

AirPlay 2 receivers that decode ALAC build their codec extradata
from the negotiated `audioFormat` constant with `spf=352` baked
in (see `.airplay2-test/airplay2-receiver/ap2/connections/audio.py`
line 467 — `AudioSetup(codec_tag='alac', sr=44100, ss=16, cc=2)`
uses the class default `spf=352`). PyAV's ALAC encoder is fixed at
`frame_size=4096` and isn't user-overridable, so we cannot use it
for AirPlay output.

This module writes ALAC frames in the verbatim/uncompressed
("escape") mode: a small bit-packed header declares the frame as
non-compressed, followed by raw interleaved s16 stereo samples,
followed by an END_OF_FRAME element marker. The result is larger
than a properly compressed ALAC frame (we waste ~10% over plain
LPCM due to the wrapper) but it's accepted by every ALAC decoder
including the one the AirPlay 2 receiver assembles via libav.

The bitstream layout follows the public Apple ALAC reference
encoder (`alac/codec/ALACEncoder.cpp`) for the "writeRawData" path
that runs when the compressed candidate is larger than the input.
The element layout for a Channel Pair Element (stereo):

    3 bits  element_type = 1 (ID_CPE)
    4 bits  element_instance_tag = 0
    12 bits unused = 0
    1 bit   partial_frame (1 if frame_size != mConfig.frameLength)
    2 bits  shift_off = 0
    1 bit   is_not_compressed = 1  ← verbatim escape
    32 bits frame_size (only present when partial_frame == 1)

Followed by 2 * frame_size interleaved s16 samples, big-endian
(samples are packed at the sample size declared in the codec
config — 16 bits each). Followed by:

    3 bits  element_type = 7 (ID_END)

And then 0-bit padding to a byte boundary.

For frame_size = 352 stereo s16:

    55 (header) + 352 * 32 (audio) + 3 (end) = 11322 bits
    = 1415.25 bytes → 1416 bytes after byte-align padding.

There are no compression coefficients, no Rice parameters, no
mix-residual data. The decoder reads each sample size's worth of
bits per sample directly off the bitstream.
"""

from __future__ import annotations

import struct

# Element type tags.
_ID_CPE = 1  # Channel Pair Element (stereo)
_ID_END = 7  # End-of-frame marker

# Channel index defaults; harmless for stereo.
_INSTANCE_TAG = 0
_SHIFT_OFF = 0


class _BitWriter:
    """Tiny MSB-first bit packer. Accumulates a 64-bit register and
    flushes whole bytes as they fill. We don't need a general-purpose
    bitstream library — every ALAC verbatim frame fits a known shape
    and the audio samples are byte-aligned anyway, so the only
    bit-level work happens in the ~55-bit header and the 3-bit end
    marker."""

    __slots__ = ("_buf", "_reg", "_bits")

    def __init__(self, capacity: int) -> None:
        self._buf = bytearray(capacity)
        # Write cursor into _buf is implicit: len(_buf) - remaining.
        # We use an external `_pos` via slicing below to avoid the
        # per-write list-append overhead.
        self._reg = 0
        self._bits = 0
        self._buf.clear()

    def push(self, value: int, nbits: int) -> None:
        if nbits <= 0:
            return
        # Mask to nbits in case caller passed extra; values that don't
        # fit are a programming error so we mask defensively rather
        # than asserting (the hot path doesn't want an assert per call).
        value &= (1 << nbits) - 1
        self._reg = (self._reg << nbits) | value
        self._bits += nbits
        while self._bits >= 8:
            self._bits -= 8
            byte = (self._reg >> self._bits) & 0xFF
            self._buf.append(byte)
            self._reg &= (1 << self._bits) - 1 if self._bits else 0

    def push_bytes_aligned(self, data: bytes) -> None:
        """Fast-path for the audio payload once the bit position has
        been advanced past a byte boundary. ALAC's verbatim audio
        section ISN'T byte-aligned in general (the 55-bit header
        leaves us mid-byte), so this path is unused; we go through
        push() with nbits=16 per sample which is still fast enough."""
        for b in data:
            self.push(b, 8)

    def finish(self) -> bytes:
        # Pad to the next byte boundary with zero bits.
        if self._bits:
            self._reg <<= 8 - self._bits
            self._buf.append(self._reg & 0xFF)
            self._reg = 0
            self._bits = 0
        return bytes(self._buf)


def encode_verbatim_stereo_s16(pcm_be: bytes, frame_size: int) -> bytes:
    """Encode one ALAC verbatim frame.

    `pcm_be` is `frame_size * 2 * 2` bytes of interleaved big-endian
    s16 stereo PCM (LR LR LR ...). Returns the ALAC frame bytes
    suitable for putting directly into the RTP payload.

    Verbatim mode means the decoder treats every "sample" word as
    raw PCM at the codec's sample size, so we just write the LRLR
    stream straight through. No prediction, no Rice coding.
    """
    if len(pcm_be) != frame_size * 4:
        raise ValueError(
            f"expected {frame_size * 4} bytes of stereo s16 PCM, "
            f"got {len(pcm_be)}"
        )

    # Maximum payload size: header (7 bytes) + audio (frame_size * 4) +
    # end marker + padding. Round up generously; finish() trims.
    bw = _BitWriter(capacity=frame_size * 4 + 16)

    # Header: 23-bit fixed prelude + 32-bit frame_size.
    bw.push(_ID_CPE, 3)              # 001
    bw.push(_INSTANCE_TAG, 4)         # 0000
    bw.push(0, 12)                    # 12 unused bits
    bw.push(1, 1)                     # partial_frame = 1 (always declare)
    bw.push(_SHIFT_OFF, 2)            # 00
    bw.push(1, 1)                     # is_not_compressed = 1 → verbatim
    bw.push(frame_size, 32)           # frame_size as 32-bit be

    # Audio data: interleaved 16-bit samples in pcm_be. The samples
    # are already big-endian; push them as 16-bit groups.
    for i in range(0, len(pcm_be), 2):
        sample = (pcm_be[i] << 8) | pcm_be[i + 1]
        bw.push(sample, 16)

    # End-of-frame element.
    bw.push(_ID_END, 3)

    return bw.finish()


def encode_verbatim_stereo_s16_iter(
    pcm_iter, frame_size: int
):
    """Generator wrapper for streaming: yields one ALAC verbatim
    payload per chunk in `pcm_iter`. Each chunk must be exactly
    `frame_size * 4` bytes. Convenience so the caller doesn't have
    to manage byte counts itself when iterating over a continuous
    PCM source.
    """
    for chunk in pcm_iter:
        yield encode_verbatim_stereo_s16(chunk, frame_size)


__all__ = ["encode_verbatim_stereo_s16", "encode_verbatim_stereo_s16_iter"]
