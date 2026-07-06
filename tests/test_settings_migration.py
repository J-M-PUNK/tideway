"""Regression tests for the Tideway→Tidal default-paths migration.

Tightly scoped to `_migrate_default_paths`. The rest of `load_settings`
touches on-disk state and is covered indirectly by the app's manual
smoke tests.
"""
from pathlib import Path

from app.settings import (
    Settings,
    _migrate_default_paths,
    _migrate_output_device_index,
)


def test_default_tideway_music_path_migrates_to_tidal(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    s = Settings()
    # Simulate a settings.json carrying the old default.
    s.output_dir = str(tmp_path / "Music" / "Tideway")

    changed = _migrate_default_paths(s)

    assert changed is True
    assert s.output_dir == str(tmp_path / "Music" / "Tidal")


def test_default_tideway_videos_path_migrates(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    s = Settings()
    # macOS-style legacy.
    s.videos_dir = str(tmp_path / "Movies" / "Tideway")

    changed = _migrate_default_paths(s)

    assert changed is True
    # Migration replaces with current per-OS default, which ends in "Tidal".
    assert s.videos_dir.endswith("Tidal")
    assert "Tideway" not in s.videos_dir


def test_custom_music_path_is_not_migrated(monkeypatch, tmp_path):
    """User picked a custom path — leave it alone."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    s = Settings()
    custom = str(tmp_path / "SomethingElse" / "MyMusic")
    s.output_dir = custom
    # Pin videos_dir to a known non-legacy value so the overall
    # `changed` result reflects only the output_dir decision.
    s.videos_dir = str(tmp_path / "Movies" / "Tidal")

    changed = _migrate_default_paths(s)

    assert s.output_dir == custom
    assert changed is False


def test_custom_videos_path_is_not_migrated(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    s = Settings()
    s.output_dir = str(tmp_path / "Music" / "Tidal")  # already new default
    custom = str(tmp_path / "Elsewhere" / "MyVideos")
    s.videos_dir = custom

    changed = _migrate_default_paths(s)

    assert changed is False
    assert s.videos_dir == custom


def test_migration_is_idempotent(monkeypatch, tmp_path):
    """Second call after a successful migration should be a no-op."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    s = Settings()
    s.output_dir = str(tmp_path / "Music" / "Tideway")
    s.videos_dir = str(tmp_path / "Movies" / "Tideway")

    _migrate_default_paths(s)  # first pass migrates
    changed_again = _migrate_default_paths(s)  # second is no-op

    assert changed_again is False


def test_legacy_numeric_output_device_is_cleared():
    """A saved PortAudio index (issue #245) resets to system default."""
    s = Settings()
    s.audio_output_device = "1"

    changed = _migrate_output_device_index(s)

    assert changed is True
    assert s.audio_output_device == ""


def test_system_default_output_device_is_untouched():
    s = Settings()
    s.audio_output_device = ""

    changed = _migrate_output_device_index(s)

    assert changed is False
    assert s.audio_output_device == ""


def test_named_output_device_is_untouched():
    """A name selection under the new scheme is never mistaken for a
    legacy index, so it survives the migration."""
    s = Settings()
    s.audio_output_device = "MacBook Pro Speakers"

    changed = _migrate_output_device_index(s)

    assert changed is False
    assert s.audio_output_device == "MacBook Pro Speakers"
