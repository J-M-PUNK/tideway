import os
import shutil
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from app.http import SESSION


TIDAL_ID_TAG = "TIDAL_TRACK_ID"
M4A_TIDAL_ID_KEY = "----:com.tidaldownloader:track_id"

# Only pull cover art from known Tidal CDN hosts. If tidalapi were ever
# to return a URL from elsewhere (compromise, DNS rebinding, a bug), we
# don't want the downloader process to start fetching arbitrary URLs and
# embedding the bytes into the user's files.
_ALLOWED_COVER_HOSTS = {"resources.tidal.com", "images.tidal.com"}
# 30 MB ceiling. "origin" covers are 3000x3000 and run a few MB; the cap
# is generous enough not to reject a real one but still guards against a
# misbehaving server streaming an unbounded payload into memory.
_MAX_COVER_BYTES = 30 * 1024 * 1024

# Cover-art resolutions tidalapi exposes, largest first. The download
# path requests the user's chosen size and falls back down this ladder
# when an album doesn't publish it (origin in particular is missing for
# some back-catalog releases). Values are exactly what
# `tidalapi.Album.image()` accepts.
_COVER_SIZE_LADDER: tuple = ("origin", 1280, 640, 320)
DEFAULT_COVER_RESOLUTION = "1280"

# Process-lifetime LRU of resolved cover bytes, keyed by
# (album id, resolution). tag_file embeds the same cover into every
# track of an album, so without this the full image is re-downloaded
# — and the fallback ladder re-walked — once per track: 20 redundant
# fetches for a 20-track album, far worse at "origin" (multi-MB each)
# or for a cover-less album that 404s the whole ladder every time.
# Bounded small: concurrent downloads are capped well below this, and
# an entry is only useful for the lifetime of one album's download
# burst. None is cached too, so a genuinely cover-less album isn't
# re-walked per track.
_cover_cache_lock = threading.Lock()
_cover_cache: "OrderedDict[tuple[str, str], Optional[bytes]]" = OrderedDict()
_COVER_CACHE_MAX = 8


def tag_file(
    file_path: Path,
    track,
    cover_data: Optional[bytes] = None,
    album_obj=None,
):
    """Tag a downloaded audio file atomically.

    mutagen's in-place save rewrites the file, which means a crash between
    open-for-write and finish leaves the audio corrupted or truncated. We
    work on a sibling temp copy and os.replace() it over the original so
    the user always has either the original untagged file or a fully
    tagged one — never a half-written hybrid.
    """
    ext = file_path.suffix.lower()
    if ext not in (".flac", ".m4a", ".mp4"):
        return
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.", suffix=".tag.tmp", dir=str(file_path.parent)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copy2(file_path, tmp_path)
        if ext == ".flac":
            _tag_flac(tmp_path, track, cover_data, album_obj)
        else:
            _tag_m4a(tmp_path, track, cover_data, album_obj)
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_track_id(file_path: Path) -> Optional[str]:
    """Read the Tidal track ID we wrote at download time, if any.

    Returns None for files we didn't tag (pre-existing files on disk, or
    anything not produced by this app).
    """
    ext = file_path.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC

            audio = FLAC(str(file_path))
            vals = audio.get(TIDAL_ID_TAG.lower())
            if vals:
                return str(vals[0])
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4

            audio = MP4(str(file_path))
            vals = audio.get(M4A_TIDAL_ID_KEY)
            if vals:
                raw = vals[0]
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", errors="ignore")
                return str(raw)
    except Exception:
        return None
    return None


def read_track_tags(file_path: Path) -> Optional[dict]:
    """Extract the tags we wrote at download time plus a few generic ones
    (title/artist/album/track_num/duration) for display in the Local
    Library view. Returns None for untagged or unreadable files — the
    caller skips them rather than listing "Unknown Artist" rows.
    """
    ext = file_path.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC

            audio = FLAC(str(file_path))
            tidal_id = audio.get(TIDAL_ID_TAG.lower())
            title = audio.get("title")
            artist = audio.get("artist")
            album = audio.get("album")
            album_artist = audio.get("albumartist")
            track_num = audio.get("tracknumber")
            return {
                "tidal_id": str(tidal_id[0]) if tidal_id else None,
                "title": str(title[0]) if title else None,
                "artist": str(artist[0]) if artist else None,
                "album": str(album[0]) if album else None,
                "album_artist": str(album_artist[0]) if album_artist else None,
                "track_num": int(str(track_num[0])) if track_num and str(track_num[0]).isdigit() else 0,
                "duration": int(getattr(audio.info, "length", 0) or 0),
            }
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4

            audio = MP4(str(file_path))
            tidal_raw = audio.get(M4A_TIDAL_ID_KEY)
            tidal_id: Optional[str] = None
            if tidal_raw:
                raw = tidal_raw[0]
                tidal_id = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            trkn = audio.get("trkn")
            track_num = 0
            if trkn and isinstance(trkn[0], tuple) and trkn[0]:
                track_num = int(trkn[0][0])
            return {
                "tidal_id": tidal_id,
                "title": (audio.get("\xa9nam") or [None])[0],
                "artist": (audio.get("\xa9ART") or [None])[0],
                "album": (audio.get("\xa9alb") or [None])[0],
                "album_artist": (audio.get("aART") or [None])[0],
                "track_num": track_num,
                "duration": int(getattr(audio.info, "length", 0) or 0),
            }
    except Exception:
        return None
    return None


def _cover_size_ladder(resolution) -> list:
    """The sizes to try, in order, for a requested resolution: the
    chosen size first, then every smaller fallback. An unknown
    resolution falls back to the default so a corrupt setting can't
    leave a download with no cover."""
    # `origin` is the largest size — try it then everything below.
    if resolution == "origin":
        return list(_COVER_SIZE_LADDER)
    try:
        target = int(resolution)
    except (TypeError, ValueError):
        target = int(DEFAULT_COVER_RESOLUTION)
    # Drop sizes larger than the target so we never request beyond
    # what the user asked for.
    return [s for s in _COVER_SIZE_LADDER if s != "origin" and s <= target]


def _fetch_cover_at(album_obj, size) -> Optional[bytes]:
    """Fetch one cover-art size. Returns None (not raises) when the
    size isn't published, the host check fails, or the byte cap is
    exceeded, so the caller can try the next size down."""
    try:
        # tidalapi raises ValueError for a size the album doesn't
        # publish; any other error (missing attr, malformed payload)
        # is just as good a reason to fall through to the next size.
        url = album_obj.image(size)
    except Exception:
        return None
    parsed = urlparse(url)
    # Reject non-HTTPS or non-Tidal hosts before touching the network.
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_COVER_HOSTS:
        return None
    if parsed.username or parsed.password:
        return None
    try:
        # Stream + cap size so a misbehaving server can't exhaust memory
        # by advertising (or actually sending) a giant image.
        with SESSION.get(url, timeout=10, stream=True, allow_redirects=False) as resp:
            if not resp.ok:
                return None
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared and declared > _MAX_COVER_BYTES:
                return None
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > _MAX_COVER_BYTES:
                    return None
            return bytes(buf) or None
    except Exception:
        return None


def fetch_cover_art(
    album_obj, resolution: str = DEFAULT_COVER_RESOLUTION
) -> Optional[bytes]:
    """Download album cover art at the chosen `resolution`, falling
    back to smaller sizes when the album doesn't publish it. Returns
    None when nothing is reachable. Results are cached per
    (album, resolution) so an album's tracks share one fetch — see
    `_cover_cache`."""
    if album_obj is None:
        return None

    album_id = str(getattr(album_obj, "id", "") or "")
    cache_key = (album_id, str(resolution)) if album_id else None
    if cache_key is not None:
        with _cover_cache_lock:
            if cache_key in _cover_cache:
                _cover_cache.move_to_end(cache_key)
                return _cover_cache[cache_key]

    data: Optional[bytes] = None
    for size in _cover_size_ladder(resolution):
        data = _fetch_cover_at(album_obj, size)
        if data is not None:
            break

    if cache_key is not None:
        with _cover_cache_lock:
            _cover_cache[cache_key] = data
            _cover_cache.move_to_end(cache_key)
            while len(_cover_cache) > _COVER_CACHE_MAX:
                _cover_cache.popitem(last=False)
    return data


def _tag_flac(path: Path, track, cover_data: Optional[bytes], album_obj=None):
    from mutagen.flac import FLAC, Picture

    audio = FLAC(str(path))
    audio["title"] = track.name
    audio["artist"] = _artist_names(track)
    audio["album"] = _safe(getattr(track, "album", None), "name") or ""
    audio["albumartist"] = _album_artist_name(track, album_obj)
    audio["tracknumber"] = str(getattr(track, "track_num", 0))
    num_tracks = _safe(getattr(track, "album", None), "num_tracks")
    if num_tracks:
        audio["totaltracks"] = str(num_tracks)
    release_date = _release_date_str(track, album_obj)
    if release_date:
        # Vorbis `DATE` is the field Mp3Tag / foobar2000 / Picard
        # derive their "Year" column from (issue #196).
        audio["date"] = release_date

    track_id = getattr(track, "id", None)
    if track_id is not None:
        audio[TIDAL_ID_TAG.lower()] = str(track_id)

    if cover_data:
        pic = Picture()
        pic.data = cover_data
        pic.type = 3  # cover front
        pic.mime = "image/jpeg"
        audio.add_picture(pic)

    audio.save()


def _tag_m4a(path: Path, track, cover_data: Optional[bytes], album_obj=None):
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(str(path))
    audio["\xa9nam"] = track.name
    audio["\xa9ART"] = _artist_names(track)
    audio["\xa9alb"] = _safe(getattr(track, "album", None), "name") or ""
    audio["aART"] = _album_artist_name(track, album_obj)
    num_tracks = _safe(getattr(track, "album", None), "num_tracks") or 0
    audio["trkn"] = [(getattr(track, "track_num", 0), num_tracks)]
    release_date = _release_date_str(track, album_obj)
    if release_date:
        # ©day is MP4's release-date atom — same "Year" column
        # source as FLAC's DATE (issue #196).
        audio["\xa9day"] = release_date

    track_id = getattr(track, "id", None)
    if track_id is not None:
        audio[M4A_TIDAL_ID_KEY] = [str(track_id).encode("utf-8")]

    if cover_data:
        audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()


def _artist_names(track) -> str:
    """The track-level ARTIST tag credit: MAIN-credit artists,
    comma-joined. FEATURED credits are excluded — Tidal already
    carries the featuring in the track title ("… (feat. Jack)"), and
    including them in the artist string makes strict-grouping players
    (iPods, iTunes, Rockbox) mint a phantom "John, Jack" artist for
    every featured track instead of filing it under John.

    True collaborations credit every artist as MAIN, so duets still
    list everyone. Credits without role information (older payloads,
    objects from other code paths) are kept — dropping a credit is
    worse than the grouping nit this fixes.
    """
    try:
        artists = [a for a in track.artists if getattr(a, "name", None)]
    except Exception:
        artists = []
    if artists:
        def _role_value(artist) -> Optional[str]:
            role = getattr(artist, "role", None)
            # tidalapi's Role enum (`role.value` is "MAIN"/"FEATURED")
            # or a raw string, depending on the payload's vintage.
            value = getattr(role, "value", role)
            return str(value).upper() if value is not None else None

        mains = [a for a in artists if _role_value(a) != "FEATURED"]
        # Defensive: a payload crediting ONLY featured artists still
        # needs an artist tag — better the old joined string than an
        # empty field.
        return ", ".join(a.name for a in (mains or artists))
    try:
        return track.artist.name
    except Exception:
        return ""


def _album_artist_name(track, album_obj=None) -> str:
    """The canonical album-level artist. We tag this so file managers
    and our own Local Library group every track on an album under one
    heading even when individual tracks credit guest artists (e.g.
    Thriller's "The Girl Is Mine" lists Paul McCartney alongside
    Michael Jackson). Falls back to the track's own primary artist
    when tidalapi doesn't expose an album.artist.

    `album_obj` is the single album object the downloader resolved for
    the whole release. Prefer it: the per-track `track.album` blob
    Tidal embeds is not consistent across an album — some tracks carry
    a "Various Artists"/TIDAL placeholder credit — so deriving the
    album artist per track splits one album into several in the
    library. The resolved album object is the same for every track of
    an album download, so its artist is the stable key.
    """
    try:
        if album_obj is not None:
            name = album_obj.artist.name
            if name:
                return name
    except Exception:
        pass
    try:
        name = track.album.artist.name
        if name:
            return name
    except Exception:
        pass
    try:
        return track.artists[0].name
    except Exception:
        pass
    try:
        return track.artist.name
    except Exception:
        return ""


def _release_date_str(track, album_obj=None) -> str:
    """The release date for the FLAC `DATE` / MP4 `©day` tag —
    `YYYY-MM-DD` when the full date is known, a bare `YYYY` when only
    the year is. Taggers (Mp3Tag, foobar2000, Picard) derive their
    "Year" column from these fields, which is what issue #196 asks
    for.

    Source preference mirrors the downloader's `_album_year` (used
    for the `{year}` filename token): the editorial `release_date`
    over `tidal_release_date` — back-catalog reissues should carry
    the year users expect, not the date Tidal first hosted the
    stream. And like `_album_artist_name`, the resolved `album_obj`
    is preferred over the per-track `track.album` blob, which isn't
    consistent across an album.
    """
    for source in (album_obj, getattr(track, "album", None)):
        if source is None:
            continue
        for attr in ("release_date", "tidal_release_date"):
            dt = _safe(source, attr)
            if dt is None:
                continue
            try:
                if getattr(dt, "year", None):
                    return f"{int(dt.year):04d}-{int(dt.month):02d}-{int(dt.day):02d}"
            except Exception:
                continue
        # tidalapi also exposes a plain `year` int on some album
        # payloads when the full date is missing.
        year = _safe(source, "year")
        try:
            if year:
                return str(int(year))
        except Exception:
            pass
    return ""


def _safe(obj, attr: str):
    try:
        return getattr(obj, attr)
    except Exception:
        return None
