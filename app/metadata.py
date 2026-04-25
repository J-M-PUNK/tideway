import os
import shutil
import tempfile
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
_MAX_COVER_BYTES = 5 * 1024 * 1024  # 5 MB — larger than any real Tidal cover


def tag_file(file_path: Path, track, cover_data: Optional[bytes] = None):
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
            _tag_flac(tmp_path, track, cover_data)
        else:
            _tag_m4a(tmp_path, track, cover_data)
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


def fetch_cover_art(album_obj) -> Optional[bytes]:
    if album_obj is None:
        return None
    try:
        url = album_obj.image(640)
        parsed = urlparse(url)
        # Reject non-HTTPS or non-Tidal hosts before touching the network.
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_COVER_HOSTS:
            return None
        if parsed.username or parsed.password:
            return None
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
            return bytes(buf)
    except Exception:
        return None


def _tag_flac(path: Path, track, cover_data: Optional[bytes]):
    from mutagen.flac import FLAC, Picture

    audio = FLAC(str(path))
    audio["title"] = track.name
    audio["artist"] = _artist_names(track)
    audio["album"] = _safe(getattr(track, "album", None), "name") or ""
    audio["albumartist"] = _album_artist_name(track)
    audio["tracknumber"] = str(getattr(track, "track_num", 0))
    num_tracks = _safe(getattr(track, "album", None), "num_tracks")
    if num_tracks:
        audio["totaltracks"] = str(num_tracks)

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


def _tag_m4a(path: Path, track, cover_data: Optional[bytes]):
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(str(path))
    audio["\xa9nam"] = track.name
    audio["\xa9ART"] = _artist_names(track)
    audio["\xa9alb"] = _safe(getattr(track, "album", None), "name") or ""
    audio["aART"] = _album_artist_name(track)
    num_tracks = _safe(getattr(track, "album", None), "num_tracks") or 0
    audio["trkn"] = [(getattr(track, "track_num", 0), num_tracks)]

    track_id = getattr(track, "id", None)
    if track_id is not None:
        audio[M4A_TIDAL_ID_KEY] = [str(track_id).encode("utf-8")]

    if cover_data:
        audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()


def _artist_names(track) -> str:
    try:
        return ", ".join(a.name for a in track.artists)
    except Exception:
        pass
    try:
        return track.artist.name
    except Exception:
        return ""


def _album_artist_name(track) -> str:
    """The canonical album-level artist. We tag this so file managers
    and our own Local Library group every track on an album under one
    heading even when individual tracks credit guest artists (e.g.
    Thriller's "The Girl Is Mine" lists Paul McCartney alongside
    Michael Jackson). Falls back to the track's own primary artist
    when tidalapi doesn't expose an album.artist.
    """
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


def _safe(obj, attr: str):
    try:
        return getattr(obj, attr)
    except Exception:
        return None
