"""Test-wide fixtures.

Imports anything non-trivial lazily inside fixtures so collection
doesn't fail for tests that don't need a fully-configured environment.
"""
import sys
from pathlib import Path

import pytest

# Make the repo root importable as `app.*` etc. without requiring a
# `pip install -e .` — the project is run directly via `./run.sh`, not
# installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _reset_tidal_backoff_state():
    """Clear the module-level Tidal backoff before and after every
    test so cross-test bleed (a 30-min backoff from one test tripping
    subsequent tests) can't happen. Cheap: two attribute writes on
    tests that don't touch the module."""
    try:
        from app import tidal_client
    except Exception:
        yield
        return
    tidal_client._tidal_backoff_until = 0.0
    tidal_client._tidal_backoff_reason = ""
    yield
    tidal_client._tidal_backoff_until = 0.0
    tidal_client._tidal_backoff_reason = ""


def known_to_curl_cffi(profile: str) -> bool:
    """Whether curl_cffi recognises this impersonate profile.

    Shared by the AOTY and Tidal profile guards. 0.15 answered this with
    `normalize_browser_type`, which 0.16 removed; the replacement is two
    lookups — REAL_TARGET_MAP for the aliases that resolve to a latest
    version ("chrome", "safari", "firefox"), and the BrowserType enum
    for concrete targets ("chrome131_android"). A typo is in neither,
    which is the case these guards exist to catch: an unknown profile
    does not raise at request time, it silently 403s.
    """
    from curl_cffi.requests.impersonate import BrowserType, REAL_TARGET_MAP

    if profile in REAL_TARGET_MAP:
        return True
    try:
        BrowserType(profile)
    except ValueError:
        return False
    return True
