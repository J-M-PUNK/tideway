"""Tests for the Flatpak detection branch of the in-app self-updater.

Inside a Flatpak sandbox the in-app "Install now" button can't reach
the real installation — updates come through `flatpak update`, not by
downloading an AppImage and exec'ing it. `_running_in_flatpak()`
detects the sandbox via `/.flatpak-info` or `$FLATPAK_ID`, and the
two updater endpoints branch off that:

  - `/api/update-check` flags `kind="flatpak"` so the banner can swap
    the install action for a `flatpak update ...` hint. The check
    still considers an update "available" without looking up a
    per-platform asset URL, since the bits come from the Flatpak
    remote.
  - `/api/update/install` refuses with HTTP 409 and a message naming
    the right command, instead of downloading an AppImage the user
    can't run.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import server


@pytest.fixture(autouse=True)
def _reset_update_cache(monkeypatch):
    monkeypatch.setattr(server, "_update_cache", {})
    monkeypatch.setattr(server, "_UPDATE_REPO", "test/repo")
    monkeypatch.setattr(server, "APP_VERSION", "0.4.7")


def _fake_release_with_dmg(timeout: float = 8.0) -> dict:  # noqa: ARG001
    return {
        "tag_name": "v0.4.8",
        "html_url": "https://example/r/v0.4.8",
        "body": "release notes",
        "assets": [
            {
                "name": "Tideway-0.4.8.dmg",
                "browser_download_url": "https://example/Tideway-0.4.8.dmg",
            }
        ],
    }


def test_running_in_flatpak_detects_env_var(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "com.tidaldownloader.Tideway")
    assert server._running_in_flatpak() is True


def test_running_in_flatpak_returns_false_outside_sandbox(monkeypatch):
    """On macOS/Windows or a non-Flatpak Linux install neither the
    env var nor the marker file exists, and the helper must say so —
    a false-positive here would silently disable the in-app updater."""
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    # The marker file probe is what matters here; on the dev box
    # `/.flatpak-info` doesn't exist. Patch the Path check to be
    # explicit so the test stays meaningful on a build host that
    # happens to have a stray file there.
    with patch.object(server.Path, "is_file", return_value=False):
        assert server._running_in_flatpak() is False


def test_update_check_emits_flatpak_kind(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "com.tidaldownloader.Tideway")
    with patch.object(server, "_fetch_latest_release", _fake_release_with_dmg):
        result = server.update_check()
    assert result["kind"] == "flatpak"
    assert result["available"] is True
    assert result["latest"] == "v0.4.8"


def test_update_check_emits_installer_kind_by_default(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    with patch.object(server.Path, "is_file", return_value=False):
        with patch.object(server, "_fetch_latest_release", _fake_release_with_dmg):
            # The dmg asset matches macOS; on Linux without an AppImage
            # match this would return available=False, but kind is
            # independent of platform-match. Test the field directly.
            result = server.update_check()
    assert result["kind"] == "installer"


def test_update_check_flatpak_available_without_asset_match(monkeypatch):
    """In Flatpak we don't need a per-platform asset URL — the user
    runs `flatpak update`, the bits come from the Flatpak remote.
    The release that's "available" is just the GitHub tag. A
    release without any matching installer (e.g. Windows-only
    point release) still reports available for Flatpak users."""
    monkeypatch.setenv("FLATPAK_ID", "com.tidaldownloader.Tideway")
    def _release_no_assets(timeout: float = 8.0) -> dict:  # noqa: ARG001
        return {
            "tag_name": "v0.4.8",
            "html_url": "https://example/r/v0.4.8",
            "body": "windows-only patch",
            "assets": [],
        }
    with patch.object(server, "_fetch_latest_release", _release_no_assets):
        result = server.update_check()
    assert result["available"] is True
    assert result["kind"] == "flatpak"


def test_update_install_refuses_in_flatpak(monkeypatch):
    """The endpoint must return 409 with the `flatpak update` hint,
    not try to download the AppImage."""
    monkeypatch.setenv("FLATPAK_ID", "com.tidaldownloader.Tideway")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        # `_require_local_access` is the first thing the endpoint
        # checks; patch it out so we exercise the Flatpak branch.
        with patch.object(server, "_require_local_access", lambda: None):
            server.update_install()
    assert exc_info.value.status_code == 409
    assert "flatpak update" in exc_info.value.detail.lower()
    assert "com.tidaldownloader.Tideway" in exc_info.value.detail
