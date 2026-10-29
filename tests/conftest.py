"""Test-wide fixtures.

Imports anything non-trivial lazily inside fixtures so collection
doesn't fail for tests that don't need a fully-configured environment.
"""
import os
import shutil
import sys
import tempfile
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

_MEDIA_DIR = pytest.StashKey[Path]()


def pytest_configure(config):
    """Point the default download folders at a scratch dir for the run.

    Every TestClient lifespan startup in server.py deletes `*.part`
    files under settings.output_dir and starts a library scan of it,
    and a fresh Settings takes that folder from ~/Music/Tidal. So
    without this each run walked the developer's real music folder
    once per TestClient (close to a hundred times), opened every audio
    file in it to read tags, and deleted the partial file of any
    download the installed app had in flight. videos_dir is redirected
    alongside it for the same reason.

    server.py builds its Settings when it is imported, and test
    modules import it during collection, so this has to happen before
    collection rather than in a fixture. A hook rather than module-
    level code because two test modules import this file as
    `tests.conftest`, which runs its body a second time.
    """
    media_dir = Path(tempfile.mkdtemp(prefix="tideway-tests-media-"))
    config.stash[_MEDIA_DIR] = media_dir
    os.environ["TIDEWAY_OUTPUT_DIR"] = str(media_dir / "output")
    os.environ["TIDEWAY_VIDEOS_DIR"] = str(media_dir / "videos")


def pytest_unconfigure(config):
    """Delete the scratch media dir once the run is over. The
    overrides stay set on purpose, so anything still running after
    this falls back to the scratch path rather than the real folders."""
    shutil.rmtree(config.stash[_MEDIA_DIR])


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
