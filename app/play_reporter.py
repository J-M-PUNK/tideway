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

import base64
import json
import logging
import platform
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Optional

from app.http import SESSION

logger = logging.getLogger("tideway.play_reporter")

_EVENT_URL = "https://ec.tidal.com/api/event-batch"
_EVENT_NAME = "playback_session"
_EVENT_GROUP = "play_log"
_EVENT_VERSION = 2

_APP_NAME = "TIDAL Desktop"
_APP_VERSION = "2.47.0"
_CONSENT_CATEGORY = "NECESSARY"

# Browser attributes reported in the Headers MessageAttribute.
# Tidal Desktop is an Electron shell sitting on top of the
# tidal-sdk-web code, so the values it reports are the Chromium
# engine strings below. The event bus itself doesn't enforce these
# values. They feed telemetry breakdowns. The reason they're here
# at all is that an earlier version of this module omitted them,
# which was a drift from `tidal-sdk-web/utils/headerUtils.ts` and
# may have been what the play_log consumer's validator was
# catching on.
_BROWSER_NAME = "Chrome"
_BROWSER_VERSION = "120.0.0.0"


# Rolling buffer of the most recent report attempts + their outcomes.
# Exposed via /api/play-report/log so the user can confirm whether
# plays are actually reaching Tidal without digging through stderr.
# Keep the buffer small — older entries fall off once capacity is hit.
_REPORT_LOG_CAP = 50
_report_log: list[dict] = []
_report_log_lock = threading.Lock()


def _append_log(entry: dict) -> None:
    entry["ts_ms"] = int(time.time() * 1000)
    with _report_log_lock:
        _report_log.append(entry)
        if len(_report_log) > _REPORT_LOG_CAP:
            del _report_log[: len(_report_log) - _REPORT_LOG_CAP]
    # Also echo to stderr so launching the app from Terminal surfaces
    # the result live. Keeps the dev-mode debug path free.
    print(
        f"[play-report] {entry.get('phase')} track={entry.get('track_id')} "
        f"status={entry.get('http_status')} note={entry.get('note', '')}",
        file=sys.stderr,
        flush=True,
    )


def recent_log() -> list[dict]:
    with _report_log_lock:
        return list(_report_log)


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


def _decode_jwt_claims(token: str) -> dict:
    """Return the JWT payload segment as a dict. Best-effort — any
    failure (malformed token, wrong algorithm, base64 noise) yields
    an empty dict so the caller can fall back to string-based client
    ids. Tidal's access tokens are `header.payload.signature` base64url
    with no leading `Bearer`; the `cid` claim holds the numeric client
    id, `sub` the user id."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        seg = parts[1]
        padded = seg + "=" * ((4 - len(seg) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded)
        return json.loads(raw)
    except Exception:
        return {}


def _playback_session_payload(session: PlaySession) -> dict:
    """Inner `payload.*` fields for a playback_session event.

    `sourceType` / `sourceId` are typed as non-nullable strings in
    Tidal's SDK schema (playback-session.ts), with empty string as
    the "unset" default — null would fail validation silently.
    """
    return {
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
    }


def _message_body(session: PlaySession) -> str:
    """Wrap the play_log event the same way tidal-sdk-web does.

    The real SDK builds the outer body in
    `tidal-sdk-web/packages/event-producer/src/send/send.ts`. The
    `createPayload` function spreads the raw event and appends
    `ts` and `uuid`, so the body shape is just `group`, `name`,
    `payload`, `version`, `ts`, and `uuid`. There is no `user` or
    `client` object. Attribution is done on the server side from
    the JWT in the `authorization` Headers attribute.

    An earlier version of this function did include `user` and
    `client` keys, matching a schema we assumed Tidal used
    internally. The bus accepted those events and SQS returned
    200, but the plays never surfaced in Recently Played. The
    most likely explanation is that the play_log consumer
    validates the body shape against the real SDK schema and
    quietly rejects events with unexpected keys.
    """
    return json.dumps(
        {
            "group": _EVENT_GROUP,
            "name": _EVENT_NAME,
            "version": _EVENT_VERSION,
            "ts": int(time.time() * 1000),
            "uuid": str(uuid.uuid4()),
            "payload": _playback_session_payload(session),
        },
        separators=(",", ":"),
    )


def _headers_attr(access_token: str, client_id: str) -> str:
    # Mirrors `tidal-sdk-web/utils/headerUtils.ts` exactly. Every
    # key the real SDK sets is set here. The `browser-name` and
    # `browser-version` entries were missing in an earlier version
    # of this function, which is most likely why the bus accepted
    # events but Recently Played never surfaced them.
    #
    # Two details to watch. The `authorization` value is the raw
    # token with no `Bearer ` prefix. The outer HTTP Authorization
    # header is still `Bearer <token>`. The `requested-sent-timestamp`
    # value is a number, not a string. The SDK types the value as
    # `Record<string, number | string>`, so either would work at
    # the JSON level, but sending a number matches what the real
    # client does and removes one more thing the validator might
    # care about.
    return json.dumps(
        {
            "app-name": _APP_NAME,
            "app-version": _APP_VERSION,
            "browser-name": _BROWSER_NAME,
            "browser-version": _BROWSER_VERSION,
            "client-id": client_id,
            "consent-category": _CONSENT_CATEGORY,
            "os-name": _os_name(),
            "requested-sent-timestamp": int(time.time() * 1000),
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
            _append_log({
                "phase": "skipped",
                "track_id": session.track_id,
                "http_status": None,
                "note": "no tidal session",
            })
            return
        access_token = getattr(tidal_session, "access_token", None)
        config = getattr(tidal_session, "config", None)
        client_id = getattr(config, "client_id", None) if config else None
        if not access_token:
            _append_log({
                "phase": "skipped",
                "track_id": session.track_id,
                "http_status": None,
                "note": "no access token",
            })
            return
        msg_id = str(uuid.uuid4())
        body = _message_body(session)
        form = _encode_sqs_batch([
            (msg_id, body, _headers_attr(access_token, client_id or "unknown")),
        ])
        try:
            resp = SESSION.post(
                _EVENT_URL,
                data=form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=10,
            )
        except Exception as exc:
            _append_log({
                "phase": "sent",
                "track_id": session.track_id,
                "http_status": None,
                "note": f"network error: {exc!r}",
            })
            raise
        listened_s = max(0.0, session.end_position_s - session.start_position_s)
        # The log entry surfaces which numeric cid / user id Tidal will
        # attribute this play to. Attribution is entirely server-side
        # from the JWT claims — `uid` (Tidal's non-standard name for
        # the user id) and `cid` (numeric client id). If either is "?"
        # in the log, the token itself is missing a claim and Recently
        # Played won't pick the event up regardless of what we send.
        claims = _decode_jwt_claims(access_token)
        resolved_user_id = str(claims.get("uid") or claims.get("sub") or "?")
        entry = {
            "phase": "sent",
            "track_id": session.track_id,
            "http_status": resp.status_code,
            "listened_s": round(listened_s, 1),
            "client_id": (client_id[:12] + "…") if client_id else "unknown",
            "numeric_cid": str(claims.get("cid") or "?"),
            "user_id": resolved_user_id,
            # sourceType/sourceId is the single biggest Recently-Played
            # attribution knob on the server side: TRACK events count for
            # "Most Listened" aggregates but don't surface in Recently
            # Played — only ALBUM / PLAYLIST / MIX / ARTIST container
            # events do. Surfacing both in the log lets us confirm at a
            # glance whether the play we're about to fire has a container
            # context attached, without digging into the raw body.
            "source_type": session.source_type or "",
            "source_id": session.source_id or "",
        }
        if resp.status_code >= 400:
            # Tidal's event-producer returns an AWS-style XML error body;
            # truncate so the log stays scannable. Keep enough to
            # distinguish "bad auth" from "bad payload".
            entry["note"] = resp.text[:300] if resp.text else ""
        else:
            # On success, surface the ids Tidal will key this event
            # off of. "user=?" means the JWT didn't carry a `uid`
            # claim and Recently Played can't attribute it. "src=TRACK"
            # is the usual Recently-Played blocker — TRACK plays count
            # for aggregates but don't surface in Recently Played.
            src_label = (
                f"{entry['source_type']}:{entry['source_id']}"
                if entry["source_type"]
                else "none"
            )
            entry["note"] = (
                f"cid={entry['numeric_cid']} user={entry['user_id']} "
                f"src={src_label}"
            )
        # Stash the full outgoing event body on the log entry so the
        # user can inspect exactly what we sent when Recently Played
        # isn't working. Only kept on the most-recent entries because
        # the buffer is capped at _REPORT_LOG_CAP — no unbounded growth.
        entry["payload_preview"] = body
        _append_log(entry)

    def stop(self) -> None:
        self._stop.set()
