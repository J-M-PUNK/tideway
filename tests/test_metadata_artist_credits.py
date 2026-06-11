"""ARTIST tag credit filtering (the "phantom 'John, Jack' artist" bug).

Joining every credited artist into the ARTIST tag makes
strict-grouping players (iPods, iTunes, Rockbox) create a separate
artist entry per featuring combination. The tag must carry only the
MAIN credits; FEATURED artists already live in the track title's
"(feat. …)". True collaborations credit everyone as MAIN and keep the
joined form.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.metadata import _artist_names, tag_file


def _artist(name: str, role=None):
    a = SimpleNamespace(name=name)
    if role is not None:
        a.role = role
    return a


def _track(artists):
    return SimpleNamespace(
        id=1,
        name="Song",
        track_num=1,
        artists=artists,
        album=SimpleNamespace(name="Album", num_tracks=10),
    )


class _EnumLikeRole:
    """Mimics tidalapi's Role enum member (has a .value)."""

    def __init__(self, value: str):
        self.value = value


def test_featured_artist_excluded_from_artist_tag():
    track = _track([_artist("John", "MAIN"), _artist("Jack", "FEATURED")])
    assert _artist_names(track) == "John"


def test_enum_style_roles_are_understood():
    track = _track(
        [
            _artist("John", _EnumLikeRole("MAIN")),
            _artist("Jack", _EnumLikeRole("FEATURED")),
        ]
    )
    assert _artist_names(track) == "John"


def test_true_collaboration_keeps_every_main_artist():
    track = _track([_artist("John", "MAIN"), _artist("Jack", "MAIN")])
    assert _artist_names(track) == "John, Jack"


def test_credits_without_roles_are_kept():
    # Older payloads / other code paths don't attach roles — dropping
    # a credit would be worse than the grouping nit.
    track = _track([_artist("John"), _artist("Jack")])
    assert _artist_names(track) == "John, Jack"


def test_all_featured_falls_back_to_full_credit():
    # Defensive: a payload crediting ONLY featured artists still needs
    # an artist tag.
    track = _track([_artist("Jack", "FEATURED")])
    assert _artist_names(track) == "Jack"


def test_flac_artist_tag_round_trip(tmp_path: Path):
    """End-to-end through the production tag path on a real FLAC."""
    import av
    from mutagen.flac import FLAC

    f = tmp_path / "01 - Song.flac"
    with av.open(str(f), "w", format="flac") as container:
        stream = container.add_stream("flac", rate=44100)
        frame = av.AudioFrame.from_ndarray(
            np.zeros((2, 1024), dtype=np.int16), format="s16p", layout="stereo"
        )
        frame.sample_rate = 44100
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    track = _track([_artist("John", "MAIN"), _artist("Jack", "FEATURED")])
    tag_file(f, track, cover_data=None, album_obj=None)

    audio = FLAC(str(f))
    assert audio["artist"] == ["John"]
