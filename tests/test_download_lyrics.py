"""Tests for saving lyrics alongside a downloaded track.

Reported as "lyrics are not downloaded along with the songs": the
player had lyrics all along, via `GET /api/track/{id}/lyrics`, but the
download path never asked for them, so a downloaded library lost
something the streaming view was showing.

Tidal hands back two independent things under one call. `text` is the
plain lyric and goes into the file's own tags, where every tagger can
read it but nothing can time it. `subtitles` is the same words with
[mm:ss.xx] cues, and goes out as a sidecar .lrc because that is the
only thing players with scrolling lyrics look for.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.metadata import Lyrics, fetch_lyrics, write_lrc_sidecar


class _RawLyrics:
    """Shaped like tidalapi's Lyrics object."""

    def __init__(self, text=None, subtitles=None) -> None:
        self.text = text
        self.subtitles = subtitles


class _Track:
    def __init__(self, raw) -> None:
        self._raw = raw

    def lyrics(self):
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw


class _HttpError(Exception):
    """Shaped like the curl_cffi HTTPError our HTTP layer actually
    raises: the status lives on an attached response, not in the
    message. tidalapi's own ObjectNotFound mapping never runs because
    curl_cffi replaces its request session."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP Error {status}: ")
        self.response = type("_R", (), {"status_code": status})()


class _MetadataNotAvailable(Exception):
    """Stands in for tidalapi.exceptions.MetadataNotAvailable, which
    is what Track.lyrics() raises when the mapping does run."""


_LRC = "[00:12.34]First line\n[00:15.00]Second line"


# ---------------------------------------------------------------------------
# fetch_lyrics
# ---------------------------------------------------------------------------


def test_reads_both_forms() -> None:
    got = fetch_lyrics(_Track(_RawLyrics(text="Plain words", subtitles=_LRC)))
    assert got == Lyrics(text="Plain words", subtitles=_LRC)


def test_reads_plain_text_when_there_are_no_cues() -> None:
    got = fetch_lyrics(_Track(_RawLyrics(text="Plain words")))
    assert got is not None
    assert got.text == "Plain words"
    assert got.subtitles == ""


def test_a_track_with_no_lyrics_is_not_an_error() -> None:
    """Most of the catalogue has none. Tidal answers 404, and that has
    to stay an ordinary outcome or every instrumental would surface as
    a tagging warning."""
    assert fetch_lyrics(_Track(_HttpError(404))) is None


def test_tidalapis_own_no_lyrics_error_is_also_ordinary() -> None:
    _MetadataNotAvailable.__name__ = "MetadataNotAvailable"
    assert fetch_lyrics(_Track(_MetadataNotAvailable("no lyrics"))) is None


def test_a_rate_limit_is_raised_not_swallowed() -> None:
    """A 429 has to reach the downloader so it can feed the shared
    backoff. Collapsing it to None would look identical to an
    instrumental and leave sibling workers hammering."""
    with pytest.raises(Exception) as caught:
        fetch_lyrics(_Track(_HttpError(429)))
    assert caught.value.response.status_code == 429


def test_a_transport_failure_is_raised_not_swallowed() -> None:
    """The audio file is already written and skip-existing will
    decline to redo it, so a dropped connection that silently yields
    no lyrics would never get a second attempt."""
    with pytest.raises(ConnectionError):
        fetch_lyrics(_Track(ConnectionError("connection reset")))


def test_an_auth_failure_is_raised_not_swallowed() -> None:
    with pytest.raises(Exception) as caught:
        fetch_lyrics(_Track(_HttpError(401)))
    assert caught.value.response.status_code == 401


def test_empty_strings_count_as_no_lyrics() -> None:
    assert fetch_lyrics(_Track(_RawLyrics(text="", subtitles=""))) is None
    assert fetch_lyrics(_Track(_RawLyrics(text="   "))) is None


def test_a_null_response_is_no_lyrics() -> None:
    assert fetch_lyrics(_Track(None)) is None


def test_surrounding_whitespace_is_stripped() -> None:
    got = fetch_lyrics(_Track(_RawLyrics(text="\n Words \n")))
    assert got is not None and got.text == "Words"


# ---------------------------------------------------------------------------
# write_lrc_sidecar
# ---------------------------------------------------------------------------


def test_timed_lyrics_land_in_a_matching_lrc(tmp_path: Path) -> None:
    audio = tmp_path / "01 - Song.flac"
    audio.touch()

    written = write_lrc_sidecar(audio, Lyrics(text="Plain", subtitles=_LRC))

    assert written == tmp_path / "01 - Song.lrc"
    assert written.read_text(encoding="utf-8") == _LRC + "\n"


def test_no_sidecar_without_cues(tmp_path: Path) -> None:
    """Plain text alone belongs in the tags. A .lrc with no timestamps
    is not something a synced-lyrics player can use."""
    audio = tmp_path / "Song.flac"
    audio.touch()

    assert write_lrc_sidecar(audio, Lyrics(text="Plain")) is None
    assert list(tmp_path.glob("*.lrc")) == []


def test_no_sidecar_when_the_track_has_no_lyrics(tmp_path: Path) -> None:
    audio = tmp_path / "Song.flac"
    audio.touch()

    assert write_lrc_sidecar(audio, None) is None
    assert list(tmp_path.glob("*.lrc")) == []


def test_an_existing_lrc_is_left_alone(tmp_path: Path) -> None:
    """The user may have fetched or hand-corrected a better one.
    Silently overwriting it on a re-download is data loss they would
    have no reason to look for."""
    audio = tmp_path / "Song.flac"
    audio.touch()
    existing = tmp_path / "Song.lrc"
    existing.write_text("[00:01.00]Mine, corrected", encoding="utf-8")

    assert write_lrc_sidecar(audio, Lyrics(subtitles=_LRC)) is None
    assert existing.read_text(encoding="utf-8") == "[00:01.00]Mine, corrected"


def test_sidecar_replaces_the_audio_extension(tmp_path: Path) -> None:
    """Players pair `Song.lrc` with `Song.m4a`, not `Song.m4a.lrc`."""
    audio = tmp_path / "Song.m4a"
    audio.touch()

    written = write_lrc_sidecar(audio, Lyrics(subtitles=_LRC))
    assert written is not None and written.name == "Song.lrc"


def test_a_dotted_track_name_keeps_its_stem(tmp_path: Path) -> None:
    audio = tmp_path / "Mr. Brightside.flac"
    audio.touch()

    written = write_lrc_sidecar(audio, Lyrics(subtitles=_LRC))
    assert written is not None and written.name == "Mr. Brightside.lrc"


# ---------------------------------------------------------------------------
# Tag writing
# ---------------------------------------------------------------------------


class _Album:
    id = 1
    name = "An Album"
    num_tracks = 10
    artist = type("_A", (), {"name": "An Artist"})()
    artists = [artist]
    release_date = None
    available_release_date = None


class _TaggableTrack:
    id = 42
    name = "A Song"
    track_num = 1
    album = _Album()
    artist = _Album.artist
    artists = [_Album.artist]


def _flac_fixture(tmp_path: Path) -> Path:
    """A real, minimal FLAC. mutagen refuses to open anything else, so
    there's no way to test tag writing without one.

    Built with PyAV rather than a shelled-out ffmpeg: PyAV is already a
    hard dependency (it's the decoder the player runs on) and its
    wheels bundle libav, so this works on a checkout with nothing but
    `pip install -r requirements.txt`.
    """
    pytest.importorskip("mutagen")
    av = pytest.importorskip("av")

    src = tmp_path / "silence.flac"
    with av.open(str(src), mode="w", format="flac") as container:
        stream = container.add_stream("flac", rate=44100)
        stream.layout = "stereo"
        frame = av.AudioFrame(format="s16", layout="stereo", samples=4410)
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.rate = 44100
        frame.pts = 0
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return src


def test_flac_gets_both_vorbis_lyric_fields(tmp_path: Path) -> None:
    """Vorbis never settled on one field name. foobar2000 and Picard
    read UNSYNCEDLYRICS, VLC and Kodi read LYRICS, so both get written
    or half the user's players show nothing."""
    from mutagen.flac import FLAC

    from app.metadata import tag_file

    path = _flac_fixture(tmp_path)
    tag_file(path, _TaggableTrack(), None, lyrics=Lyrics(text="Plain words"))

    tags = FLAC(str(path))
    assert tags["lyrics"] == ["Plain words"]
    assert tags["unsyncedlyrics"] == ["Plain words"]


def test_flac_without_lyrics_gets_no_lyric_field(tmp_path: Path) -> None:
    from mutagen.flac import FLAC

    from app.metadata import tag_file

    path = _flac_fixture(tmp_path)
    tag_file(path, _TaggableTrack(), None, lyrics=None)

    tags = FLAC(str(path))
    assert "lyrics" not in tags
    assert "unsyncedlyrics" not in tags


def test_timed_only_lyrics_do_not_become_a_tag(tmp_path: Path) -> None:
    """A tag holds no timing, so writing the raw LRC into one would
    show the user a wall of [00:12.34] markers."""
    from mutagen.flac import FLAC

    from app.metadata import tag_file

    path = _flac_fixture(tmp_path)
    tag_file(path, _TaggableTrack(), None, lyrics=Lyrics(subtitles=_LRC))

    assert "lyrics" not in FLAC(str(path))
