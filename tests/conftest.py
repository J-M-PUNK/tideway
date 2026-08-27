"""Test-wide fixtures.

Imports anything non-trivial lazily inside fixtures so collection
doesn't fail for tests that don't need a fully-configured environment.
"""
import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable as `app.*` etc. without requiring a
# `pip install -e .` — the project is run directly via `./run.sh`, not
# installed.
# Keep test runs out of the user's real data dir. server.py's perf
# logger writes to perf.log there, and the recs tests call
# _taste_profile() directly — so without this a suite run interleaves
# stub timings with whatever a real cold start recorded, and the file
# stops being usable as evidence. Set before server is ever imported,
# which is why it is module level rather than a fixture.
os.environ["TIDEWAY_NO_PERF_LOG"] = "1"

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
