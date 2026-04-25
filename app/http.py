"""Shared HTTP session for connection reuse across the app.

The downloader uses this for DASH segment fetches; tidalapi's API
session is patched to use the same impersonation. Both prefer a
curl-cffi session whose TLS ClientHello and HTTP/2 SETTINGS frames
match a real mobile-Chrome stack, so anti-abuse layers can't
fingerprint us at the handshake layer the way they can with default
urllib3. Falls back to a plain requests.Session if curl-cffi can't
be loaded.
"""
import sys

# Profile name is centralized here so a future Chrome bump only
# needs one edit. Picked an Android variant because tidalapi's
# user-agent claims to be the Tidal Android app.
_IMPERSONATE_PROFILE = "chrome131_android"


def build_impersonated_session():
    """Return a curl-cffi Session with the shared impersonation
    profile, or None if curl-cffi can't be loaded. Also installs the
    one-time __enter__/__exit__ shim on curl-cffi's Response so
    `with session.get(...) as resp:` callers keep working, and a
    requests-compatible positional-`data` shim on .post / .put so
    callers like tidalapi (which does `session.post(url, data)`)
    don't blow up — curl-cffi's signature is keyword-only and would
    otherwise reject the second positional argument.
    """
    try:
        from curl_cffi import requests as _curl_req
        from curl_cffi.requests.models import Response as _CurlResp
        from curl_cffi.requests.session import Session as _CurlSession
    except Exception as exc:
        print(
            f"[http] curl-cffi unavailable, falling back to urllib3: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return None
    if not hasattr(_CurlResp, "__enter__"):
        _CurlResp.__enter__ = lambda self: self
        _CurlResp.__exit__ = lambda self, *a: self.close()
    if not getattr(_CurlSession, "_tideway_post_compat", False):
        for _method_name in ("post", "put"):
            _orig = getattr(_CurlSession, _method_name)

            def _make_compat(orig):
                def _compat(self, url, data=None, **kwargs):
                    if data is not None:
                        kwargs.setdefault("data", data)
                    return orig(self, url, **kwargs)

                return _compat

            setattr(_CurlSession, _method_name, _make_compat(_orig))
        _CurlSession._tideway_post_compat = True
    try:
        return _curl_req.Session(impersonate=_IMPERSONATE_PROFILE)
    except Exception as exc:
        print(
            f"[http] curl-cffi session construction failed: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return None


def _build_requests_session():
    from requests import Session
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


SESSION = build_impersonated_session() or _build_requests_session()
