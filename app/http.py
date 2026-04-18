"""Shared HTTP session for connection reuse across the app.

A single requests.Session is used everywhere (Tidal API-adjacent calls,
image cache, stream downloads) so we keep TLS connections warm instead
of paying a handshake on every request.
"""
from requests import Session
from requests.adapters import HTTPAdapter

SESSION: Session = Session()

# Generous pool sizes — we fire off many parallel image + download requests.
_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=0)
SESSION.mount("http://", _adapter)
SESSION.mount("https://", _adapter)
