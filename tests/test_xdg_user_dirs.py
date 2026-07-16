"""Tests for the XDG-aware Linux download-folder defaults (#261).

The Flatpak manifest grants xdg-music / xdg-videos, which map to the
directories named in user-dirs.dirs. The defaults have to resolve
through that same file or, on a localized system, they'd point at a
literal ~/Music that only exists as unmounted tmpfs in the sandbox —
downloads would "complete" and vanish on restart.
"""
import sys
from pathlib import Path

from app.settings import (
    _default_output_dir,
    _default_videos_dir,
    _migrate_default_paths,
    _xdg_user_dir,
    Settings,
)


def _write_user_dirs(config_home: Path, body: str) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "user-dirs.dirs").write_text(body, encoding="utf-8")


def _linux(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


def test_xdg_user_dir_reads_localized_entry(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    _write_user_dirs(
        tmp_path / ".config",
        'XDG_MUSIC_DIR="$HOME/Música"\nXDG_VIDEOS_DIR="$HOME/Vídeos"\n',
    )
    fallback = tmp_path / "Music"
    assert _xdg_user_dir("MUSIC", fallback) == tmp_path / "Música"
    assert _xdg_user_dir("VIDEOS", tmp_path / "Videos") == tmp_path / "Vídeos"


def test_xdg_user_dir_falls_back_without_config(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    fallback = tmp_path / "Music"
    assert _xdg_user_dir("MUSIC", fallback) == fallback


def test_xdg_user_dir_ignores_disabled_entry(monkeypatch, tmp_path):
    """The spec disables a user dir by pointing it at $HOME (or a
    relative path) — treat both as 'no such dir', not as a target."""
    _linux(monkeypatch, tmp_path)
    _write_user_dirs(
        tmp_path / ".config",
        'XDG_MUSIC_DIR="$HOME/"\nXDG_VIDEOS_DIR="Videos"\n',
    )
    fallback = tmp_path / "Music"
    assert _xdg_user_dir("MUSIC", fallback) == fallback
    assert _xdg_user_dir("VIDEOS", tmp_path / "Videos") == tmp_path / "Videos"


def test_xdg_user_dir_is_linux_only(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _write_user_dirs(tmp_path / ".config", 'XDG_MUSIC_DIR="$HOME/Música"\n')
    fallback = tmp_path / "Music"
    assert _xdg_user_dir("MUSIC", fallback) == fallback


def test_default_dirs_follow_user_dirs_config(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    _write_user_dirs(
        tmp_path / ".config",
        'XDG_MUSIC_DIR="$HOME/Música"\nXDG_VIDEOS_DIR="$HOME/Vídeos"\n',
    )
    assert _default_output_dir() == str(tmp_path / "Música" / "Tidal")
    assert _default_videos_dir() == str(tmp_path / "Vídeos" / "Tidal")


def test_default_dirs_without_config_keep_conventional_names(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    assert _default_output_dir() == str(tmp_path / "Music" / "Tidal")
    assert _default_videos_dir() == str(tmp_path / "Videos" / "Tidal")


def test_migration_moves_old_default_to_xdg_dir(monkeypatch, tmp_path):
    """A persisted old hardcoded default follows the music dir the
    user-dirs config names; a custom path stays put."""
    _linux(monkeypatch, tmp_path)
    _write_user_dirs(
        tmp_path / ".config",
        'XDG_MUSIC_DIR="$HOME/Música"\nXDG_VIDEOS_DIR="$HOME/Vídeos"\n',
    )
    s = Settings()
    s.output_dir = str(tmp_path / "Music" / "Tidal")
    s.videos_dir = str(tmp_path / "Videos" / "Tidal")

    assert _migrate_default_paths(s) is True
    assert s.output_dir == str(tmp_path / "Música" / "Tidal")
    assert s.videos_dir == str(tmp_path / "Vídeos" / "Tidal")

    custom = Settings()
    custom.output_dir = str(tmp_path / "stash")
    custom.videos_dir = str(tmp_path / "stash-video")
    assert _migrate_default_paths(custom) is False
    assert custom.output_dir == str(tmp_path / "stash")
