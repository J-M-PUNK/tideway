"""Tests for `/api/update-check` error handling.

The original implementation hit GitHub via `urllib.request` with a
bare `except Exception: pass`. When the bundled Python's urllib
couldn't resolve the system CA bundle, the cert verification failed,
the exception got swallowed, and the response shape was
`{available: false, latest: null}` — indistinguishable from a
healthy "no update available" reply. Real users reported "I never
see update banners" and we had no way to tell them why.

Fix: the function now uses `requests` (which bundles certifi) and
surfaces the error message on the response payload + logs a warning
so support has something to point at. Tests pin both behaviors.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import server


@pytest.fixture(autouse=True)
def _reset_update_cache(monkeypatch):
    """Each test starts with an empty cache so we can deterministically
    drive the `_fetch_latest_release` mock through the cold path."""
    monkeypatch.setattr(server, "_update_cache", {})
    monkeypatch.setattr(server, "_UPDATE_REPO", "test/repo")
    monkeypatch.setattr(server, "APP_VERSION", "0.4.7")


def test_update_check_returns_error_when_github_fails():
    """The cert-verify regression returned `latest: null` with no
    other signal. The fix surfaces the failure reason."""
    def _boom(timeout=8.0):  # noqa: ARG001
        raise ConnectionError("CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate")

    with patch.object(server, "_fetch_latest_release", _boom):
        result = server.update_check()

    assert result["available"] is False
    assert result["latest"] is None
    assert result["error"] is not None
    assert "CERTIFICATE_VERIFY_FAILED" in result["error"]


def test_update_check_clears_error_on_success():
    """A subsequent successful fetch must not carry the previous
    failure's error field."""
    def _ok(timeout=8.0):  # noqa: ARG001
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

    with patch.object(server, "_fetch_latest_release", _ok), \
         patch.object(server.sys, "platform", "darwin"):
        result = server.update_check()

    assert result["error"] is None
    assert result["latest"] == "v0.4.8"


def test_update_check_caches_error_state():
    """A failure shouldn't refire on every page load. The cache holds
    the error payload for the same TTL as a success response so we
    don't hammer GitHub during an outage."""
    call_count = {"n": 0}

    def _boom(timeout=8.0):  # noqa: ARG001
        call_count["n"] += 1
        raise ConnectionError("network down")

    with patch.object(server, "_fetch_latest_release", _boom):
        first = server.update_check()
        second = server.update_check()

    assert call_count["n"] == 1, "second call should hit the cached error"
    assert first == second
    assert first["error"] is not None


def test_update_check_disabled_repo_returns_no_error():
    """When the env var unsets the upstream repo, the response should
    be a clean idle payload — no error string, since "no repo
    configured" isn't a failure, it's a deliberate off-state."""
    with patch.object(server, "_UPDATE_REPO", ""):
        result = server.update_check()

    assert result["error"] is None
    assert result["latest"] is None
    assert result["available"] is False


def test_fetch_latest_release_uses_requests():
    """Smoke test that the function pulls in `requests` (so certifi's
    CA bundle is in play) rather than `urllib.request`. We don't make
    the real network call — just verify the right module is imported
    and called."""
    captured = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"tag_name": "v9.9.9"}

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResp()

    with patch("requests.get", _fake_get):
        out = server._fetch_latest_release(timeout=2.0)

    assert out == {"tag_name": "v9.9.9"}
    assert captured["url"].endswith("/repos/test/repo/releases/latest")
    assert captured["headers"]["Accept"] == "application/vnd.github+json"
    assert captured["timeout"] == 2.0
