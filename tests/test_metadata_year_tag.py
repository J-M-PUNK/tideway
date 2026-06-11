"""Release-date tagging (issue #196).

Downloaded files carried title/artist/album/track tags but no
DATE / ©day, so taggers like Mp3Tag showed an empty "Year" column.
These tests pin the new behavior: a real FLAC tagged through the
production `tag_file` path round-trips a `date` Vorbis comment, and
`_release_date_str` prefers the editorial date with sensible
fallbacks.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.metadata import _release_date_str, tag_file


def _make_flac(path: Path) -> None:
    """Write a tiny real FLAC (a few ms of silence) so mutagen has an
    actual STREAMINFO block to work with. PyAV is already a hard
    dependency of the audio engine."""
    import av

    with av.open(str(path), "w", format="flac") as container:
        stream = container.add_stream("flac", rate=44100)
        frame = av.AudioFrame.from_ndarray(
            np.zeros((2, 1024), dtype=np.int16), format="s16p", layout="stereo"
        )
        frame.sample_rate = 44100
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def _track(album=None):
    return SimpleNamespace(
        id=12345,
        name="Billie Jean",
        track_num=6,
        artists=[SimpleNamespace(name="Michael Jackson")],
        album=album
        if album is not None
        else SimpleNamespace(
            name="Thriller",
            num_tracks=9,
            artist=SimpleNamespace(name="Michael Jackson"),
        ),
    )


def _album(**over):
    base = {
        "name": "Thriller",
        "num_tracks": 9,
        "artist": SimpleNamespace(name="Michael Jackson"),
        "release_date": datetime.datetime(1982, 11, 30),
    }
    base.update(over)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# End-to-end: real FLAC through the production tag path
# ---------------------------------------------------------------------------


def test_flac_gets_date_tag(tmp_path: Path) -> None:
    from mutagen.flac import FLAC

    f = tmp_path / "06 - Billie Jean.flac"
    _make_flac(f)

    tag_file(f, _track(), cover_data=None, album_obj=_album())

    audio = FLAC(str(f))
    assert audio["date"] == ["1982-11-30"]
    # The pre-existing tags still land — date is additive.
    assert audio["title"] == ["Billie Jean"]
    assert audio["tracknumber"] == ["6"]


def test_flac_without_any_date_writes_no_date_tag(tmp_path: Path) -> None:
    from mutagen.flac import FLAC

    f = tmp_path / "no-date.flac"
    _make_flac(f)

    bare_album = _album(release_date=None)
    tag_file(f, _track(album=bare_album), cover_data=None, album_obj=bare_album)

    audio = FLAC(str(f))
    # No fabricated date — an absent field beats a wrong one.
    assert "date" not in audio


# ---------------------------------------------------------------------------
# Source preference
# ---------------------------------------------------------------------------


def test_editorial_release_date_beats_tidal_hosting_date() -> None:
    album = _album(
        release_date=datetime.datetime(1982, 11, 30),
        tidal_release_date=datetime.datetime(2012, 1, 15),
    )
    assert _release_date_str(_track(), album) == "1982-11-30"


def test_tidal_release_date_is_the_fallback() -> None:
    album = _album(release_date=None)
    album.tidal_release_date = datetime.datetime(2012, 1, 15)
    assert _release_date_str(_track(), album) == "2012-01-15"


def test_bare_year_used_when_no_full_date() -> None:
    album = _album(release_date=None)
    album.year = 1982
    assert _release_date_str(_track(), album) == "1982"


def test_track_album_blob_used_when_no_resolved_album() -> None:
    # Lone-track downloads may not resolve a full album object; the
    # per-track album blob's date is better than nothing.
    blob = SimpleNamespace(
        name="Thriller",
        num_tracks=9,
        release_date=datetime.datetime(1982, 11, 30),
    )
    assert _release_date_str(_track(album=blob), None) == "1982-11-30"


def test_no_date_anywhere_is_empty() -> None:
    blob = SimpleNamespace(name="Thriller", num_tracks=9)
    assert _release_date_str(_track(album=blob), None) == ""


@pytest.mark.parametrize("junk", ["not-a-date", 0, ""])
def test_junk_year_values_are_skipped(junk) -> None:
    album = _album(release_date=None)
    album.year = junk
    assert _release_date_str(_track(), album) == ""
