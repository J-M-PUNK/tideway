"""Build the cloud queue used by TIDAL Connect receivers.

The receiver resolves tracks and advances its own queue so it can buffer
across track boundaries. Queue operations match the official desktop
client: create/upload in batches of 100, preserve opaque item IDs, and
use ETags for edits before asking the receiver to refresh. These endpoints
are an observed protocol, not a published SDK contract.

A DASH manifest's first URL is not a complete stream. Keep credentials
in the WSS payload, never logs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable
from collections import defaultdict, deque

QUEUE_URL = "https://connectqueue.tidal.com/v1/queues"
CONTENT_URL = "https://api.tidal.com/v1"
AUTH_URL = "https://auth.tidal.com/v1/oauth2/token"


@dataclass(frozen=True)
class CloudQueue:
    # Authentication belongs only on the encrypted receiver connection.
    payload: dict = field(repr=False)
    item_ids: tuple[str, ...]
    track_ids: tuple[int, ...]
    start_index: int
    etag: str = ""


@dataclass(frozen=True)
class QueueContext:
    """Frontend queue positions are distinct even for repeated track IDs."""
    track_ids: tuple[int, ...]
    order: tuple[int, ...]
    index: int
    repeat: str
    token: str
    quality: str = "hi_res_lossless"
    stop_after_current: bool = False

    def __post_init__(self):
        if (not self.track_ids or any(t <= 0 for t in self.track_ids)
                or sorted(self.order) != list(range(len(self.track_ids)))
                or not 0 <= self.index < len(self.track_ids)
                or self.repeat not in ("off", "all", "one") or not self.token):
            raise ValueError("Invalid Tidal Connect queue context")

    @property
    def ordered_ids(self) -> list[int]:
        order = self.order[:self.order.index(self.index) + 1] if self.stop_after_current else self.order
        return [self.track_ids[i] for i in order]


@dataclass
class QueueProvider:
    """Server-owned access to the authenticated Tidal session."""
    create: Callable[[QueueContext, bool, int], CloudQueue]
    update: Callable[[CloudQueue, list[int], int, str], CloudQueue]
    media_info: Callable[[int, str], dict]


def _raw_items(ids, offset=0):
    return [{
        "media_id": str(tid), "type": "track",
        "properties": {
            "active": "false", "original_order": str(offset + i),
            "source_id": str(tid), "source_type": "TRACK",
        },
    } for i, tid in enumerate(ids)]


def _request(session, method, url, body=None, etag=""):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if etag:
        headers["If-Match"] = etag
    response = session.request.basic_request(
        method, url, params={"locale": session.locale, "deviceType": "DESKTOP"},
        headers=headers, data=json.dumps(body) if body is not None else None,
    )
    if not response.ok:
        raise RuntimeError(f"Tidal Connect queue operation failed (HTTP {response.status_code})")
    return response


def _etag(response):
    value = response.headers.get("etag")
    return value if isinstance(value, str) else ""


def _item_ids(response, count):
    try:
        ids = tuple(item["id"] for item in response.json()["items"])
        if len(ids) != count or len(set(ids)) != count or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError("invalid item identifiers")
        return ids
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("Tidal Connect queue response is incomplete") from None


def create_track_queue(session: Any, track_id: int, quality: str) -> dict:
    """Single-track handoff retained for clients without queue context."""
    return create_playback_queue(session, [track_id], 0, quality).payload


def create_playback_queue(
    session: Any, track_ids: list[int], start_index: int, quality: str,
    repeat: str = "off", autoplay: bool = True, position_ms: int = 0,
) -> CloudQueue:
    """Hand the playback order to the receiver before it reaches a boundary."""
    ids = tuple(int(t) for t in track_ids)
    if not ids or any(t <= 0 for t in ids) or not 0 <= start_index < len(ids):
        raise ValueError("Invalid Tidal Connect playback queue")
    if repeat not in ("off", "all", "one"):
        raise ValueError("Invalid Tidal Connect repeat mode")
    track_id = ids[start_index]
    track = session.track(track_id)
    # The cloud API accepts batches of 100. Start around the selected
    # item, then upload the rest before handing the queue to the device.
    head = max(0, start_index - 50)
    initial = ids[head:head + 100]
    body = {
        "items": _raw_items(initial, head),
        "properties": {"position": start_index - head},
        "repeat_mode": repeat,
        "shuffled": False,
    }
    response = _request(session, "POST", QUEUE_URL, body)
    try:
        queue = response.json()
        queue_id = queue["id"]
        if not isinstance(queue_id, str) or not queue_id:
            raise ValueError("empty queue identifiers")
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("Tidal Connect queue response is incomplete") from None
    item_ids = _item_ids(response, len(initial))
    etag = _etag(response)
    url = f"{QUEUE_URL}/{queue_id}/items"
    for offset in range(head + len(initial), len(ids), 100):
        batch = ids[offset:offset + 100]
        response = _request(session, "PUT", url, {
            "item_id": item_ids[-1], "items": _raw_items(batch, offset), "mode": "append",
        }, etag)
        item_ids += _item_ids(response, len(batch))
        etag = _etag(response)
    while head > 0:
        offset = max(0, head - 100)
        batch = ids[offset:head]
        response = _request(session, "PUT", url, {
            "items": _raw_items(batch, offset), "mode": "prepend",
        }, etag)
        item_ids = _item_ids(response, len(batch)) + item_ids
        etag = _etag(response)
        head = offset

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

    payload = {
        "autoplay": autoplay,
        "position": max(0, int(position_ms)),
        "queueInfo": {
            "queueId": queue_id, "repeatMode": {"off": "OFF", "all": "ALL", "one": "SINGLE"}[repeat], "shuffled": False,
            "maxBeforeSize": 10, "maxAfterSize": 10,
        },
        "currentMediaInfo": _media_info(track, track_id, item_ids[start_index]),
        "contentServerInfo": server_info(CONTENT_URL, True),
        "queueServerInfo": server_info(QUEUE_URL),
    }
    return CloudQueue(payload, item_ids, ids, start_index, etag)


def create_media_info(session, track_id: int, item_id: str) -> dict:
    return _media_info(session.track(track_id), track_id, item_id)


def _media_info(track, track_id, item_id):
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
        "itemId": item_id, "mediaId": str(track_id), "mediaType": 0,
        "metadata": {
            "title": str(getattr(track, "name", "") or ""),
            "albumTitle": str(getattr(album, "name", "") or ""),
            "artists": [str(getattr(a, "name", "")) for a in (getattr(track, "artists", None) or [])],
            "duration": int(float(getattr(track, "duration", 0) or 0) * 1000),
            "images": images,
        },
    }


def update_playback_queue(session, cloud: CloudQueue, ids: list[int], index: int, current_item_id: str) -> CloudQueue:
    """Edit a cloud queue in place; never reload the currently decoding media."""
    ids = tuple(ids)
    old_index = cloud.item_ids.index(current_item_id)
    if not ids or not 0 <= index < len(ids) or ids[index] != cloud.track_ids[old_index]:
        raise ValueError("Current receiver track is missing from the edited queue")
    url = f"{QUEUE_URL}/{cloud.payload['queueInfo']['queueId']}"
    etag = _etag(_request(session, "GET", url))
    available = defaultdict(deque)
    for tid, item_id in zip(cloud.track_ids, cloud.item_ids):
        if item_id != current_item_id:
            available[tid].append(item_id)
    target = []
    for i, tid in enumerate(ids):
        target.append(current_item_id if i == index else available[tid].popleft() if available[tid] else None)
    missing = [i for i, item_id in enumerate(target) if item_id is None]
    tail = cloud.item_ids[-1]
    for offset in range(0, len(missing), 100):
        positions = missing[offset:offset + 100]
        response = _request(session, "PUT", url + "/items", {
            "item_id": tail, "items": _raw_items([ids[i] for i in positions]), "mode": "append",
        }, etag)
        added = _item_ids(response, len(positions))
        for i, item_id in zip(positions, added):
            target[i] = item_id
        tail, etag = added[-1], _etag(response)
    retained = set(target)
    for item_id in cloud.item_ids:
        if item_id not in retained:
            etag = _etag(_request(session, "DELETE", url + "/items/" + item_id, etag=etag))
    # Move the remaining items behind the first desired item. Like
    # insertion, a move accepts at most 100 IDs. Each batch anchors
    # behind the preceding batch and preserves the playing item ID.
    for offset in range(1, len(target), 100):
        batch = target[offset:offset + 100]
        response = _request(session, "PATCH", url + "/items", {
            "after": target[offset - 1], "ids": batch,
        }, etag)
        etag = _etag(response)
    return CloudQueue(cloud.payload, tuple(target), ids, index)
