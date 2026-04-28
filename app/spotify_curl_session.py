"""curl-cffi-backed transport for spotapi.

spotapi's protocol layer (TOTP, JS-bundle hash scraping, GraphQL
operation framing) is solid and worth keeping. What spotapi shipped
*as the transport* — `tls_client` 1.0.1, a Python wrapper around an
unmaintained Go-CGO DLL (`tls-client-64.dll`) — is what crashed the
Windows app: a Go runtime panic at offset 0x66621 takes the whole
process down on the first Spotify enrichment call. Python `try/except`
can't catch a native crash.

`curl_cffi` is the TLS-impersonating HTTP transport the rest of
Tideway already uses for tidalapi (see `app/http.py`), and its
maintainer ships matching ClientHello + HTTP/2 SETTINGS profiles for
real Chrome versions. Pointing spotapi at it removes the broken
native dep entirely without giving up the upstream protocol code.

`CurlSpotifyClient` is a duck-typed substitute for spotapi's
`TLSClient`: spotapi's `BaseClient` only touches the transport via
`headers.update()`, `cookies.get("sp_t")`, `client_identifier`,
`get`, `post`, `put`, `authenticate`, and `close` — all of which
this adapter exposes. spotapi's `Response` dataclass is reused so
the response shape `BaseClient` compares against (`.fail`,
`.error.string`, `.response`, `.status_code`) is identical.

Import order matters: `install_tls_client_stubs()` MUST run before
any `import spotapi…` because spotapi's `http/data.py` and
`http/request.py` unconditionally `from tls_client…` at module top
level. We install the stubs at this module's load time so any code
that does `from app.spotify_curl_session import CurlSpotifyClient`
is automatically safe regardless of which module imports first.
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any, Callable, Optional


def install_spotapi_dep_stubs() -> None:
    """Register no-op stubs for the spotapi imports we don't use.

    Three categories of stubs land here:

    1. `tls_client` — spotapi/http/data.py and http/request.py
       unconditionally `from tls_client...` at module top level. The
       real `tls_client/__init__.py` loads `tls-client-64.dll` via
       ctypes, and that DLL panics with STATUS_BREAKPOINT at offset
       0x66621 on Windows the first time spotapi makes a request.
       Stubbing it means we keep spotapi's protocol code (TOTP,
       hash scraping, GraphQL framing) without ever loading the
       broken transport. The actual HTTP work is done by
       `CurlSpotifyClient` (curl-cffi).

    2. `pymongo` — spotapi/utils/saver.py has unconditional
       `import pymongo` for the `MongoSaver` class we never use.

    3. `redis` — same story as pymongo, for `RedisSaver`.

    Empty `types.ModuleType` objects satisfy the import lines and
    keep the unused libraries (~15+ MB combined) out of the install.

    Idempotent — once installed, subsequent calls are no-ops.
    """
    existing = sys.modules.get("tls_client")
    if existing is not None and getattr(existing, "_tideway_stub", False):
        return

    for name in ("pymongo", "redis"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    class _FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
            pass

        def close(self) -> None:
            pass

    class _FakeResponse:  # spotapi uses this only as a type annotation
        pass

    class _FakeException(Exception):
        pass

    tls_root = types.ModuleType("tls_client")
    tls_root.Session = _FakeSession  # type: ignore[attr-defined]
    tls_root._tideway_stub = True  # type: ignore[attr-defined]

    tls_response = types.ModuleType("tls_client.response")
    tls_response.Response = _FakeResponse  # type: ignore[attr-defined]

    tls_settings = types.ModuleType("tls_client.settings")
    # spotapi's request.py uses ClientIdentifiers as a type annotation
    # only. `str` satisfies that at both import time and the @enforce
    # check time, since callers pass strings like "chrome_131".
    tls_settings.ClientIdentifiers = str  # type: ignore[attr-defined]

    tls_exceptions = types.ModuleType("tls_client.exceptions")
    tls_exceptions.TLSClientExeption = _FakeException  # type: ignore[attr-defined]  # noqa: E501

    sys.modules["tls_client"] = tls_root
    sys.modules["tls_client.response"] = tls_response
    sys.modules["tls_client.settings"] = tls_settings
    sys.modules["tls_client.exceptions"] = tls_exceptions


# Install stubs BEFORE the spotapi import below — otherwise this very
# import line would load the broken DLL we're trying to avoid AND
# blow up on the missing pymongo/redis dependencies.
install_spotapi_dep_stubs()

from spotapi.http.data import Response as _SpotapiResponse  # noqa: E402

# curl-cffi exposes a Chrome 131 desktop profile; that's the closest
# match to what spotapi's BaseClient announces in its User-Agent
# header (Chrome <browser_version>.0.0.0). Keeping the TLS handshake
# and the UA string on the same Chrome major-version family is what
# makes the impersonation coherent — Spotify's anti-bot stack flags
# the mismatch when one says Chrome 120 and the other says Firefox.
_DEFAULT_PROFILE = "chrome131"

# spotapi's `client_identifier` is a string like "chrome_120" whose
# digit suffix BaseClient parses as the Chrome major version for the
# User-Agent / Sec-Ch-Ua headers. We expose 131 so the headers
# spotapi emits match the curl-cffi impersonation profile we use.
_CLIENT_IDENTIFIER = "chrome_131"


def _looks_like_json(body: Any) -> bool:
    if not isinstance(body, str):
        return False
    s = body.lstrip()
    return s.startswith("{") or s.startswith("[")


class CurlSpotifyClient:
    """curl-cffi transport that quacks like `spotapi.http.request.TLSClient`.

    The interface is whatever spotapi's `BaseClient` and its callers
    in `spotapi.song.Song` / `spotapi.artist.Artist` actually touch —
    not the full TLSClient surface. Keep it minimal so we're not
    chasing spotapi internals we don't use.
    """

    def __init__(
        self,
        profile: str = _CLIENT_IDENTIFIER,
        proxy: str = "",
        *,
        auto_retries: int = 2,
    ) -> None:
        try:
            from curl_cffi.requests import Session as _CurlSession
        except Exception as exc:  # pragma: no cover — bundling guard
            raise RuntimeError(
                "curl_cffi is required for the Spotify transport but is not "
                f"importable: {exc!r}"
            ) from exc

        # client_identifier is a cosmetic field BaseClient reads to
        # build its User-Agent. The real impersonation comes from the
        # curl-cffi profile name.
        self.client_identifier = profile
        self._session = _CurlSession(impersonate=_DEFAULT_PROFILE)

        if proxy:
            # spotapi accepts a bare "host:port" and prefixes "http://".
            self._session.proxies = {
                "http": f"http://{proxy}",
                "https": f"http://{proxy}",
            }

        # Forward header / cookie state directly from the curl-cffi
        # session. spotapi mutates these in place.
        self.headers = self._session.headers
        self.cookies = self._session.cookies

        # +1 because spotapi's accounting treats `auto_retries` as
        # "retries on top of the first attempt" — same semantics as
        # the original TLSClient.
        self._max_attempts = max(1, int(auto_retries) + 1)

        # spotapi assigns to .authenticate after construction with a
        # callable that mutates the kwargs dict before each request.
        self.authenticate: Optional[Callable[[dict], dict]] = None

    # -- HTTP verbs --------------------------------------------------

    def get(
        self,
        url: str,
        *,
        authenticate: bool = False,
        **kwargs: Any,
    ) -> _SpotapiResponse:
        return self._do("GET", url, authenticate=authenticate, **kwargs)

    def post(
        self,
        url: str,
        *,
        authenticate: bool = False,
        **kwargs: Any,
    ) -> _SpotapiResponse:
        return self._do("POST", url, authenticate=authenticate, **kwargs)

    def put(
        self,
        url: str,
        *,
        authenticate: bool = False,
        **kwargs: Any,
    ) -> _SpotapiResponse:
        return self._do("PUT", url, authenticate=authenticate, **kwargs)

    # -- internals ---------------------------------------------------

    def _do(
        self,
        method: str,
        url: str,
        *,
        authenticate: bool,
        **kwargs: Any,
    ) -> _SpotapiResponse:
        if authenticate and self.authenticate is not None:
            kwargs = self.authenticate(kwargs)

        # spotapi's TLSClient passes `allow_redirects=True` on get/
        # post; curl-cffi follows redirects by default and rejects
        # the kwarg name, so strip it.
        kwargs.pop("allow_redirects", None)

        last_exc: Optional[BaseException] = None
        resp = None
        for _ in range(self._max_attempts):
            try:
                resp = self._session.request(method, url, **kwargs)
                break
            except Exception as exc:
                # curl-cffi raises subclasses of RequestsError for
                # network / TLS / HTTP-level failures. Preserve the
                # last one so a fully-failed sequence carries
                # diagnostic context up the stack.
                last_exc = exc
                continue

        if resp is None:
            # All attempts failed. Hand back a Response with status 0
            # — spotapi treats anything outside [200, 302] as `.fail`
            # and propagates the error string into its own exception
            # types, so callers see the same shape they would have
            # from the legacy transport.
            err_str = repr(last_exc) if last_exc else "unknown"
            return _SpotapiResponse(
                raw=None,
                status_code=0,
                response=f"transport failed: {err_str}",
            )

        return self._build_response(resp)

    @staticmethod
    def _build_response(resp: Any) -> _SpotapiResponse:
        body: Any = resp.text
        ctype = (resp.headers.get("content-type") or "").lower()
        # Spotify's pathfinder endpoint is JSON but doesn't always set
        # content-type. spotapi's original transport does the same
        # heuristic — try parse on either signal.
        if "application/json" in ctype or _looks_like_json(body):
            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError):
                pass

        if not body:
            body = None

        return _SpotapiResponse(
            raw=resp,
            status_code=int(resp.status_code),
            response=body,
        )

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            # atexit-time cleanup; the interpreter is going down anyway.
            pass
