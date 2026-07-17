import json
import logging
import os
import re
import stat
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

import tidalapi

from app.paths import user_data_dir

# audio.log is bound to the "tideway.audio" logger in
# app/audio/player.py (a RotatingFileHandler, propagate=False).
# Auth-lifecycle lines used to print only to stderr, which is
# invisible in the packaged app — so a user "getting logged out"
# left no diagnosable trace in production. Mirror the high-signal
# auth lines to audio.log as well, same pattern the AirPlay
# diagnostics use.
_audit_log = logging.getLogger("tideway.audio")


def _tlog(msg: str) -> None:
    """High-signal Tidal auth line: dev console (stderr) + audio.log."""
    line = f"[tidal] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        _audit_log.info(line)
    except Exception:
        # Logging to file must never break the auth path; the
        # stderr line already carried the signal.
        pass

SESSION_FILE = user_data_dir() / "tidal_session.json"


# --- Tidal request gate + abuse/rate-limit backoff ---------------------
#
# Tidal's anti-abuse layer responds to suspicious volume with either
# HTTP 429 (soft rate-limit) or HTTP 403 + `"abuse_detected"` (account
# suspension escalation path). When we see either signal we want to
# stop hitting Tidal immediately: further requests only deepen the
# suspension and can trigger permanent-ban. We install one interceptor
# on tidalapi's central HTTP method so every call passes through the
# gate. Module-level state so any place that instantiates a
# TidalClient shares the same backoff window.
_tidal_backoff_until: float = 0.0
_tidal_backoff_reason: str = ""
_tidal_backoff_lock = threading.Lock()


class TidalBackoffError(Exception):
    """Raised when we refuse to make a Tidal request because we're
    still inside a backoff window. Contains the remaining seconds so
    the API layer can surface a 503 with a Retry-After-style message."""

    def __init__(self, seconds_remaining: float, reason: str):
        self.seconds_remaining = seconds_remaining
        self.reason = reason
        super().__init__(
            f"Tidal backoff active for another {seconds_remaining:.0f}s: {reason}"
        )


class TransientRefreshError(Exception):
    """Raised when a token refresh fails for a transient reason — a 429
    rate-limit, a 5xx from Tidal, or a network error — rather than a
    genuinely dead refresh token.

    The distinction matters because force_refresh logs the user out and
    deletes the session file on a *permanent* auth failure
    (AuthenticationError). A transient failure must NOT do that: the
    refresh token is still valid, so we keep the session and let the
    watchdog retry on its next tick. Conflating the two was silently
    logging users out days later whenever a background refresh happened
    to land during a rate-limit window or a Tidal outage."""


def tidal_backoff_state() -> dict:
    """Snapshot for /api/tidal/backoff so the frontend can surface a
    banner explaining why things are blocked."""
    with _tidal_backoff_lock:
        return {
            "active": time.time() < _tidal_backoff_until,
            "until_epoch": _tidal_backoff_until,
            "seconds_remaining": max(0.0, _tidal_backoff_until - time.time()),
            "reason": _tidal_backoff_reason,
        }


def _classify_tidal_error(status_code: Optional[int], body: str) -> None:
    """Inspect a Tidal response's status + body and engage the
    appropriate backoff. No-op on success / non-abuse failures.
    Shared between the session request gate (for normal API calls)
    and the PKCE token-endpoint exception handler (which bypasses
    the gate because tidalapi uses its own requests.post for auth).
    """
    if status_code == 429:
        _trigger_tidal_backoff(60.0, "rate-limited (HTTP 429)")
        return
    if status_code == 403:
        body_lower = (body or "").lower()
        if "abuse" in body_lower or "suspended" in body_lower:
            _trigger_tidal_backoff(
                30 * 60.0, f"abuse-detected (HTTP 403): {(body or '')[:200]}"
            )


def _trigger_tidal_backoff(duration_seconds: float, reason: str) -> None:
    global _tidal_backoff_until, _tidal_backoff_reason
    with _tidal_backoff_lock:
        until = time.time() + duration_seconds
        # Extend if we'd be setting a shorter window than already
        # pending — never pull a backoff in early.
        if until > _tidal_backoff_until:
            _tidal_backoff_until = until
            _tidal_backoff_reason = reason
    import sys as _sys
    print(
        f"[tidal] backoff engaged for {duration_seconds:.0f}s: {reason}",
        file=_sys.stderr,
        flush=True,
    )


def tidal_jitter_sleep() -> None:
    """Random 50-200 ms pause used inside parallel pools that hit
    Tidal. Aligns bursts to look closer to human-paced navigation
    than a mechanical fan-out — our five concurrent requests all
    firing within 5 ms is a signature no real client produces."""
    import random as _random
    time.sleep(_random.uniform(0.05, 0.20))


# Headers we apply on top of whatever curl-cffi's impersonation
# profile already supplies. UA matches the real Tidal Android client;
# Accept-Language pins us to en-US so personalized editorial doesn't
# vary with the (often empty) system locale.
_TIDAL_ANDROID_HEADERS: dict = {
    "User-Agent": "TIDAL_ANDROID/2.88.0 okhttp/4.12.0",
    "Accept-Language": "en-US",
}


def _install_tidal_request_gate(session: tidalapi.Session) -> None:
    """Wrap tidalapi's Requests.basic_request so every HTTP call:
      1. Refuses early with TidalBackoffError if we're still in a
         backoff window (preserves the account — every request while
         suspended extends the strike window).
      2. On 429, engages a 60 s backoff.
      3. On 403 with `abuse_detected` / `"abuse"` in the body, engages
         a much longer (30-minute) backoff so we don't re-hit while
         Tidal's fraud system is still watching.
    """
    requests_obj = session.request
    original = requests_obj.basic_request

    def wrapped(method, path, params=None, data=None, headers=None, base_url=None):
        now = time.time()
        with _tidal_backoff_lock:
            remaining = _tidal_backoff_until - now
            reason = _tidal_backoff_reason
        if remaining > 0:
            raise TidalBackoffError(remaining, reason)

        resp = original(method, path, params=params, data=data, headers=headers, base_url=base_url)
        body = ""
        if getattr(resp, "status_code", None) == 403:
            try:
                body = resp.text[:400]
            except Exception:
                pass
        _classify_tidal_error(getattr(resp, "status_code", None), body)
        return resp

    # Bind the wrapper on the instance so we don't leak patches
    # across Session objects (the login flow rebuilds the session).
    requests_obj.basic_request = wrapped


def _swap_to_impersonated_transport(session: tidalapi.Session) -> None:
    """Replace tidalapi's underlying request_session with a curl-cffi
    session that matches a real mobile-Chrome TLS stack. urllib3's
    ClientHello + HTTP/2 SETTINGS are a Python-specific fingerprint
    Tidal's anti-abuse can match on without seeing any request
    volume. Best-effort: keeps the plain requests.Session if
    curl-cffi can't be loaded.
    """
    from app.http import build_impersonated_session

    impersonated = build_impersonated_session()
    if impersonated is None:
        return
    old = getattr(session, "request_session", None)
    if old is not None and hasattr(old, "headers"):
        try:
            impersonated.headers.update(dict(old.headers))
        except Exception:
            pass
    impersonated.headers["User-Agent"] = _TIDAL_ANDROID_HEADERS["User-Agent"]
    impersonated.headers["Accept-Language"] = _TIDAL_ANDROID_HEADERS[
        "Accept-Language"
    ]
    session.request_session = impersonated


_track_ai_patched = False


def _patch_track_ai_field() -> None:
    """Teach tidalapi's Track parser to keep the `ai` flag.

    Tidal tags every 100% AI-generated track with a top-level `ai`
    boolean on the track payload (its July 2026 AI-content policy).
    tidalapi 0.8.11 parses ~30 track fields but drops this one and
    retains no raw JSON, so without this patch the flag never reaches
    Python and the AI-content filter has nothing to act on. Wrap
    parse_track so the returned Track carries `ai`; a class-level
    default of None keeps `getattr(track, "ai", None)` safe on any
    Track built before the wrapper ran.

    Class-level and idempotent: Track objects are created deep inside
    tidalapi, so an instance patch can't reach them, and the login
    flow rebuilds the Session more than once.
    """
    global _track_ai_patched
    if _track_ai_patched:
        return
    from tidalapi.media import Track

    original = Track.parse_track
    Track.ai = None

    def parse_track_with_ai(self, json_obj, album=None):
        track = original(self, json_obj, album)
        ai = bool(json_obj.get("ai")) if "ai" in json_obj else None
        # parse_track returns copy.copy(self), and the two callers keep
        # different objects: list parsing (map_json) uses the returned
        # copy, but Track.__init__ discards _get's return and keeps
        # `self`. Set the flag on both so either path carries it.
        track.ai = ai
        self.ai = ai
        return track

    Track.parse_track = parse_track_with_ai
    _track_ai_patched = True


_patch_track_ai_field()


def _friendly_pkce_error(exc: BaseException) -> Optional[str]:
    """Translate the cryptic exceptions tidalapi can raise during the
    PKCE token exchange into something a non-developer can act on.
    Returns None when the exception isn't one we have a friendlier
    string for, so the caller can fall back to the raw message.

    The big one is `ConnectionError("Connection aborted!", PermissionError(13, ...))`,
    which on Windows almost always means an antivirus or firewall is
    blocking outbound TCP from the bundled Python. The user gets a
    401 with no idea what to fix; we want them to know it's a local
    security product, not a Tidal-side rejection.
    """
    text = repr(exc)
    chain: list[BaseException] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    # The PermissionError is sometimes nested as the inner-args entry of
    # the requests ConnectionError rather than via __cause__, so also
    # walk .args looking for it. Same id-tracking dedupe so a wrapped
    # arg already on the cause chain doesn't trigger reprocessing.
    for item in list(chain):
        for a in getattr(item, "args", ()) or ():
            if isinstance(a, BaseException) and id(a) not in seen:
                seen.add(id(a))
                chain.append(a)

    for inner in chain:
        if isinstance(inner, PermissionError):
            return (
                "The login request was blocked by your computer before it "
                "reached Tidal. This is almost always antivirus or firewall "
                "software (Bitdefender, Sophos, McAfee, Norton, Windows "
                "Defender, etc) refusing to let Tideway open a network "
                "connection. Add Tideway (or Tideway.exe) to your security "
                "software's allow list and try again."
            )
    if "Connection aborted" in text and "Permission denied" in text:
        return (
            "The login request was blocked by your computer before it "
            "reached Tidal. This is almost always antivirus or firewall "
            "software refusing to let Tideway open a network connection. "
            "Add Tideway to your security software's allow list and try again."
        )
    return None


def _fetch_all_pages(method) -> list:
    """Exhaustively fetch every item from a paginated tidalapi list method.

    Strategy, in order:
      1. Ask for a huge single page with newest-first ordering.
      2. If that returns nothing or throws, ask for a huge single page with no
         ordering (some older tidalapi versions don't accept the kwargs).
      3. If still nothing, page through manually in chunks of 50.
      4. As a final fallback, call with no arguments.
    """
    # 1) Single-shot ordered fetch
    for kwargs in (
        {"limit": 10000, "order": "DATE", "order_direction": "DESC"},
        {"limit": 10000},
    ):
        try:
            items = list(method(**kwargs))
            if items:
                return items
        except Exception:
            pass

    # 2) Manual pagination in chunks of 50
    collected: list = []
    seen_ids: set = set()
    offset = 0
    page_size = 50
    while True:
        page: list = []
        for kwargs in (
            {"limit": page_size, "offset": offset},
            {"limit": page_size, "offset": offset, "order": "DATE", "order_direction": "DESC"},
        ):
            try:
                page = list(method(**kwargs))
                break
            except Exception:
                continue
        if not page:
            break
        # Duplicate-detection safety: if the endpoint misbehaves and keeps
        # returning the same page regardless of offset (happens on some
        # tidalapi versions / under transient 5xx retries), `len(page)
        # == page_size` would loop until the 20k cap with a completely
        # duplicated `collected` list. Break as soon as a page contributes
        # zero new items.
        added = 0
        for obj in page:
            key = getattr(obj, "id", None)
            if key is None:
                key = getattr(obj, "uuid", None)
            if key is None:
                # Item without a stable identifier — append unconditionally
                # but don't count it as "new" for the progress check.
                collected.append(obj)
                continue
            if key in seen_ids:
                continue
            seen_ids.add(key)
            collected.append(obj)
            added += 1
        if added == 0:
            break
        if len(page) < page_size:
            break
        offset += page_size
        if offset > 20000:  # hard safety cap
            break
    if collected:
        return collected

    # 3) Last resort
    try:
        return list(method())
    except Exception:
        return []


def _sort_by_date_added(items: list) -> list:
    """Sort items newest-first using whichever timestamp attribute tidalapi
    exposes. If no such attribute is found, return the list unchanged."""
    if not items:
        return items

    candidates = ("user_date_added", "date_added", "created", "added_at", "dateAdded")

    def key(obj):
        for attr in candidates:
            value = getattr(obj, attr, None)
            if value is None:
                continue
            if isinstance(value, datetime):
                return value.timestamp()
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except Exception:
                try:
                    return float(value)
                except Exception:
                    continue
        return None

    keyed = [(key(o), i, o) for i, o in enumerate(items)]
    if all(k is None for k, _, _ in keyed):
        return items  # no timestamp available; preserve server order
    # Items without a timestamp fall to the bottom
    keyed.sort(key=lambda t: (t[0] is None, -(t[0] or 0), t[1]))
    return [o for _, _, o in keyed]


# Tidal returns a `highestSoundQuality` code on the subscription endpoint.
# Map those codes to the tidalapi.Quality member names we use everywhere
# else in the app, and keep them in ascending order so "everything at or
# below X" is a simple slice.
_SUB_TTL_SEC = 300.0  # 5 minutes; subscriptions change rarely.

_SUB_QUALITY_ORDER = ["low_96k", "low_320k", "high_lossless", "hi_res_lossless"]
_SUB_QUALITY_MAP = {
    "LOW": "low_96k",
    "HIGH": "low_320k",
    "LOSSLESS": "high_lossless",
    "HI_RES_LOSSLESS": "hi_res_lossless",
    # Tidal's current single-tier subscription reports `HI_RES` as the
    # `highestSoundQuality`, which DOES include hi-res FLAC (Max). The
    # catch: only a PKCE-authenticated session's access_token is
    # entitled to stream it — device-code sessions 401 at Max even
    # though the subscription allows it. We therefore translate HI_RES
    # to hi_res_lossless ONLY when the session is PKCE; for device-code
    # sessions we fall back to high_lossless so the UI doesn't offer
    # an unreachable option. See get_max_quality() below.
    "HI_RES": "hi_res_lossless",
    "MASTER": "hi_res_lossless",
    "HIFI": "high_lossless",
    "HIFI_PLUS": "hi_res_lossless",
}

# Client-level ceiling: even if the subscription says Max, the device-code
# OAuth client_id is capped at Lossless. Only the PKCE client is entitled
# for hi-res.
_DEVICE_CODE_MAX = "high_lossless"


# Device-code (Limited Input Device) credentials. tidalapi 0.8.x ships a
# default client_id that Tidal has revoked Limited Input Device
# entitlement on; calling /v1/oauth2/device_authorization with it now
# 400s with `Client is not a Limited Input Device client`. The legacy
# tidalapi <=0.7.x default — Tidal's "TV" client — still has the
# entitlement, so we override at TidalClient construction. PKCE uses
# `client_id_pkce`, which we leave on tidalapi's bundled value.
_DEVICE_CODE_CLIENT_ID = "zU4XHVVkc2tDPo4t"
_DEVICE_CODE_CLIENT_SECRET = "VJKhDFqJPqvsPVNBV6ukXTJmwlvbttP7wlMlrc72se4="


class TidalClient:
    # Proactive refresh window: if the stored expiry is within this many
    # seconds of "now" when the watchdog ticks, refresh the token.
    # tidalapi tokens last an hour, so a 5-minute cushion gives us plenty
    # of margin without thrashing on refreshes.
    _REFRESH_WINDOW_SEC = 5 * 60
    # How often the watchdog re-checks. 60s is frequent enough that the
    # expiry window can't slip past between checks, but light enough on
    # cycles that nobody notices.
    _REFRESH_CHECK_INTERVAL_SEC = 60

    def __init__(self):
        config = tidalapi.Config(quality=tidalapi.Quality.high_lossless)
        # Pin the device-code client_id to the legacy "TV" client that
        # still has Limited Input Device entitlement. tidalapi's
        # bundled default has been revoked by Tidal — see the constants
        # above. PKCE uses `client_id_pkce`, untouched by this override.
        config.client_id = _DEVICE_CODE_CLIENT_ID
        config.client_secret = _DEVICE_CODE_CLIENT_SECRET
        self.session = tidalapi.Session(config)
        _install_tidal_request_gate(self.session)
        _swap_to_impersonated_transport(self.session)
        self._install_capturing_refresh()
        # Set by the server to its auth-cache invalidator. Invoked
        # when a hard refresh failure logs the user out, so the UI
        # bounces to Login immediately instead of after the cache TTL.
        self.on_auth_lost: Optional[Callable[[], None]] = None
        self._login_future: Optional[Future] = None
        # Cached subscription-tier result. Populated on first call and
        # refreshed every _SUB_TTL_SEC. Subscription almost never changes
        # within a session, so a long TTL is fine.
        self._sub_cache: tuple[float, Optional[str]] = (0.0, None)
        self._sub_lock = threading.Lock()
        # Serializes EVERY token refresh — the watchdog's force_refresh
        # AND tidalapi's internal auto-refresh-on-401 — through one lock,
        # so concurrent 401s from parallel workers can't fire two
        # token_refresh RPCs at once. With rotating refresh tokens the
        # second POST would carry a token Tidal just invalidated and then
        # persist that broken lineage, logging the user out days later.
        # The single-flight check lives in _refresh_once.
        self._refresh_lock = threading.Lock()
        # Background watchdog: refreshes the token a few minutes before
        # it's due to expire. Without this, a long-running session (user
        # leaves the tab open overnight) lands a 401 the next time they
        # click anything, and though downstream callers have their own
        # retry-on-401 wrappers, it's a worse UX than just keeping the
        # token fresh in the background. Daemon so it doesn't block
        # process exit.
        self._refresh_stop = threading.Event()
        threading.Thread(
            target=self._refresh_watchdog, daemon=True, name="tidal-refresh"
        ).start()

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def load_session(self) -> bool:
        if not SESSION_FILE.exists():
            return False
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            expiry = (
                datetime.fromisoformat(data["expiry_time"])
                if data.get("expiry_time")
                else None
            )
            refresh_token = data.get("refresh_token")
            is_pkce = bool(data.get("is_pkce", False))
            # For PKCE sessions, swap the client_id to the hi-res-entitled
            # one BEFORE any Tidal call. Otherwise the /sessions call
            # inside load_oauth_session (and the proactive refresh below)
            # would run under the device-code client_id and the resulting
            # tokens/session wouldn't be entitled for Max quality.
            if is_pkce:
                self.session.is_pkce = True
                self.session.client_enable_hires()
            # Proactive refresh: tidalapi only refreshes on 401 when the
            # response's userMessage starts with the exact string
            # "The token has expired." — Tidal has changed that message
            # format in the past, and when it doesn't match we get raw
            # 401s propagating to the caller (e.g. download endpoints).
            # If the stored expiry is in the past, skip trying the stale
            # token and refresh up front.
            now = datetime.now(expiry.tzinfo) if expiry and expiry.tzinfo else datetime.now()
            if expiry and refresh_token and expiry <= now:
                try:
                    if self._token_refresh_capturing(refresh_token):
                        self.save_session()
                except Exception:
                    pass
            self.session.load_oauth_session(
                data["token_type"],
                self.session.access_token or data["access_token"],
                getattr(self.session, "refresh_token", None) or refresh_token,
                self.session.expiry_time or expiry,
                is_pkce=is_pkce,
            )
            return self.session.check_login()
        except Exception:
            return False

    def _token_refresh_capturing(self, refresh_token: str) -> bool:
        """Refresh the access token, keeping a rotated refresh token.

        tidalapi 0.8.11's `Session.token_refresh()` updates the
        access token, expiry, and token type from Tidal's response
        but never reads the `refresh_token` field of that response.
        Tidal rotates refresh tokens: a refresh sometimes comes back
        with a new refresh token and the old one is then invalidated
        server-side. Because tidalapi drops it, `save_session()` keeps
        re-persisting the original token; once Tidal kills it (a few
        days out) the next refresh fails and the user is logged out.

        This mirrors tidalapi's refresh exactly — same OAuth params,
        same config (so device-code and PKCE both stay correct), same
        impersonated transport — but also carries a rotated refresh
        token back onto the session so the subsequent save persists it.

        Failures are split by cause. A genuine auth rejection (Tidal's
        `invalid_grant` and friends, or a bare 401) raises
        AuthenticationError so the caller logs the user out. A transient
        transport failure (429 rate-limit, 5xx, non-JSON error page)
        raises TransientRefreshError so the caller keeps the session and
        retries later. Conflating the two used to delete a valid session
        whenever a background refresh landed on a rate-limit or outage.
        """
        session = self.session
        config = session.config
        is_pkce = bool(getattr(session, "is_pkce", False))
        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": (
                config.client_id_pkce if is_pkce else config.client_id
            ),
            "client_secret": (
                config.client_secret_pkce
                if is_pkce
                else config.client_secret
            ),
        }
        resp = session.request_session.post(config.api_oauth2_token, params)
        if resp.status_code != 200:
            # Split permanent auth rejection from transient transport
            # failure. Only the former should log the user out and wipe
            # the session file (force_refresh's AuthenticationError path).
            # A 429 rate-limit or a 5xx is "retry shortly" and the refresh
            # token is still valid — wrongly treating it as a dead token
            # is what silently logged users out days later when a
            # background refresh happened to hit a rate-limit window.
            try:
                body = resp.json()
            except ValueError:
                # Non-JSON body (e.g. an HTML 5xx error page). No OAuth
                # error code to read; treat as transient.
                body = {}
            error = str(body.get("error") or "")
            desc = body.get("error_description")
            # The OAuth spec's permanent failure codes, plus a bare 401
            # (bad client auth). Tidal returns `invalid_grant` when the
            # refresh token is expired or revoked — the genuine re-login
            # case. Anything else (429, 5xx, an unrecognized 4xx) is
            # transient: keep the session and let the watchdog retry.
            permanent = resp.status_code == 401 or error in (
                "invalid_grant",
                "invalid_request",
                "invalid_client",
                "unauthorized_client",
                "unsupported_grant_type",
            )
            if permanent:
                raise tidalapi.exceptions.AuthenticationError(
                    "Authentication failed with error "
                    f"'{error}: {desc}'"
                )
            raise TransientRefreshError(
                f"token refresh got HTTP {resp.status_code} "
                f"(error={error or 'none'}); refresh token kept"
            )
        body = resp.json()
        session.access_token = body["access_token"]
        # Naive UTC, matching how tidalapi stores expiry_time
        # everywhere else. Mixing a tz-aware value here would make
        # tidalapi's own naive-vs-aware datetime math raise.
        session.expiry_time = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) + timedelta(seconds=body["expires_in"])
        session.token_type = body["token_type"]
        # The actual fix: a rotated refresh token must replace the
        # stored one. Tidal only returns this field when it rotates;
        # when absent the existing token stays valid, so keep it.
        new_refresh = body.get("refresh_token")
        if new_refresh:
            session.refresh_token = new_refresh
        return True

    def _refresh_once(self, based_on: Optional[str]) -> bool:
        """Locked, single-flight refresh shared by every runtime path
        (the watchdog's force_refresh and tidalapi's internal
        auto-refresh-on-401). Holding one lock across both is what
        actually delivers the "no two concurrent refresh RPCs"
        guarantee: previously only force_refresh took the lock, so two
        parallel 401s going through tidalapi's path both POSTed the
        same refresh token. With rotation, the second POST carries a
        token Tidal just invalidated, and persisting that broken
        lineage logs the user out a few days later.

        `based_on` is the refresh token the caller observed before
        contending for the lock. Once we hold the lock, if the
        session's refresh token has already moved on — another thread
        refreshed and Tidal rotated — that refresh supersedes this one,
        so we reuse it instead of re-POSTing the now-dead token.

        A permanent AuthenticationError propagates so force_refresh's
        logout-on-auth-failure path runs. A TransientRefreshError (429,
        5xx, network) is swallowed to a False return: the refresh token
        is still valid, so we keep the session and let the next watchdog
        tick retry rather than wiping a good session over a blip.
        """
        with self._refresh_lock:
            current = getattr(self.session, "refresh_token", None)
            if not current:
                return False
            if based_on is not None and current != based_on:
                # A parallel refresh already rotated the token we were
                # going to use; its result stands. Re-POSTing `current`
                # would be redundant, and re-POSTing `based_on` would
                # use a token Tidal already invalidated.
                return True
            try:
                ok = self._token_refresh_capturing(current)
            except TransientRefreshError as exc:
                _tlog(f"refresh: transient failure, session kept: {exc}")
                return False
            if ok:
                try:
                    self.save_session()
                except Exception:
                    # Best-effort: the in-memory session is already
                    # valid, and the watchdog / next refresh re-persists.
                    # Don't fail the refresh over a transient disk error.
                    pass
            return ok

    def _token_refresh_and_persist(self, refresh_token: str) -> bool:
        """token_refresh that captures rotation AND persists.

        This is what tidalapi's own request layer calls on a 401
        (request.py: `self.session.token_refresh(refresh_token)`).
        tidalapi's native implementation drops a rotated refresh
        token, so a few days later the next refresh fails and the
        user is silently logged out. Routing that internal path
        through _refresh_once captures the rotation, persists it, and
        — crucially — serializes it against every other refresh path
        so parallel 401s can't clobber each other's rotated token.
        """
        return self._refresh_once(based_on=refresh_token)

    def _install_capturing_refresh(self) -> None:
        """Shadow tidalapi's Session.token_refresh on this session
        with the rotation-capturing+persisting version, so EVERY
        refresh path — explicit force_refresh, the background
        watchdog, and tidalapi's own internal auto-refresh-on-401 —
        keeps a rotated refresh token. Re-applied after logout()
        because that builds a fresh Session. Same monkeypatch
        approach already used for the request gate and impersonated
        transport on this object."""
        self.session.token_refresh = self._token_refresh_and_persist

    def _notify_auth_lost(self) -> None:
        """Tell the server its cached auth state is stale (refresh
        token dead, user logged out) so the next /auth/status flips
        to logged-out and the UI bounces to Login immediately."""
        cb = getattr(self, "on_auth_lost", None)
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def force_refresh(self) -> bool:
        """Explicitly refresh the access token using the stored refresh
        token. Called by the download path when it hits a 401 — tidalapi's
        built-in refresh only triggers on a specific error message we
        can't rely on. Returns True on success.

        Logs to stderr on failure so the caller (and whoever is tailing
        the server log) can see whether the refresh actually ran and
        why it failed. If the refresh token is itself expired, wipes the
        persisted session file so the user is bounced to the login flow
        on next auth check instead of being stuck in a retry loop.

        Serialized on _refresh_lock so concurrent 401s from parallel
        workers don't fire two token_refresh RPCs at once. The second
        caller will see the fresh token after the first finishes.
        """
        refresh_token = getattr(self.session, "refresh_token", None)
        if not refresh_token:
            _tlog("force_refresh: no refresh_token on session")
            return False
        try:
            # _refresh_once takes _refresh_lock and single-flights: if a
            # parallel refresh already rotated the token, it reuses that
            # result instead of re-POSTing this now-stale one. It also
            # persists on success, so there's no save_session() here.
            ok = self._refresh_once(based_on=refresh_token)
        except Exception as exc:
            _tlog(f"force_refresh: token_refresh raised: {exc!r}")
            # AuthenticationError means the refresh token itself is
            # dead. Blow the session away so the user is prompted to
            # log in again rather than chasing 401s forever, and tell
            # the server to drop its cached auth state so the very
            # next /auth/status bounces the UI to Login immediately
            # instead of waiting out the cache TTL.
            if type(exc).__name__ == "AuthenticationError":
                try:
                    self.logout()
                except Exception:
                    pass
                self._notify_auth_lost()
            return False
        if not ok:
            _tlog("force_refresh: token_refresh returned False")
            return False
        _tlog("force_refresh: success")
        return True

    def _refresh_watchdog(self) -> None:
        """Background loop that refreshes the token before it expires.

        Sleeps for _REFRESH_CHECK_INTERVAL_SEC between checks. Each tick:
        if we're logged in and the stored expiry is within the window,
        fire force_refresh. Errors inside force_refresh are already
        logged there; the watchdog swallows its own errors so a transient
        network blip doesn't kill the thread.
        """
        while not self._refresh_stop.is_set():
            # Wake up early if someone signals stop (e.g. tests).
            if self._refresh_stop.wait(self._REFRESH_CHECK_INTERVAL_SEC):
                return
            try:
                expiry = getattr(self.session, "expiry_time", None)
                refresh_token = getattr(self.session, "refresh_token", None)
                if not expiry or not refresh_token:
                    continue
                now = (
                    datetime.now(expiry.tzinfo)
                    if getattr(expiry, "tzinfo", None)
                    else datetime.now()
                )
                seconds_left = (expiry - now).total_seconds()
                if seconds_left > self._REFRESH_WINDOW_SEC:
                    continue
                _tlog(
                    f"refresh watchdog: token expires in "
                    f"{seconds_left:.0f}s — refreshing"
                )
                self.force_refresh()
            except Exception as exc:
                _tlog(f"refresh watchdog: ignoring error: {exc!r}")

    def get_max_quality(self) -> Optional[str]:
        """Return the highest audio quality the logged-in account is
        allowed to stream, as a tidalapi Quality member name (e.g.
        'high_lossless'). None means the check failed or the user isn't
        logged in yet — caller should assume no filtering in that case.

        The subscription endpoint is the only source of truth for this;
        tidalapi itself doesn't surface the tier. Cached for _SUB_TTL_SEC
        so the frontend hitting /api/qualities on every mount doesn't
        fan out to a network call each time.
        """
        import sys as _sys

        now = time.monotonic()
        with self._sub_lock:
            cached_at, cached_val = self._sub_cache
            if now - cached_at < _SUB_TTL_SEC and cached_val is not None:
                return cached_val
        try:
            user = getattr(self.session, "user", None)
            uid = getattr(user, "id", None) if user else None
            if not uid:
                print(
                    "[tidal] get_max_quality: no user.id yet",
                    file=_sys.stderr,
                    flush=True,
                )
                return None
            resp = self.session.request.basic_request(
                "GET", f"users/{uid}/subscription"
            )
            if not resp.ok:
                print(
                    f"[tidal] get_max_quality: subscription fetch returned "
                    f"{resp.status_code}",
                    file=_sys.stderr,
                    flush=True,
                )
                return None
            data = resp.json()
        except Exception as exc:
            print(
                f"[tidal] get_max_quality: subscription fetch raised: {exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            return None
        # Tidal exposes the ceiling under `highestSoundQuality`. Some
        # older/newer responses nest it under `subscription.type` —
        # check both and map to our internal code.
        hsq = (
            data.get("highestSoundQuality")
            or (data.get("subscription") or {}).get("highestSoundQuality")
            or (data.get("subscription") or {}).get("type")
            or ""
        )
        mapped = _SUB_QUALITY_MAP.get(str(hsq).upper())
        # Cap at the client's ceiling. A device-code session with a Max
        # subscription still can't stream hi-res — the client_id is the
        # gate, not the subscription. Clamp to _DEVICE_CODE_MAX in that
        # case so the UI never offers Max to a session that can't
        # deliver it.
        is_pkce = bool(getattr(self.session, "is_pkce", False))
        capped = mapped
        if mapped and not is_pkce:
            try:
                if (
                    _SUB_QUALITY_ORDER.index(mapped)
                    > _SUB_QUALITY_ORDER.index(_DEVICE_CODE_MAX)
                ):
                    capped = _DEVICE_CODE_MAX
            except ValueError:
                pass
        print(
            f"[tidal] get_max_quality: raw={hsq!r} mapped={mapped!r} "
            f"is_pkce={is_pkce} capped={capped!r} "
            f"response_keys={sorted(data.keys())}",
            file=_sys.stderr,
            flush=True,
        )
        mapped = capped
        if not mapped:
            return None
        with self._sub_lock:
            self._sub_cache = (now, mapped)
        return mapped

    def clamp_quality_to_subscription(
        self, requested: Optional[str]
    ) -> Optional[str]:
        """Downgrade `requested` to the highest tier the account can
        actually stream. Callers shove this in front of any code path
        that sets `session.config.quality` before a stream / download
        fetch — without it, picking e.g. "hi_res_lossless" on a HiFi
        account generates inevitable 401s from Tidal's playbackinfo
        endpoint. When the subscription lookup fails (network / stale
        token), pass the value through unchanged — better a 401 than
        silently downgrading the user for a transient lookup miss.
        """
        if not requested:
            return requested
        max_q = self.get_max_quality()
        if not max_q:
            return requested
        try:
            req_idx = _SUB_QUALITY_ORDER.index(requested)
            max_idx = _SUB_QUALITY_ORDER.index(max_q)
        except ValueError:
            return requested
        if req_idx <= max_idx:
            return requested
        return max_q

    def save_session(self):
        """Persist the session atomically and with 0600 perms.

        A crash between truncating the file and json.dump finishing would
        otherwise leave a zero-byte `tidal_session.json` and silently log
        the user out. We also chmod the file to user-only (the access +
        refresh tokens sit in plaintext) — otherwise on a shared Unix box
        every other user can read them.
        """
        expiry = self.session.expiry_time
        data = {
            "token_type": self.session.token_type,
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "expiry_time": expiry.isoformat() if expiry else None,
            # Track PKCE sessions separately — their client_id/secret
            # need to be re-swapped to the hi-res-entitled pair after
            # load_oauth_session, otherwise subsequent stream requests
            # silently drop back to Lossless even if the tokens are
            # valid.
            "is_pkce": bool(getattr(self.session, "is_pkce", False)),
        }
        target = SESSION_FILE
        # Write to a sibling tempfile in the same dir so os.replace stays
        # atomic (rename across filesystems isn't).
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tidal_session.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f)
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                # chmod is best-effort on Windows / exotic filesystems.
                pass
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def logout(self):
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        config = tidalapi.Config(quality=tidalapi.Quality.high_lossless)
        config.client_id = _DEVICE_CODE_CLIENT_ID
        config.client_secret = _DEVICE_CODE_CLIENT_SECRET
        self.session = tidalapi.Session(config)
        _install_tidal_request_gate(self.session)
        _swap_to_impersonated_transport(self.session)
        # logout() builds a fresh Session, so re-shadow token_refresh
        # or the next login's session would fall back to tidalapi's
        # rotation-dropping native implementation.
        self._install_capturing_refresh()
        # The subscription-tier cache belongs to the previous session.
        # Bust it so the next login re-detects under the new client_id.
        with self._sub_lock:
            self._sub_cache = (0.0, None)
        # The cached PKCE URL holds the OLD session's verifier. Without
        # clearing it, the next login returns a stale URL for up to 10 min
        # and its code exchange fails because the verifier is gone.
        self._pkce_url_cache = None

    # ------------------------------------------------------------------
    # OAuth login
    # ------------------------------------------------------------------

    def start_oauth_login(self) -> Tuple[str, str, Future]:
        """Initiate device-code OAuth. Returns (url, user_code, future).
        The future resolves when the user completes browser auth.

        Force the device-code client_id back into the config first.
        If a previous PKCE session was loaded at boot,
        `client_enable_hires` will have swapped `config.client_id` to
        the PKCE client — which Tidal rejects on the device-code
        endpoint with "Client is not a Limited Input Device client".
        """
        self.session.config.client_id = _DEVICE_CODE_CLIENT_ID
        self.session.config.client_secret = _DEVICE_CODE_CLIENT_SECRET
        login, future = self.session.login_oauth()
        self._login_future = future
        url = f"https://{login.verification_uri_complete}"
        return url, login.user_code, future

    def complete_login(self, future: Future, timeout: int = 300) -> bool:
        """Block until the OAuth future resolves, then save session. Returns success."""
        try:
            future.result(timeout=timeout)
            if self.session.check_login():
                self.save_session()
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # PKCE login — required for hi-res (Max) downloads.
    #
    # Tidal caps the device-code OAuth `client_id` at Lossless regardless
    # of subscription. Only the PKCE flow (which uses a different
    # `client_id_pkce` baked into Tidal's own mobile app) is entitled to
    # stream hi-res FLAC. The tradeoff is UX: there's no device-code
    # handoff — the user logs in in a browser, gets redirected to an
    # "Oops" page (Tidal has no handler for our redirect), copies the
    # URL, and pastes it back to us.
    # ------------------------------------------------------------------

    def pkce_login_url(self) -> str:
        """URL the user should open in a browser to begin PKCE login.

        Cached for 10 minutes after generation. tidalapi's
        `session.pkce_login_url()` rotates the PKCE verifier on every
        call — if the frontend fetches the URL twice (React strict-
        mode double-render, a remount after coming back from Safari,
        a reconnect), the second call overwrites the first verifier,
        and the code the user pastes back (which was bound to the
        first verifier) fails to exchange with a 401. Caching the
        URL keeps the verifier stable across repeated fetches while
        one login attempt is in progress.
        """
        cached = getattr(self, "_pkce_url_cache", None)
        if cached is not None:
            url, generated_at = cached
            if time.time() - generated_at < 600:  # 10 min
                return url
        url = self.session.pkce_login_url()
        self._pkce_url_cache = (url, time.time())
        return url

    def complete_pkce_login(self, redirect_url: str) -> tuple[bool, Optional[str]]:
        """Exchange the pasted 'Oops' redirect URL for access tokens,
        enable the hi-res client_id, and persist the session. Returns
        `(True, None)` on success, `(False, reason)` on failure so the
        API layer can surface a concrete error to the user.
        """
        # Respect any active backoff — pkce_get_auth_token hits
        # auth.tidal.com/v1/oauth2/token via its own requests.post so
        # the module-level request gate doesn't cover it. Refusing
        # here stops "Continue" spam from compounding a suspension.
        state = tidal_backoff_state()
        if state["active"]:
            return False, (
                f"Tidal is holding us off for another "
                f"{int(state['seconds_remaining'])}s ({state['reason']})"
            )
        try:
            token = self.session.pkce_get_auth_token(redirect_url)
            self.session.process_auth_token(token, is_pkce_token=True)
            # Defensive: make sure is_pkce actually sticks on the session
            # so save_session() writes is_pkce=True. If tidalapi's
            # process_auth_token ever stops setting this attribute, the
            # reloaded session would silently fall back to the non-hi-res
            # client_id on next restart.
            self.session.is_pkce = True
            # Swap the active client_id/secret to the hi-res-entitled
            # PKCE pair so subsequent API calls use the credentials that
            # actually unlock Max quality streams.
            self.session.client_enable_hires()
            if self.session.check_login():
                self.save_session()
                # Bust the subscription cache — it may have been
                # populated earlier under a device-code session that
                # reported a Lossless ceiling; the PKCE session
                # should re-detect at Max.
                with self._sub_lock:
                    self._sub_cache = (0.0, None)
                # Clear the cached PKCE URL so a subsequent logout +
                # re-login generates a fresh verifier.
                self._pkce_url_cache = None
                return True, None
            return False, "check_login returned False"
        except Exception as exc:
            import sys as _sys
            # If tidalapi's requests call raised, pull the response
            # body out so we can see Tidal's actual error payload
            # ("invalid_grant", "invalid_client", code-already-used,
            # etc.) instead of just the 403 status.
            resp_body = ""
            resp = getattr(exc, "response", None)
            status_code = None
            if resp is not None:
                status_code = getattr(resp, "status_code", None)
                try:
                    resp_body = resp.text[:500]
                except Exception:
                    pass
            # tidalapi's pkce_get_auth_token uses its own requests.post
            # so the session-level gate never sees the response. Run
            # the shared classifier manually to keep the two paths in
            # lockstep.
            _classify_tidal_error(status_code, resp_body)
            print(
                f"[tidal] complete_pkce_login failed: {exc!r}\n"
                f"  redirect_url: {redirect_url[:200]}\n"
                f"  response_body: {resp_body}",
                file=_sys.stderr,
                flush=True,
            )
            friendly = _friendly_pkce_error(exc)
            if friendly:
                return False, friendly
            detail = f"{type(exc).__name__}: {exc}"
            if resp_body:
                detail = f"{detail} | body: {resp_body}"
            return False, detail

    # ------------------------------------------------------------------
    # User info
    # ------------------------------------------------------------------

    def get_user_info(self) -> Optional[str]:
        try:
            user = self.session.user
            for attr in ("username", "first_name", "email"):
                value = getattr(user, attr, None)
                if value:
                    return str(value)
            return "Tidal User"
        except Exception:
            return None

    def get_user_avatar_url(self) -> Optional[str]:
        """Tidal exposes a per-user `picture_id` on FetchedUser; most
        accounts don't set one. Returns a CDN URL (210×210) if available,
        or None — callers should fall back to initials.
        """
        try:
            user = self.session.user
            img = getattr(user, "image", None)
            if callable(img):
                return img(210)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # URL parsing and content fetching
    # ------------------------------------------------------------------

    def parse_url(self, url: str) -> Tuple[str, str]:
        """Return (content_type, id_string) for a Tidal URL."""
        patterns = {
            "track": r"/track/(\d+)",
            "album": r"/album/(\d+)",
            "playlist": r"/playlist/([\w\-]+)",
        }
        for content_type, pattern in patterns.items():
            m = re.search(pattern, url)
            if m:
                return content_type, m.group(1)
        raise ValueError(f"Unrecognized Tidal URL: {url}")

    def fetch_url(self, url: str):
        """Fetch track, album, or playlist object from a Tidal URL.
        Returns (content_type, object)."""
        content_type, content_id = self.parse_url(url)
        if content_type == "track":
            return content_type, self.session.track(int(content_id))
        if content_type == "album":
            return content_type, self.session.album(int(content_id))
        if content_type == "playlist":
            return content_type, self.session.playlist(content_id)
        raise ValueError(f"Unsupported content type: {content_type}")

    def get_track_url(self, track) -> Optional[str]:
        try:
            return track.get_url()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 25) -> dict:
        """Returns dict with keys: tracks, albums, artists, playlists."""
        models = [tidalapi.Track, tidalapi.Album, tidalapi.Artist, tidalapi.Playlist]
        return self.session.search(query, models=models, limit=limit)

    # ------------------------------------------------------------------
    # Library / favorites
    # ------------------------------------------------------------------

    def _favorites_sorted(self, method_name: str) -> list:
        """Fetch every favorite of the given type, paging through the API if
        necessary, then sort newest-first."""
        try:
            method = getattr(self.session.user.favorites, method_name)
        except Exception:
            return []

        items = _fetch_all_pages(method)
        return _sort_by_date_added(items)

    def get_favorite_artists(self) -> list:
        return self._favorites_sorted("artists")

    def get_favorite_tracks(self) -> list:
        return self._favorites_sorted("tracks")

    def get_favorite_albums(self) -> list:
        return self._favorites_sorted("albums")

    def get_favorite_playlists(self) -> list:
        return self._favorites_sorted("playlists")

    def get_user_playlists(self) -> list:
        items = _fetch_all_pages(self.session.user.playlists)
        return _sort_by_date_added(items)

    def get_artist_albums(self, artist) -> list:
        try:
            return list(artist.get_albums())
        except Exception:
            return []

    def get_artist_releases(self, artist, limit: int = 30) -> list:
        """Albums + EPs + singles for an artist, combined. `get_albums()`
        alone excludes singles, which is exactly the content the feed
        page needs (singles drop more frequently than albums). Results
        are deduped by id, sorted nothing — the caller sorts by date."""
        out: list = []
        seen: set = set()
        for fn in (
            lambda: artist.get_albums(limit=limit),
            lambda: artist.get_ep_singles(limit=limit),
        ):
            try:
                for item in fn():
                    key = str(getattr(item, "id", "") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    out.append(item)
            except Exception:
                continue
        return out

    def get_artist_top_tracks(self, artist) -> list:
        """Top tracks for an artist, with one retry and an album-tracks
        fallback. Tidal's get_top_tracks is intermittently flaky for
        very popular artists (Travis Scott, Drake, Taylor Swift in
        casual testing) — the request succeeds but returns an empty
        list ~30% of the time. Empty top tracks cascade into broken
        Spotify monthly-listeners on the artist page (no ISRC to
        pivot on), so we retry once, then fall back to walking the
        first couple of albums for tracks if the API still gives us
        nothing.
        """
        for _ in range(2):
            try:
                tracks = list(artist.get_top_tracks(limit=10))
                if tracks:
                    return tracks
            except Exception:
                pass

        # Fallback: pull tracks off the first couple of releases.
        # Anything with an ISRC works for the artist-resolve pivot
        # downstream, and a couple of album cuts is plenty.
        try:
            albums = list(artist.get_albums(limit=2))
        except Exception:
            albums = []
        out: list = []
        for alb in albums:
            try:
                out.extend(list(alb.tracks())[:5])
            except Exception:
                continue
            if len(out) >= 10:
                break
        return out

    def get_album_tracks(self, album) -> list:
        try:
            return list(album.tracks())
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Writes (favorites + playlists)
    # ------------------------------------------------------------------

    @property
    def user_id(self) -> Optional[str]:
        try:
            uid = getattr(self.session.user, "id", None)
            return str(uid) if uid is not None else None
        except Exception:
            return None

    def favorite(self, kind: str, obj_id: str, add: bool) -> None:
        """Add or remove a favorite. `kind` in {track, album, artist, playlist, mix}."""
        favs = self.session.user.favorites
        # tidalapi's mix helpers are plural (add_mixes / remove_mixes) and
        # accept a list of string ids, while the other kinds are singular
        # and expect an int (or a uuid string for playlist).
        if kind == "mix":
            method_name = ("add_" if add else "remove_") + "mixes"
            method = getattr(favs, method_name, None)
            if method is None:
                raise ValueError("Unsupported favorite kind: mix")
            method([str(obj_id)])
            return
        method_name = ("add_" if add else "remove_") + kind
        method = getattr(favs, method_name, None)
        if method is None:
            raise ValueError(f"Unsupported favorite kind: {kind}")
        # tidalapi: playlist uses uuid (str), others use int.
        arg: object = obj_id if kind == "playlist" else int(obj_id)
        method(arg)

    def favorites_snapshot(self) -> dict:
        """Return sets of favorite IDs the UI needs to render heart states."""
        result = {"tracks": [], "albums": [], "artists": [], "playlists": [], "mixes": []}
        for kind, attr in (
            ("tracks", "get_favorite_tracks"),
            ("albums", "get_favorite_albums"),
            ("artists", "get_favorite_artists"),
            ("playlists", "get_favorite_playlists"),
        ):
            try:
                items = getattr(self, attr)()
                result[kind] = [
                    str(getattr(i, "id", "") or getattr(i, "uuid", "")) for i in items
                ]
            except Exception:
                continue
        # Mixes live under session.user.mixes() in tidalapi rather than
        # self.get_favorite_* like the other kinds.
        try:
            mixes = self.session.user.mixes(limit=200)
            result["mixes"] = [str(getattr(m, "id", "") or "") for m in mixes if getattr(m, "id", "")]
        except Exception:
            pass
        return result

    def create_playlist(self, title: str, description: str = ""):
        return self.session.user.create_playlist(title, description)

    def owns_playlist(self, playlist) -> bool:
        try:
            creator = getattr(playlist, "creator", None)
            if creator is None:
                return False
            return str(getattr(creator, "id", "")) == (self.user_id or "")
        except Exception:
            return False
