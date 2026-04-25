"""Tests for the curl-cffi TLS-impersonation install path.

This is one of the bigger ban-protection changes — we replace the
underlying HTTP session that tidalapi and the downloader use with a
curl-cffi session that impersonates a mobile-Chrome TLS ClientHello.
A regression here silently re-exposes the urllib3 fingerprint that
Tidal's anti-abuse layer can match on, so we cover both the success
path (curl-cffi installed → impersonated session in use) and the
fallback (curl-cffi import error → plain requests.Session, app keeps
working).
"""
import builtins
import importlib
import sys

import pytest


def _block_curl_cffi(monkeypatch) -> None:
    """Make any `import curl_cffi[.<sub>]` raise ImportError. Used by
    the fallback-path tests to simulate the package missing."""
    real_import = builtins.__import__

    def patched_import(name, *args, **kwargs):
        if name == "curl_cffi" or name.startswith("curl_cffi."):
            raise ImportError("simulated absent curl-cffi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", patched_import)


def test_app_http_session_uses_curl_cffi_when_available():
    """Default install: curl-cffi is present, SESSION is one of its
    Session objects."""
    import app.http as http_mod

    assert type(http_mod.SESSION).__module__.startswith("curl_cffi"), (
        f"expected curl_cffi-flavored SESSION, got {type(http_mod.SESSION)!r}"
    )


def test_app_http_session_falls_back_when_curl_cffi_missing(monkeypatch):
    """Simulate curl-cffi being unimportable. The module must still
    expose a working SESSION (now plain requests) so the app boots."""
    _block_curl_cffi(monkeypatch)

    sys.modules.pop("app.http", None)
    try:
        http_mod = importlib.import_module("app.http")
        assert type(http_mod.SESSION).__module__.startswith("requests"), (
            f"expected requests-flavored SESSION fallback, got "
            f"{type(http_mod.SESSION)!r}"
        )
    finally:
        # Restore the real curl-cffi-backed SESSION for subsequent tests.
        sys.modules.pop("app.http", None)
        importlib.import_module("app.http")


def test_curl_cffi_session_post_accepts_positional_data():
    """tidalapi's pkce_get_auth_token calls
    `self.request_session.post(url, data)` — passes the body as a
    second POSITIONAL argument. curl-cffi's native Session.post is
    keyword-only and would raise TypeError without the requests-
    compatible shim. Regression guard for the login-broken-after-TLS-
    fingerprint-swap bug."""
    import inspect

    import app.http  # noqa: F401  ensure shim is applied

    try:
        from curl_cffi.requests.session import Session as CurlSession
    except Exception:
        pytest.skip("curl-cffi not available")

    sig = inspect.signature(CurlSession.post)
    params = list(sig.parameters.values())
    # (self, url, data, **kwargs)
    assert len(params) >= 3
    assert params[1].name == "url"
    assert params[2].name == "data"
    # data must be a positional-or-keyword param so the second
    # positional call site keeps working.
    assert params[2].kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    )


def test_curl_cffi_response_supports_with_statement():
    """The downloader uses `with SESSION.get(...) as resp:` extensively.
    curl-cffi's Response doesn't ship a context manager out of the
    box; we patch one in. If that shim regresses, every download
    breaks at runtime."""
    import app.http  # noqa: F401  ensure shim is applied

    try:
        from curl_cffi.requests.models import Response as CurlResp
    except Exception:
        pytest.skip("curl-cffi not available")

    assert hasattr(CurlResp, "__enter__")
    assert hasattr(CurlResp, "__exit__")


def test_swap_keeps_session_when_curl_cffi_fails(monkeypatch):
    """If curl-cffi import fails, _swap_to_impersonated_transport
    must leave the session's request_session intact (still a
    requests.Session) rather than swap it for None or crash."""
    from app import tidal_client

    _block_curl_cffi(monkeypatch)
    # Force a fresh import of app.http so its build_impersonated_session
    # sees the blocked import.
    sys.modules.pop("app.http", None)

    try:
        import tidalapi
        s = tidalapi.Session(tidalapi.Config())
        original_session = s.request_session

        tidal_client._swap_to_impersonated_transport(s)

        assert s.request_session is original_session
    finally:
        # Restore real app.http SESSION for subsequent tests.
        sys.modules.pop("app.http", None)
        importlib.import_module("app.http")


def test_swap_replaces_session_when_curl_cffi_available():
    """Default path: curl-cffi import succeeds, session.request_session
    is replaced with a curl-cffi session."""
    from app import tidal_client

    try:
        import curl_cffi  # noqa: F401
    except Exception:
        pytest.skip("curl-cffi not available")

    import tidalapi
    s = tidalapi.Session(tidalapi.Config())
    tidal_client._swap_to_impersonated_transport(s)

    assert type(s.request_session).__module__.startswith("curl_cffi"), (
        f"expected curl_cffi session, got {type(s.request_session)!r}"
    )
