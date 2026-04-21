"""Text-based playlist import (M3U, M3U8, plain text).

Parses whatever the user pastes in / uploads into a list of
(artist, title, duration) tuples we can feed through the same Tidal
matcher the Spotify importer uses. Handles:

  - `#EXTINF:<secs>,<artist> - <title>` headers from Extended M3U
  - Bare filepaths (`/Users/.../Artist/Album/03 - Title.flac`) —
    filename-derived matching. Best-effort; filenames that don't
    look like "<track_num> - <title>" or "<artist> - <title>" just
    pass through as "title only" and the matcher does its best.
  - Plain text — one `Artist - Title` per line, # comments ignored.

Zero state — input in, rows out. The `match_each` helper wires each
row through `spotify_import.match_track()` since the matcher's
contract (dict with name/artists/duration_ms/isrc) is intentionally
generic, not Spotify-specific.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app import spotify_import

# "#EXTINF:182,The Beatles - Something" → (182, "The Beatles", "Something").
# The duration is optional (some exporters stamp -1 for unknown).
_EXTINF_RE = re.compile(
    r"^#EXTINF\s*:\s*(-?\d+)?\s*,\s*(?P<rest>.+?)\s*$",
    re.IGNORECASE,
)

# Filename-to-track-guess patterns, tried in order:
#   "01 - Artist - Title.flac"      (track number + artist + title)
#   "01. Title.flac"                 (track number + title only)
#   "Artist - Title.flac"            (artist + title)
# Anything else falls through as a plain title.
_FN_TRACK_ARTIST_TITLE = re.compile(
    r"^\s*(?:\d+\s*[-.]\s*)?(?P<artist>.+?)\s+-\s+(?P<title>.+?)\s*$"
)
_FN_TRACK_TITLE_ONLY = re.compile(
    r"^\s*\d+\s*[-.]\s*(?P<title>.+?)\s*$"
)


@dataclass
class ParsedTrack:
    name: str
    artists: list[str]
    duration_ms: int  # 0 when unknown
    isrc: Optional[str] = None  # always None here — no ISRC info in M3U

    def as_match_dict(self) -> dict:
        return {
            "name": self.name,
            "artists": self.artists,
            "duration_ms": self.duration_ms,
            "isrc": self.isrc,
        }


def parse(text: str) -> list[ParsedTrack]:
    """Turn an M3U / plain-text blob into a list of ParsedTrack rows.
    Empty input → empty list; never raises."""
    out: list[ParsedTrack] = []
    lines = (text or "").splitlines()
    pending_ext: Optional[tuple[int, str, str]] = None
    for raw in lines:
        line = raw.strip()
        if not line:
            pending_ext = None
            continue
        if line.startswith("#"):
            m = _EXTINF_RE.match(line)
            if m:
                secs_raw, rest = m.group(1), m.group("rest")
                secs = int(secs_raw) if secs_raw and secs_raw.lstrip("-").isdigit() else 0
                if secs < 0:
                    secs = 0
                artist, title = _split_artist_title(rest)
                pending_ext = (secs * 1000, artist, title)
            # other #-comments ignored
            continue

        # Non-comment, non-blank line — either a path/URL that
        # consumes the pending EXTINF, or a standalone "Artist - Title".
        if pending_ext is not None:
            ms, artist, title = pending_ext
            pending_ext = None
            out.append(
                ParsedTrack(
                    name=title,
                    artists=[artist] if artist else [],
                    duration_ms=ms,
                )
            )
            continue

        # No EXTINF header — derive from the line itself. If it looks
        # like a path, strip to the filename first.
        candidate = line
        if _looks_like_path(candidate):
            candidate = os.path.splitext(os.path.basename(candidate))[0]
        artist, title = _split_artist_title(candidate)
        if not title:
            continue
        out.append(
            ParsedTrack(
                name=title,
                artists=[artist] if artist else [],
                duration_ms=0,
            )
        )
    return out


def _split_artist_title(s: str) -> tuple[str, str]:
    """Try the "<artist> - <title>" heuristic first; fall back to
    title-only when there's no hyphen or the filename follows the
    "01. Title" pattern."""
    m = _FN_TRACK_ARTIST_TITLE.match(s)
    if m:
        return (m.group("artist").strip(), m.group("title").strip())
    m = _FN_TRACK_TITLE_ONLY.match(s)
    if m:
        return ("", m.group("title").strip())
    return ("", s.strip())


def _looks_like_path(s: str) -> bool:
    # Treat as a path if it starts with a slash, a Windows drive
    # letter, or contains a path separator + a file extension.
    if s.startswith("/") or s.startswith("\\"):
        return True
    if len(s) >= 3 and s[1] == ":" and (s[2] in ("/", "\\")):
        return True
    if ("/" in s or "\\" in s) and "." in s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
        return True
    return False


def match_each(session, rows: Iterable[ParsedTrack]) -> list[dict]:
    """Run every parsed row through the Tidal matcher and return the
    same {spotify, match} shape the ImportPage already renders —
    spotify-as-source-label is a misnomer here; the key stuck."""
    out: list[dict] = []
    for row in rows:
        raw = row.as_match_dict()
        match = spotify_import.match_track(session, raw)
        out.append(
            {
                "spotify": {
                    "name": row.name,
                    "artists": row.artists,
                    "duration_ms": row.duration_ms,
                    "isrc": None,
                },
                "match": match,
            }
        )
    return out
