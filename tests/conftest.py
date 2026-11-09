"""Test-wide fixtures.

Imports anything non-trivial lazily inside fixtures so collection
doesn't fail for tests that don't need a fully-configured environment.
"""
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Make the repo root importable as `app.*` etc. without requiring a
# `pip install -e .` — the project is run directly via `./run.sh`, not
# installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SCRATCH_ROOT = pytest.StashKey[Path]()


def pytest_configure(config):
    """Point the app's state and download folders at a scratch dir.

    The app resolves its state paths when a module is imported
    (audio.log's handler in app/audio/player.py, perf.log's in
    server.py, the settings file, the Tidal session, the download
    queue), and server.py builds its Settings at import too, and test
    modules import that code during collection, so this has to happen
    before collection rather than in a fixture. Without it every run
    loaded the developer's own settings and Tidal session, appended
    fake underflows, load stalls and HTTP 429 refreshes to their
    audio.log, where they read as real faults, and the logout test
    deleted their saved download queue. Every TestClient startup also
    deleted `*.part` files under settings.output_dir and scanned it, so
    each run walked the developer's real music folder close to a
    hundred times and could delete the partial file of a download the
    installed app had in flight.

    A hook rather than module-level code because two test modules
    import this file as `tests.conftest`, which runs its body a second
    time and would point the run at a second directory halfway through
    collection.
    """
    root = Path(tempfile.mkdtemp(prefix="tideway-tests-"))
    config.stash[_SCRATCH_ROOT] = root
    os.environ["TIDEWAY_DATA_DIR"] = str(root / "data")
    os.environ["TIDEWAY_OUTPUT_DIR"] = str(root / "output")
    os.environ["TIDEWAY_VIDEOS_DIR"] = str(root / "videos")


def pytest_unconfigure(config):
    """Delete the scratch dir once the run is over.

    audio.log and perf.log stay open on their RotatingFileHandlers for
    the life of the process, and Windows will not delete an open file,
    so close every handler writing into the dir first. The overrides
    stay set on purpose: anything still running after this recreates
    the scratch paths rather than reaching the real ones.
    """
    root = config.stash[_SCRATCH_ROOT]
    for logger in list(logging.Logger.manager.loggerDict.values()):
        for handler in list(getattr(logger, "handlers", ())):
            if isinstance(handler, logging.FileHandler) and Path(
                handler.baseFilename
            ).is_relative_to(root):
                logger.removeHandler(handler)
                handler.close()
    shutil.rmtree(root)


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
