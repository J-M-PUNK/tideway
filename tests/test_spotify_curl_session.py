"""Tests for the curl-cffi-backed spotapi transport.

What this pins:

1. Importing `app.spotify_curl_session` does NOT load the real
   `tls_client` package. The Go-CGO DLL behind the legacy transport
   panics on Windows; the stubs here are what keep it out of the
   running process.

2. The adapter exposes the same shape spotapi's `BaseClient` calls:
   `headers`, `cookies`, `client_identifier`, `get`/`post`/`put` with
   `authenticate=True`, `close`, and assignable `.authenticate`.
   Returns `spotapi.http.data.Response` instances so spotapi's
   downstream `.fail` / `.error.string` / `.response` checks behave
   identically to the original transport.

3. The retry loop counts attempts the way spotapi does (initial +
   `auto_retries` extras), and a fully-failed sequence returns a
   status-0 Response rather than raising — matching spotapi's
   contract that a transport never raises out of `.get()`/`.post()`.

These tests intentionally don't talk to Spotify. They verify the
adapter correctly wraps a stand-in `Session` that exposes the same
duck-typed interface curl-cffi's session does.
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest


def test_import_does_not_load_real_tls_client():
    """Re-import of the adapter installs sys.modules stubs in place
    of the real `tls_client` package. Confirming this prevents a
    silent regression where someone reorders imports or drops the
    `install_spotapi_dep_stubs()` call at module load time."""
    # Pre-existing imports from the test session may have left a real
    # tls_client in sys.modules if it was already installed in the
    # venv. Force a clean slate.
    for mod in list(sys.modules):
        if mod == "tls_client" or mod.startswith("tls_client."):
            del sys.modules[mod]
    # Drop the adapter so its module-load side effect runs fresh.
    sys.modules.pop("app.spotify_curl_session", None)

    import app.spotify_curl_session  # noqa: F401  — side-effect import

    stub = sys.modules.get("tls_client")
    assert stub is not None, "import didn't register a tls_client stub"
    assert getattr(stub, "_tideway_stub", False), (
        "sys.modules['tls_client'] is the real package, not our stub"
    )


@pytest.fixture
def mock_curl_session(monkeypatch):
    """Replace `curl_cffi.requests.Session` with a MagicMock for the
    duration of one test, then restore the real module entries.

    Uses `monkeypatch.setitem(sys.modules, ...)` so other tests that
    legitimately use curl-cffi (e.g. `tests/test_tls_impersonation.py`)
    don't see our fake `curl_cffi.requests` package and fail to find
    submodules like `curl_cffi.requests.models`.
    """
    mock_session = MagicMock(name="curl_cffi_Session")
    mock_session.headers = {}
    mock_session.cookies = MagicMock(name="cookies")
    mock_session.proxies = {}

    fake_curl_cffi = types.ModuleType("curl_cffi")
    fake_curl_cffi_requests = types.ModuleType("curl_cffi.requests")
    fake_curl_cffi_requests.Session = lambda **_: mock_session  # type: ignore[attr-defined]  # noqa: E501
    fake_curl_cffi.requests = fake_curl_cffi_requests  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_curl_cffi)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_curl_cffi_requests)

    return mock_session


def _fresh_adapter(mock_session, retries: int = 1):
    """Construct a CurlSpotifyClient backed by a fixture-provided
    fake curl-cffi Session. The fixture handles sys.modules cleanup."""
    from app import spotify_curl_session

    return spotify_curl_session.CurlSpotifyClient(auto_retries=retries), mock_session


def _ok(json_payload: Any, *, status_code: int = 200, ctype: str = "application/json"):
    resp = MagicMock(name="Response")
    resp.status_code = status_code
    resp.headers = {"content-type": ctype}
    resp.text = "" if json_payload is None else "{}"
    resp.json.return_value = json_payload
    return resp


def test_get_parses_json_response(mock_curl_session):
    adapter, sess = _fresh_adapter(mock_curl_session)
    sess.request.return_value = _ok({"hello": "world"})

    resp = adapter.get("https://example.com/api")

    assert resp.status_code == 200
    assert resp.success is True
    assert resp.fail is False
    assert resp.response == {"hello": "world"}
    sess.request.assert_called_once_with("GET", "https://example.com/api")


def test_post_passes_through_kwargs(mock_curl_session):
    """spotapi calls `.post(url, params=..., json=..., headers=...)`
    — every kwarg must reach curl-cffi unchanged so the GraphQL
    query string + body land correctly on Spotify."""
    adapter, sess = _fresh_adapter(mock_curl_session)
    sess.request.return_value = _ok({"ok": True})

    adapter.post(
        "https://api-partner.spotify.com/pathfinder/v1/query",
        params={"operationName": "getTrack"},
        json={"variables": {"uri": "spotify:track:abc"}},
        headers={"X-Test": "1"},
    )

    method, url = sess.request.call_args.args
    kwargs = sess.request.call_args.kwargs
    assert method == "POST"
    assert url == "https://api-partner.spotify.com/pathfinder/v1/query"
    assert kwargs["params"] == {"operationName": "getTrack"}
    assert kwargs["json"] == {"variables": {"uri": "spotify:track:abc"}}
    assert kwargs["headers"] == {"X-Test": "1"}


def test_authenticate_hook_runs_when_authenticate_true(mock_curl_session):
    """spotapi's BaseClient injects auth headers via `authenticate`.
    Setting it to True on a request must invoke the hook AND apply
    its kwarg mutations — otherwise GraphQL requests go without the
    Bearer / Client-Token headers and Spotify returns 401."""
    adapter, sess = _fresh_adapter(mock_curl_session)
    sess.request.return_value = _ok({})

    def _auth(kwargs: dict) -> dict:
        kwargs.setdefault("headers", {})
        kwargs["headers"]["Authorization"] = "Bearer test-token"
        return kwargs

    adapter.authenticate = _auth
    adapter.get("https://example.com/x", authenticate=True)

    sent_headers = sess.request.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer test-token"


def test_authenticate_hook_skipped_when_authenticate_false(mock_curl_session):
    """Pre-auth flow (homepage scrape, /api/token) MUST not run the
    auth hook — at that point there's no access_token to inject and
    the hook would recurse trying to fetch one."""
    adapter, sess = _fresh_adapter(mock_curl_session)
    sess.request.return_value = _ok({})
    auth_hook = MagicMock()
    adapter.authenticate = auth_hook

    adapter.get("https://open.spotify.com")

    auth_hook.assert_not_called()


def test_strips_allow_redirects_kwarg(mock_curl_session):
    """spotapi's TLSClient passes `allow_redirects=True` on get/post.
    curl-cffi's request() rejects unknown kwargs, so the adapter has
    to drop it. If this regresses, every spotapi call would raise a
    TypeError before reaching the network."""
    adapter, sess = _fresh_adapter(mock_curl_session)
    sess.request.return_value = _ok({})

    adapter.get("https://example.com/x", allow_redirects=True)

    assert "allow_redirects" not in sess.request.call_args.kwargs


def test_retries_on_exception_then_succeeds(mock_curl_session):
    adapter, sess = _fresh_adapter(mock_curl_session, retries=2)
    sess.request.side_effect = [
        ConnectionError("network blip"),
        _ok({"ok": True}),
    ]

    resp = adapter.get("https://example.com/x")

    assert resp.status_code == 200
    assert resp.response == {"ok": True}
    assert sess.request.call_count == 2


def test_returns_status_zero_when_all_attempts_fail(mock_curl_session):
    """A fully-failed retry loop must NOT raise — spotapi treats the
    transport as best-effort and inspects `.fail` / `.error.string`
    on the returned Response. Raising would surface a transport
    error to UI code that's wrapped in try/except expecting a
    soft failure path."""
    adapter, sess = _fresh_adapter(mock_curl_session, retries=2)
    sess.request.side_effect = ConnectionError("offline")

    resp = adapter.get("https://example.com/x")

    assert resp.status_code == 0
    assert resp.fail is True
    assert "offline" in str(resp.response)
    assert sess.request.call_count == 3  # initial + 2 retries


def test_parses_json_when_content_type_missing(mock_curl_session):
    """Spotify's pathfinder endpoint returns JSON without a
    Content-Type header sometimes. spotapi's original transport
    sniffed the body — preserve that behavior so the response shape
    arriving at spotapi is parsed dict, not a string."""
    adapter, sess = _fresh_adapter(mock_curl_session)
    resp = MagicMock(name="Response")
    resp.status_code = 200
    resp.headers = {}  # NO content-type
    resp.text = '{"data": {"trackUnion": {"playcount": 42}}}'
    resp.json.return_value = {"data": {"trackUnion": {"playcount": 42}}}
    sess.request.return_value = resp

    out = adapter.get("https://api-partner.spotify.com/pathfinder/v1/query")

    assert out.response == {"data": {"trackUnion": {"playcount": 42}}}


def test_empty_body_normalises_to_none(mock_curl_session):
    adapter, sess = _fresh_adapter(mock_curl_session)
    resp = MagicMock(name="Response")
    resp.status_code = 204
    resp.headers = {"content-type": "text/plain"}
    resp.text = ""
    sess.request.return_value = resp

    out = adapter.get("https://example.com/x")

    assert out.response is None


def test_close_swallows_errors(mock_curl_session):
    """atexit cleanup must never propagate. The interpreter is going
    down; logging a warning would just clutter the user's console."""
    adapter, sess = _fresh_adapter(mock_curl_session)
    sess.close.side_effect = RuntimeError("connection already gone")

    adapter.close()  # no raise

    sess.close.assert_called_once()


def test_client_identifier_matches_curl_profile_family(mock_curl_session):
    """spotapi's BaseClient parses the digit suffix of
    `client_identifier` and uses it in the User-Agent header. The
    adapter MUST report a Chrome major version that matches the
    curl-cffi profile we picked, otherwise the UA string and the
    TLS handshake claim different Chromes and Spotify's anti-bot
    flags the mismatch."""
    adapter, _ = _fresh_adapter(mock_curl_session)
    assert adapter.client_identifier.startswith("chrome_")
    digit_suffix = adapter.client_identifier.split("_", 1)[1]
    # Anything Chrome 100+ is fine — what matters is the family
    # match against the impersonate profile, which we hard-code in
    # the adapter (see _DEFAULT_PROFILE).
    assert digit_suffix.isdigit() and int(digit_suffix) >= 100


@pytest.mark.parametrize("method", ["get", "post", "put"])
def test_all_verbs_dispatch_correctly(method, mock_curl_session):
    adapter, sess = _fresh_adapter(mock_curl_session)
    sess.request.return_value = _ok({})

    getattr(adapter, method)("https://example.com/x")

    assert sess.request.call_args.args[0] == method.upper()
