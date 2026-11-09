"""A test run never touches the user's real download folders.

tests/conftest.py points TIDEWAY_OUTPUT_DIR and TIDEWAY_VIDEOS_DIR at a
scratch directory before any test module is imported. Without that,
every TestClient lifespan startup deleted `*.part` files under
~/Music/Tidal, including one from a download the installed app had in
progress, and re-scanned the whole library, opening every audio file
in it.
"""
from __future__ import annotations

import os
from pathlib import Path

from app import settings as settings_mod
from app.settings import Settings


def test_fresh_settings_download_into_the_scratch_dirs():
    s = Settings()

    assert s.output_dir == os.environ["TIDEWAY_OUTPUT_DIR"]
    assert s.videos_dir == os.environ["TIDEWAY_VIDEOS_DIR"]
    assert s.output_dir != settings_mod._platform_output_dir()
    assert s.videos_dir != settings_mod._platform_videos_dir()


def test_fresh_install_settings_download_into_the_scratch_dirs(tmp_path, monkeypatch):
    """The no-settings-file path is the one a test run's server.settings
    takes, so it is the one that decides where lifespan startup scans."""
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")

    s = settings_mod.load_settings()

    assert s.output_dir == os.environ["TIDEWAY_OUTPUT_DIR"]
    assert s.videos_dir == os.environ["TIDEWAY_VIDEOS_DIR"]


def test_migration_ignores_the_overrides(monkeypatch, tmp_path):
    """A saved shipped default is not a stale default just because an
    override is set; migrating it would rewrite the user's settings.json
    to point at the override."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    s = Settings()
    s.output_dir = settings_mod._platform_output_dir()
    s.videos_dir = settings_mod._platform_videos_dir()

    assert settings_mod._migrate_default_paths(s) is False
    assert s.output_dir == settings_mod._platform_output_dir()
    assert s.videos_dir == settings_mod._platform_videos_dir()
