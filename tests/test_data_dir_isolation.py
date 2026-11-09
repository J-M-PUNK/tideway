"""A test run never touches the user's real data dir.

tests/conftest.py points TIDEWAY_DATA_DIR at a scratch directory before
any test module is imported. These pin that the redirect lands early
enough to reach the paths the app resolves at import time. Both ways it
can go wrong have happened: suite runs appended stub timings to the
user's perf.log that were then read as real cold-start measurements,
and fake underflows, load stalls and rejected-session lines to their
audio.log, where they looked like genuine faults while crackle was
being triaged.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from app import paths


def _real_data_dir() -> Path:
    """Where user_data_dir() would point without the override."""
    return paths._app_data_root() / paths._APP_NAME


def test_user_data_dir_is_the_scratch_dir():
    scratch = Path(os.environ["TIDEWAY_DATA_DIR"])

    assert paths.user_data_dir() == scratch
    assert scratch != _real_data_dir()


def test_no_log_file_is_in_the_real_data_dir():
    import server  # noqa: F401 — attaches perf.log; player attaches audio.log

    real = _real_data_dir()
    # pytest attaches a FileHandler of its own (aimed at the null
    # device) to captured loggers, so check where each one writes
    # rather than whether there is one.
    writers = [
        f"{name}: {handler.baseFilename}"
        for name, logger in logging.Logger.manager.loggerDict.items()
        for handler in getattr(logger, "handlers", ())
        if isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename).is_relative_to(real)
    ]

    assert not writers, writers


def test_no_import_time_state_path_is_in_the_real_data_dir():
    import server  # noqa: F401 — imports every module that keeps state

    real = _real_data_dir()
    leaks = [
        f"{module_name}.{attr} = {value}"
        for module_name, module in list(sys.modules.items())
        if module_name == "server" or module_name.startswith("app.")
        for attr, value in vars(module).items()
        if isinstance(value, Path) and value.is_relative_to(real)
    ]

    assert not leaks, leaks


def test_legacy_migration_leaves_an_explicit_data_dir_alone(tmp_path, monkeypatch):
    """The old TidalDownloader folder belongs to the default location.
    Migrating it into an explicit dir would hand a test run the user's
    old settings and Tidal session."""
    legacy = tmp_path / paths._LEGACY_APP_NAME
    legacy.mkdir()
    (legacy / "tidal_session.json").write_text("{}")
    explicit = tmp_path / "explicit"
    monkeypatch.setattr(paths, "_app_data_root", lambda: tmp_path)
    monkeypatch.setattr(paths, "_migrated", False)
    monkeypatch.setenv("TIDEWAY_DATA_DIR", str(explicit))

    paths.migrate_legacy_cwd_state()

    assert not (explicit / "tidal_session.json").exists()
