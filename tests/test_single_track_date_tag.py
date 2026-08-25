"""Single-track downloads write a DATE tag (issue #357).

Downloading a track on its own wrote no `DATE`, while the same track
pulled as part of its album did. The cause is that tidalapi populates
`track.album` thinly — `release_date` and `available_release_date` are
both None on it — and the single-track path had nothing else to hand
to the tagger. An album download resolves the real album once and
passes it down, which is why only one of the two paths looked right.

Users notice because Mp3Tag, foobar2000 and Picard all derive their
"Year" column from Vorbis DATE, so a single-track download lands in
the library with a blank year next to album downloads that have one.

`Downloader._album_with_details` closes the gap by fetching the full
album when what it has carries no date. These tests pin that it fires
on the thin object, stays out of the way on the album path, and never
turns a failed lookup into a failed download.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.downloader import Downloader
from app.metadata import _album_attr, tag_file


def _thin_album():
    """Shaped like tidalapi's `track.album`: a name and a track count,
    no dates."""
    return SimpleNamespace(
        id=77, name="Pablo Honey", num_tracks=12, release_date=None
    )


def _full_album():
    """What `session.album(id)` returns."""
    return SimpleNamespace(
        id=77,
        name="Pablo Honey",
        num_tracks=12,
        release_date=datetime.date(1993, 4, 20),
        artist=SimpleNamespace(name="Radiohead"),
    )


class _Session:
    def __init__(self, result):
        self._result = result
        self.calls: list = []

    def album(self, album_id):
        self.calls.append(album_id)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _downloader(session):
    """Only the attributes `_album_with_details` touches. Building a
    real Downloader would drag in settings, callbacks and worker
    threads for a method that needs a session and a backoff hook."""
    return SimpleNamespace(
        tidal=SimpleNamespace(session=session),
        _note_rate_limit=lambda exc, attempt: None,
    )


def test_thin_album_is_resolved_to_the_full_one():
    session = _Session(_full_album())
    got = Downloader._album_with_details(_downloader(session), _thin_album())

    assert session.calls == [77], "should have fetched the full album"
    assert got.release_date == datetime.date(1993, 4, 20)


def test_album_path_does_not_pay_for_the_extra_fetch():
    """An album download already holds the resolved object. It must not
    spend a request per track re-fetching what it has."""
    session = _Session(_full_album())
    got = Downloader._album_with_details(_downloader(session), _full_album())

    assert session.calls == [], "album path made a redundant request"
    assert got.release_date == datetime.date(1993, 4, 20)


def test_failed_lookup_degrades_instead_of_failing_the_download():
    """The audio is already on disk by this point. A missing DATE is
    worse than before; a raised exception would be a lost download."""
    session = _Session(RuntimeError("tidal is having a day"))
    thin = _thin_album()
    got = Downloader._album_with_details(_downloader(session), thin)

    assert got is thin


def test_rate_limit_on_lookup_reaches_the_shared_backoff():
    """A 429 here has to be recorded, or sibling workers keep hammering
    a bucket that just said stop."""
    noted: list = []
    dl = _downloader(_Session(RuntimeError("429 Too Many Requests")))
    dl._note_rate_limit = lambda exc, attempt: noted.append(exc)

    Downloader._album_with_details(dl, _thin_album())

    assert len(noted) == 1


def test_missing_album_id_is_left_alone():
    session = _Session(_full_album())
    thin = SimpleNamespace(name="Pablo Honey", release_date=None)
    got = Downloader._album_with_details(_downloader(session), thin)

    assert session.calls == []
    assert got is thin


# --- the second half of #357: tags read straight off the thin blob ---


def test_album_attr_prefers_the_resolved_album():
    """`num_tracks` and the album name were read from `track.album`
    directly, ignoring the resolved album the other tags prefer."""
    track = SimpleNamespace(
        album=SimpleNamespace(name="Pablo Honey (Promo)", num_tracks=None)
    )
    full = SimpleNamespace(name="Pablo Honey", num_tracks=12)

    assert _album_attr(track, full, "name") == "Pablo Honey"
    assert _album_attr(track, full, "num_tracks") == 12


def test_album_attr_falls_back_to_the_track_blob():
    track = SimpleNamespace(album=SimpleNamespace(name="Pablo Honey", num_tracks=12))

    assert _album_attr(track, None, "name") == "Pablo Honey"
    assert _album_attr(track, None, "num_tracks") == 12


def _make_flac(path: Path) -> None:
    """A few ms of real silence so mutagen has a STREAMINFO block.
    Same approach as tests/test_metadata_year_tag.py."""
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


def test_resolved_album_reaches_the_file(tmp_path):
    """End to end through the production tagger: with the resolved
    album in hand the file carries both the date and the track count."""
    from mutagen.flac import FLAC

    path = tmp_path / "Creep.flac"
    _make_flac(path)
    track = SimpleNamespace(
        id=3135556,
        name="Creep",
        track_num=2,
        artists=[SimpleNamespace(name="Radiohead")],
        album=_thin_album(),
    )

    tag_file(path, track, None, album_obj=_full_album())

    audio = FLAC(str(path))
    assert audio["date"] == ["1993-04-20"]
    assert audio["totaltracks"] == ["12"]
    assert audio["album"] == ["Pablo Honey"]
