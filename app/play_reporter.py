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

logger = logging.getLogger("tidal-downloader.play_reporter")

_EVENT_URL = "https://ec.tidal.com/api/event-batch"
_EVENT_NAME = "playback_session"
_EVENT_GROUP = "play_log"
_EVENT_VERSION = 2

_APP_NAME = "TIDAL Desktop"
_APP_VERSION = "2.47.0"
_CONSENT_CATEGORY = "NECESSARY"

# Tidal web-SDK equivalents: client.platform is the string the official
# web player uses too (DESKTOP app is Electron around tidal-sdk-web).
# deviceType is what distinguishes TV / phone / tablet / desktop.
_CLIENT_PLATFORM = "web"
_CLIENT_DEVICE_TYPE = "DESKTOP"


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


def _message_body(
    session: PlaySession,
    *,
    access_token: str,
    fallback_client_id: str,
    fallback_user_id: str,
) -> str:
    """Wrap the play_log event the way Tidal's SDKs do it.

    The raw SQS endpoint returns 200 for any JSON body, but the
    downstream play_log consumer (which populates Recently Played +
    royalty accounting) drops events missing the `group: "play_log"`
    envelope. Matches `tidal-sdk-web`'s
    `packages/player/src/internal/event-tracking/play-log/index.ts`
    and `packages/event-producer/src/send/send.ts` — the outer keys
    are group/name/version/ts/uuid/user/client/payload.

    We pull the numeric `cid` out of the JWT claims for `client.token`
    and `user.clientId`; Tidal's filters distinguish real desktop/web
    clients from third-party OAuth apps by that numeric id.

    `user.id` isn't always in the JWT (tidalapi's PKCE token doesn't
    carry a `sub`), so we accept a fallback from the caller pulled
    off `tidal.session.user.id`. Without a real user id the event
    reaches the producer but can't be attributed to the user's
    history — Recently Played stays empty.
    """
    claims = _decode_jwt_claims(access_token)
    numeric_cid = str(claims.get("cid") or fallback_client_id)
    user_id = str(claims.get("sub") or fallback_user_id or "")
    event_ts_ms = int(time.time() * 1000)
    return json.dumps(
        {
            "group": _EVENT_GROUP,
            "name": _EVENT_NAME,
            "version": _EVENT_VERSION,
            "ts": event_ts_ms,
            "uuid": str(uuid.uuid4()),
            "user": {
                "id": user_id,
                "accessToken": access_token,
                "clientId": numeric_cid,
            },
            "client": {
                "token": numeric_cid,
                "version": _APP_VERSION,
                "platform": _CLIENT_PLATFORM,
                "deviceType": _CLIENT_DEVICE_TYPE,
            },
            "payload": _playback_session_payload(session),
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
        # Tidal user id from the session object. tidalapi populates
        # `session.user` after a successful login; `.id` is the
        # numeric user id Tidal uses across its API. This is the
        # fallback when the access-token JWT doesn't carry a `sub`
        # claim (PKCE tokens often don't).
        user_obj = getattr(tidal_session, "user", None)
        fallback_user_id = ""
        if user_obj is not None:
            try:
                uid = getattr(user_obj, "id", None)
                if uid is not None and int(uid) > 0:
                    fallback_user_id = str(uid)
            except (TypeError, ValueError):
                pass
        if not access_token:
            _append_log({
                "phase": "skipped",
                "track_id": session.track_id,
                "http_status": None,
                "note": "no access token",
            })
            return
        msg_id = str(uuid.uuid4())
        body = _message_body(
            session,
            access_token=access_token,
            fallback_client_id=client_id or "unknown",
            fallback_user_id=fallback_user_id,
        )
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
        # Re-decode the JWT for the log entry so the UI can confirm
        # the numeric cid / user id we actually sent, which is the
        # part Tidal filters on for Recently Played aggregation.
        # user_id reflects the actual value that ended up in the
        # envelope (JWT's `sub` first, then tidalapi's session.user.id
        # fallback) so "?" here unambiguously means we sent no user.
        claims = _decode_jwt_claims(access_token)
        resolved_user_id = str(
            claims.get("sub") or fallback_user_id or "?"
        )
        entry = {
            "phase": "sent",
            "track_id": session.track_id,
            "http_status": resp.status_code,
            "listened_s": round(listened_s, 1),
            "client_id": (client_id[:12] + "…") if client_id else "unknown",
            "numeric_cid": str(claims.get("cid") or "?"),
            "user_id": resolved_user_id,
        }
        if resp.status_code >= 400:
            # Tidal's event-producer returns an AWS-style XML error body;
            # truncate so the log stays scannable. Keep enough to
            # distinguish "bad auth" from "bad payload".
            entry["note"] = resp.text[:300] if resp.text else ""
        else:
            # On success, surface the ids we actually put into the
            # envelope so the user can see at a glance whether the
            # JWT decode + session.user.id fallback worked. "user=?"
            # means the event went out without a user-id attribution
            # and almost certainly won't show up in Recently Played.
            entry["note"] = (
                f"cid={entry['numeric_cid']} user={entry['user_id']}"
            )
        _append_log(entry)

    def stop(self) -> None:
        self._stop.set()
