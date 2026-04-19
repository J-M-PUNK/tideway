"""Shared HTTP session for connection reuse across the app.

A single requests.Session is used everywhere (Tidal API-adjacent calls,
image cache, stream downloads) so we keep TLS connections warm instead
of paying a handshake on every request. Retries are modest: two quick
tries on 5xx / connection errors for GETs, which is almost always what
saves you from a transient CDN blip mid-download.
"""
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SESSION: Session = Session()

_retry = Retry(
    total=2,
    backoff_factor=0.5,
    status_forcelist=(502, 503, 504),
    allowed_methods=frozenset(["GET", "HEAD"]),
    raise_on_status=False,
)

# Generous pool sizes — we fire off many parallel image + download requests.
_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=_retry)
SESSION.mount("http://", _adapter)
SESSION.mount("https://", _adapter)
