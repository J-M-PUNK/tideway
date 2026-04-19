from pathlib import Path
from typing import Optional

from app.http import SESSION


TIDAL_ID_TAG = "TIDAL_TRACK_ID"
M4A_TIDAL_ID_KEY = "----:com.tidaldownloader:track_id"


def tag_file(file_path: Path, track, cover_data: Optional[bytes] = None):
    ext = file_path.suffix.lower()
    if ext == ".flac":
        _tag_flac(file_path, track, cover_data)
    elif ext in (".m4a", ".mp4"):
        _tag_m4a(file_path, track, cover_data)


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


def fetch_cover_art(album_obj) -> Optional[bytes]:
    if album_obj is None:
        return None
    try:
        url = album_obj.image(640)
        resp = SESSION.get(url, timeout=10)
        if resp.ok:
            return resp.content
    except Exception:
        pass
    return None


def _tag_flac(path: Path, track, cover_data: Optional[bytes]):
    from mutagen.flac import FLAC, Picture

    audio = FLAC(str(path))
    audio["title"] = track.name
    audio["artist"] = _artist_names(track)
    audio["album"] = _safe(getattr(track, "album", None), "name")
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


def _safe(obj, attr: str):
    try:
        return getattr(obj, attr)
    except Exception:
        return None
