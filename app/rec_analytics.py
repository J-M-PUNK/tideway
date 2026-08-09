"""Impression and outcome logging for the For You rows (#307).

Recommendation quality was previously judged by looking at the page and
forming an opinion. That is not a foundation for tuning: the per-source
score constants in `_album_recommendations` were picked by feel, and a
plausible-looking row can perform badly without anyone noticing.

This records what each row actually showed, then joins that against what
the listener did afterwards, so a row can be compared against another row
with a number instead of a vibe.

Two outcome signals, in descending order of trustworthiness:

* **liked** — exact. Favorited album ids, the same id space recorded here,
  and account-level so it holds across the listener's devices.
* **played** — approximate. There is no server-side record of "this album
  id was played": `/api/player/load` only receives a track id and
  `/api/now-playing` only carries a display name. The nearest durable
  source is the listener's Last.fm scrobbles, which name the artist and
  album as text, so plays are matched on a normalized (artist, album)
  pair. Reissues and deluxe editions that Tidal and Last.fm spell
  differently will miss, which biases the play rate *down*. Treat it as a
  floor and as a number to compare *between* rows (both rows are subject
  to the same bias), not as an exact per-album verdict. An exact signal
  needs the frontend to report the album id it played; see
  `docs/rec-analytics.md`.

Storage is one JSON file next to the other per-device state. This is
local behavioural data, deliberately never sent anywhere.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from typing import Optional

from app.paths import user_data_dir

# Cap on distinct albums retained. Each entry is small (a few hundred
# bytes), so this is well under a megabyte while covering months of
# browsing; the oldest-seen entries are pruned past it so the file can't
# grow without bound on a long-lived install.
_MAX_ALBUMS = 2000

_lock = threading.Lock()


def _file():
    return user_data_dir() / "rec_impressions.json"


# Trailing edition markers Tidal and Last.fm disagree on constantly
# ("Frailty" vs "Frailty (Deluxe Edition)"). Stripping them before the
# join recovers matches that would otherwise be counted as never played.
_EDITION_RE = re.compile(
    r"\s*[\(\[][^)\]]*\b("
    r"deluxe|expanded|remaster(ed)?|edition|version|anniversary|bonus|reissue"
    r")\b[^)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)


def _norm(s: Optional[str]) -> str:
    """Normalize a title or artist for the scrobble join."""
    s = (s or "").strip().lower()
    prev = None
    while prev != s:  # a title can carry two markers: "(Deluxe) (Remastered)"
        prev = s
        s = _EDITION_RE.sub("", s)
    # Collapse punctuation and whitespace so "Vol. 2" == "vol 2".
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _key(artist: Optional[str], album: Optional[str]) -> str:
    return f"{_norm(artist)}\x1f{_norm(album)}"


def _load() -> dict:
    try:
        with open(_file()) as f:
            data = json.load(f)
        albums = data.get("albums")
        return {"albums": albums if isinstance(albums, dict) else {}}
    except FileNotFoundError:
        return {"albums": {}}
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable: start fresh rather than losing the ability
        # to record. This is analytics, not user content.
        return {"albums": {}}


def _save(data: dict) -> None:
    target = _file()
    fd, tmp = tempfile.mkstemp(prefix=".rec_impressions.", suffix=".tmp",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_impressions(sections: list) -> None:
    """Record that these sections were served.

    Counts one impression per album per section per call. The endpoint
    caches its payload, so this counts *serves* rather than eyeballs —
    fine for a comparative rate, since every row is served together and
    therefore equally.
    """
    if not sections:
        return
    now = int(time.time())
    with _lock:
        data = _load()
        albums = data["albums"]
        for sec in sections:
            skey = str((sec or {}).get("key") or "") or "unknown"
            for a in (sec or {}).get("albums") or []:
                aid = str((a or {}).get("id") or "")
                if not aid:
                    continue
                entry = albums.get(aid)
                if entry is None:
                    entry = {
                        "name": a.get("name") or "",
                        "artist": ((a.get("artists") or [{}])[0] or {}).get("name", ""),
                        "sections": {},
                        "first_seen": now,
                        "last_seen": now,
                    }
                    albums[aid] = entry
                entry["last_seen"] = now
                entry["sections"][skey] = int(entry["sections"].get(skey, 0)) + 1
        if len(albums) > _MAX_ALBUMS:
            # Drop the least recently served entries.
            ordered = sorted(albums.items(), key=lambda kv: kv[1].get("last_seen", 0))
            for aid, _ in ordered[: len(albums) - _MAX_ALBUMS]:
                albums.pop(aid, None)
        _save(data)


def stats(played_keys: Optional[set] = None,
          liked_ids: Optional[set] = None) -> dict:
    """Per-section engagement.

    `played_keys` is a set of `_key(artist, album)` strings built by the
    caller from the listener's scrobbles; `liked_ids` is a set of Tidal
    album ids. Both are optional so the caller can report whatever signals
    it can actually obtain.
    """
    played_keys = played_keys or set()
    liked_ids = liked_ids or set()

    with _lock:
        albums = _load()["albums"]

    per: dict[str, dict] = {}
    for aid, entry in albums.items():
        hit = _key(entry.get("artist"), entry.get("name")) in played_keys
        for skey, count in (entry.get("sections") or {}).items():
            s = per.setdefault(skey, {
                "section": skey, "impressions": 0, "albums": 0,
                "played": 0, "liked": 0,
            })
            s["impressions"] += int(count or 0)
            s["albums"] += 1
            if hit:
                s["played"] += 1
            if aid in liked_ids:
                s["liked"] += 1

    out = []
    for s in per.values():
        n = s["albums"] or 1
        s["play_rate"] = round(s["played"] / n, 4)
        out.append(s)
    # Best-performing row first — the whole point is the comparison.
    out.sort(key=lambda s: (s["play_rate"], s["albums"]), reverse=True)
    return {"sections": out, "tracked_albums": len(albums)}


def played_keys_from_scrobbles(scrobbles: list) -> set:
    """Build the join key set from `lastfm.get_recent_tracks()` rows."""
    keys = set()
    for row in scrobbles or []:
        album = (row or {}).get("album") or ""
        artist = (row or {}).get("artist") or ""
        if album and artist:
            keys.add(_key(artist, album))
    return keys
