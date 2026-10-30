"""Build the cloud queue used by TIDAL Connect receivers.

The receiver resolves the track itself; a DASH manifest's first URL is
not a complete stream. Keep credentials in the WSS payload, never logs.
"""
from __future__ import annotations

import json
from typing import Any

QUEUE_URL = "https://connectqueue.tidal.com/v1/queues"
CONTENT_URL = "https://api.tidal.com/v1"
AUTH_URL = "https://auth.tidal.com/v1/oauth2/token"


def create_track_queue(session: Any, track_id: int, quality: str) -> dict:
    """Create an ephemeral playback queue, using the session's gated transport."""
    track = session.track(int(track_id))
    body = {
        "items": [{
            "media_id": str(track_id),
            "type": "track",
            "properties": {
                "active": "false", "original_order": "0",
                "source_id": str(track_id), "source_type": "TRACK",
            },
        }],
        "properties": {"position": 0},
        "repeat_mode": "off",
        "shuffled": False,
    }
    response = session.request.basic_request(
        "POST", QUEUE_URL,
        params={"locale": session.locale, "deviceType": "DESKTOP"},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps(body),
    )
    if not response.ok:
        raise RuntimeError(f"Tidal Connect queue creation failed (HTTP {response.status_code})")
    try:
        queue = response.json()
        queue_id = queue["id"]
        item_id = queue["items"][0]["id"]
        if not isinstance(queue_id, str) or not queue_id or not isinstance(item_id, str) or not item_id:
            raise ValueError("empty queue identifiers")
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("Tidal Connect queue response is incomplete") from None

    # Read credentials after the request: basic_request may have refreshed them.
    token = session.access_token
    if not token:
        raise RuntimeError("Tidal Connect requires a logged-in session")

    def server_info(url: str, content: bool = False) -> dict:
        return {
            "serverUrl": url,
            "authInfo": {"oauthServerInfo": {
                "authInfo": {
                    "headerAuth": f"Bearer {token}",
                    "oauthParameters": {
                        "accessToken": token,
                        "refreshToken": session.refresh_token or "",
                    },
                },
                "formParameters": {"grant_type": "switch_client", "scope": "r_usr"},
                "httpHeaderFields": [],
                "serverUrl": AUTH_URL,
            }},
            "httpHeaderFields": [],
            "queryParameters": {"audiomode": "STEREO", "audioquality": quality} if content else {},
        }

    album = getattr(track, "album", None)
    cover = getattr(album, "cover", None)
    images = {}
    if cover:
        base = f"https://resources.tidal.com/images/{str(cover).replace('-', '/')}"
        images = {
            name: {"url": f"{base}/{size}x{size}.jpg", "width": size, "height": size}
            for name, size in (("high", 1280), ("medium", 640), ("low", 320))
        }
    return {
        "autoplay": True,
        "position": 0,
        "queueInfo": {
            "queueId": queue_id, "repeatMode": "OFF", "shuffled": False,
            "maxBeforeSize": 10, "maxAfterSize": 10,
        },
        "currentMediaInfo": {
            "itemId": item_id, "mediaId": str(track_id), "mediaType": 0,
            "metadata": {
                "title": str(getattr(track, "name", "") or ""),
                "albumTitle": str(getattr(album, "name", "") or ""),
                "artists": [str(getattr(a, "name", "")) for a in (getattr(track, "artists", None) or [])],
                "duration": int(float(getattr(track, "duration", 0) or 0) * 1000),
                "images": images,
            },
        },
        "contentServerInfo": server_info(CONTENT_URL, True),
        "queueServerInfo": server_info(QUEUE_URL),
    }
