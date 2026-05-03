"""Parser for AutoEQ's `ParametricEQ.txt` format.

These files come from the [AutoEQ project](https://github.com/jaakkopasanen/AutoEq),
which publishes per-headphone parametric EQ corrections targeting
neutral / Harman / B&K curves. The file layout is regular and
small:

    Preamp: -6.5 dB
    Filter 1: ON LSC Fc 105 Hz Gain 6.0 dB Q 0.7
    Filter 2: ON PK Fc 200 Hz Gain -3.0 dB Q 1.41
    Filter 3: ON HSC Fc 8000 Hz Gain -2.0 dB Q 0.7

Each "Filter N" line is a band: type code (PK / LSC / HSC), centre
frequency (Hz), gain (dB), and slope-Q. We parse every line into
an `AutoEqProfile` dataclass and feed those bands through the
shelf-aware biquad helpers in `app/audio/eq.py`.

Failure mode: the parser is strict on the line shape but lenient
about whitespace and decimal precision. A malformed line raises
`AutoEqParseError` rather than silently producing a partial
profile — caller decides whether to skip the offender or surface
the error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# AutoEQ's `*ParametricEQ.txt` files use these three filter type
# codes. Mirrors the constants in `app/audio/eq.py`; we keep our
# own copy here to avoid a circular import between the parser and
# the audio engine.
PEAKING = "PK"
LOW_SHELF = "LSC"
HIGH_SHELF = "HSC"
_VALID_TYPES = frozenset({PEAKING, LOW_SHELF, HIGH_SHELF})


@dataclass
class AutoEqBand:
    """One filter band parsed from a ParametricEQ.txt file."""

    filter_type: str
    freq_hz: float
    gain_db: float
    q: float


@dataclass
class AutoEqProfile:
    """A complete AutoEQ profile parsed from one file.

    `profile_id`, `brand`, `model`, and `source` are filled in by
    the caller (the directory walk in `index.py` knows which
    headphone + measurement source each file belongs to). The
    parser only handles the file contents.
    """

    profile_id: str
    brand: str
    model: str
    source: str
    target: Optional[str] = None
    preamp_db: float = 0.0
    bands: list[AutoEqBand] = field(default_factory=list)


class AutoEqParseError(ValueError):
    """Raised on a malformed ParametricEQ.txt file. The message
    includes the offending line index for easy debugging against
    a real file."""


# `Preamp: <gain> dB` — gain may be signed, may have decimals.
_PREAMP_RE = re.compile(
    r"^\s*Preamp\s*:\s*(-?\d+(?:\.\d+)?)\s*dB\s*$",
    re.IGNORECASE,
)

# `Filter N: ON <type> Fc <hz> Hz Gain <db> dB Q <q>` — one band.
# The "ON" / "OFF" toggle is in the spec but every AutoEQ-emitted
# file ships ON; we still tolerate OFF (skip the band) since
# user-edited files might use it.
_FILTER_RE = re.compile(
    r"""^
    \s*Filter\s+\d+\s*:\s*
    (?P<state>ON|OFF)\s+
    (?P<type>[A-Z]+)\s+
    Fc\s+(?P<fc>-?\d+(?:\.\d+)?)\s*Hz\s+
    Gain\s+(?P<gain>-?\d+(?:\.\d+)?)\s*dB\s+
    Q\s+(?P<q>-?\d+(?:\.\d+)?)\s*
    $""",
    re.IGNORECASE | re.VERBOSE,
)


def parse_profile_text(
    text: str,
    *,
    profile_id: str = "",
    brand: str = "",
    model: str = "",
    source: str = "",
    target: Optional[str] = None,
) -> AutoEqProfile:
    """Parse the contents of a ParametricEQ.txt file into an
    `AutoEqProfile`. The metadata args (id / brand / model /
    source / target) are passed through unchanged — they're not
    encoded in the file contents themselves and have to come from
    the file path / containing directory.
    """
    profile = AutoEqProfile(
        profile_id=profile_id,
        brand=brand,
        model=model,
        source=source,
        target=target,
    )
    for line_idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Preamp line — at most one per file, and AutoEQ always
        # emits it before the filter list. We don't enforce
        # ordering here; whatever shows up wins.
        m = _PREAMP_RE.match(line)
        if m is not None:
            profile.preamp_db = float(m.group(1))
            continue

        # Filter line.
        m = _FILTER_RE.match(line)
        if m is not None:
            if m.group("state").upper() == "OFF":
                # User-disabled band — preserve numbering by
                # skipping rather than raising.
                continue
            ftype = m.group("type").upper()
            if ftype not in _VALID_TYPES:
                raise AutoEqParseError(
                    f"line {line_idx}: unsupported filter type {ftype!r} "
                    f"(expected one of {sorted(_VALID_TYPES)})"
                )
            profile.bands.append(
                AutoEqBand(
                    filter_type=ftype,
                    freq_hz=float(m.group("fc")),
                    gain_db=float(m.group("gain")),
                    q=float(m.group("q")),
                )
            )
            continue

        # Anything else is unrecognised. Be strict — a typo'd line
        # would otherwise silently drop a band and the user would
        # hear the wrong correction.
        raise AutoEqParseError(
            f"line {line_idx}: unrecognised line {line!r}"
        )

    return profile
