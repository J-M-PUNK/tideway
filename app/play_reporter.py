"""Report track plays to Tidal's Event Producer.

Without this module, plays through this app don't count for Tidal's
Recently Played, recommendations, or artist royalty accounting —
`tidalapi` fetches stream URLs but never tells Tidal the track was
played. Tidal's own clients use an "Event Producer" pipeline
(`ec.tidal.com/api/event-batch`) that ingests `playback_session` events
containing PLAYBACK_START / PLAYBACK_STOP actions.

The wire format is unusual: it's AWS SQS's `SendMessageBatch` form
encoding (not JSON), with the event payload stuffed into `MessageBody`
and identity metadata into a `Headers` MessageAttribute. This matches
Tidal's iOS/Android/web SDKs:
  - tidal-sdk-web: packages/event-producer/src/submit/submit.ts
  - tidal-sdk-ios: Sources/EventProducer/Events/Models/EventConfig.swift
  - tidal-sdk-android: EventProducer.kt

All reporting is fire-and-forget on a background thread — playback
never waits for a network round-trip, and a failed event is logged but
doesn't surface to the user. Tidal accepts an event based on a valid
access token; the `client-id` in Headers is used only for their own
analytics breakdown.
"""
from __future__ import annotations

import json
import logging
import platform
import threading
import time
import uuid
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Optional

from app.http import SESSION

logger = logging.getLogger("tidal-downloader.play_reporter")

_EVENT_URL = "https://ec.tidal.com/api/event-batch"
_EVENT_NAME = "playback_session"

_APP_NAME = "TIDAL Desktop"
_APP_VERSION = "2.47.0"
_CONSENT_CATEGORY = "NECESSARY"


def _os_name() -> str:
    sys = platform.system().lower()
    if sys == "darwin":
        return "macos"
    if sys == "windows":
        return "windows"
    return "linux"


@dataclass
class PlaySession:
    """One completed track listen ready to be reported.

    `end_position_s - start_position_s` is the actual listened duration
    (skipping seeks). Tidal uses the `actions` list + `startTimestamp` /
    `endTimestamp` to reconstruct what happened.
    """

    session_id: str
    track_id: str
    quality: str
    source_type: Optional[str]
    source_id: Optional[str]
    start_ts_ms: int
    end_ts_ms: int
    start_position_s: float
    end_position_s: float
    audio_mode: str = "STEREO"
    asset_presentation: str = "FULL"
    is_post_paywall: bool = True


def _message_body(session: PlaySession) -> str:
    # `sourceType` / `sourceId` are typed as non-nullable strings in
    # Tidal's SDK schema (playback-session.ts), with empty string as
    # the "unset" default — null would fail validation silently.
    return json.dumps(
        {
            "playbackSessionId": session.session_id,
            "productType": "TRACK",
            "actualProductId": session.track_id,
            "requestedProductId": session.track_id,
            "actualAssetPresentation": session.asset_presentation,
            "actualAudioMode": session.audio_mode,
            "actualQuality": session.quality,
            "sourceType": session.source_type or "",
            "sourceId": session.source_id or "",
            "startTimestamp": session.start_ts_ms,
            "endTimestamp": session.end_ts_ms,
            "startAssetPosition": session.start_position_s,
            "endAssetPosition": session.end_position_s,
            "isPostPaywall": session.is_post_paywall,
            "actions": [
                {
                    "actionType": "PLAYBACK_START",
                    "assetPosition": session.start_position_s,
                    "timestamp": session.start_ts_ms,
                },
                {
                    "actionType": "PLAYBACK_STOP",
                    "assetPosition": session.end_position_s,
                    "timestamp": session.end_ts_ms,
                },
            ],
        },
        separators=(",", ":"),
    )


def _headers_attr(access_token: str, client_id: str) -> str:
    # Tidal's SDK embeds the access token RAW inside the Headers
    # attribute (no "Bearer " prefix) even though the outer HTTP
    # Authorization header uses `Bearer <token>`. Mis-prefixing here
    # causes Tidal to silently discard the event.
    return json.dumps(
        {
            "app-name": _APP_NAME,
            "app-version": _APP_VERSION,
            "client-id": client_id,
            "consent-category": _CONSENT_CATEGORY,
            "os-name": _os_name(),
            "requested-sent-timestamp": str(int(time.time() * 1000)),
            "authorization": access_token,
        },
        separators=(",", ":"),
    )


def _encode_sqs_batch(entries: list[tuple[str, str, str]]) -> dict:
    """Form-encode events as an AWS SQS SendMessageBatch request.

    Each entry is (msg_id, message_body_json, headers_attr_json).
    """
    form: dict = {}
    for i, (msg_id, body, headers_attr) in enumerate(entries, start=1):
        prefix = f"SendMessageBatchRequestEntry.{i}"
        form[f"{prefix}.Id"] = msg_id
        form[f"{prefix}.MessageBody"] = body
        form[f"{prefix}.MessageAttribute.1.Name"] = "Name"
        form[f"{prefix}.MessageAttribute.1.Value.StringValue"] = _EVENT_NAME
        form[f"{prefix}.MessageAttribute.1.Value.DataType"] = "String"
        form[f"{prefix}.MessageAttribute.2.Name"] = "Headers"
        form[f"{prefix}.MessageAttribute.2.Value.StringValue"] = headers_attr
        form[f"{prefix}.MessageAttribute.2.Value.DataType"] = "String"
    return form


class PlayReporter:
    """Background worker that drains a queue of PlaySessions to Tidal.

    Callers `record(...)` and return immediately. A daemon thread
    picks up sessions and POSTs them. Failures are logged and dropped —
    we don't retry indefinitely because a stale play event loses value
    quickly.
    """

    def __init__(self, tidal_client) -> None:
        self.tidal = tidal_client
        self.queue: Queue[PlaySession] = Queue()
        self.enabled = True
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="tidal-play-reporter"
        )
        self._thread.start()

    def record(self, session: PlaySession) -> None:
        if not self.enabled:
            return
        self.queue.put(session)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                session = self.queue.get(timeout=1.0)
            except Empty:
                continue
            try:
                self._send(session)
            except Exception:
                logger.exception("Play-report send failed for %s", session.track_id)

    def _send(self, session: PlaySession) -> None:
        tidal_session = getattr(self.tidal, "session", None)
        if tidal_session is None:
            return
        access_token = getattr(tidal_session, "access_token", None)
        config = getattr(tidal_session, "config", None)
        client_id = getattr(config, "client_id", None) if config else None
        if not access_token:
            logger.debug("No access token; skipping play report for %s", session.track_id)
            return
        msg_id = str(uuid.uuid4())
        form = _encode_sqs_batch([
            (msg_id, _message_body(session), _headers_attr(access_token, client_id or "unknown")),
        ])
        resp = SESSION.post(
            _EVENT_URL,
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {access_token}",
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.warning(
                "Play report HTTP %d for track %s: %s",
                resp.status_code,
                session.track_id,
                resp.text[:200],
            )
        else:
            logger.info(
                "Reported play: track=%s listened=%.1fs",
                session.track_id,
                max(0.0, session.end_position_s - session.start_position_s),
            )

    def stop(self) -> None:
        self._stop.set()
