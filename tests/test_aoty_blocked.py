"""Tests for the Cloudflare-challenge detection path in `app.aoty`.

We pin two things here:

  1. A 403/503 response carrying `cf-mitigated: challenge` flips
     `is_scraper_blocked()` to True so the Home page can render
     its "report on GitHub" notice.
  2. A plain non-200 response (real HTTP error, not a CF
     challenge) does NOT flip the flag — the notice should only
     fire on the specific failure mode that needs a code change.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import aoty


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""
        self.encoding = "utf-8"


@pytest.fixture(autouse=True)
def _reset_block_state():
    # Each test starts from a clean slate; the module is a singleton
    # so leaked state would silently turn the second test green.
    aoty._blocked_at = None
    yield
    aoty._blocked_at = None


def test_cf_challenge_sets_blocked_flag():
    with patch("app.aoty.cffi_requests.get") as get:
        get.return_value = _FakeResponse(
            403, {"cf-mitigated": "challenge"}
        )
        result = aoty._fetch("https://www.albumoftheyear.org/releases/this-week/")
    assert result is None
    assert aoty.is_scraper_blocked() is True


def test_cf_503_challenge_also_sets_blocked_flag():
    # Cloudflare can serve either 403 or 503 depending on the
    # ruleset; both come with the same `cf-mitigated: challenge`
    # header. Confirm both trip the detector.
    with patch("app.aoty.cffi_requests.get") as get:
        get.return_value = _FakeResponse(
            503, {"cf-mitigated": "challenge"}
        )
        aoty._fetch("https://www.albumoftheyear.org/releases/this-week/")
    assert aoty.is_scraper_blocked() is True


def test_plain_500_does_not_set_blocked_flag():
    # AOTY's own backend going down (500 with no CF header) is a
    # different failure mode — transient, no code change needed,
    # so the user-facing notice should stay quiet.
    with patch("app.aoty.cffi_requests.get") as get:
        get.return_value = _FakeResponse(500)
        aoty._fetch("https://www.albumoftheyear.org/releases/this-week/")
    assert aoty.is_scraper_blocked() is False


def test_403_without_cf_header_does_not_set_blocked_flag():
    # A vanilla 403 (e.g. AOTY's own auth wall, hypothetical) with
    # no `cf-mitigated` header isn't the Cloudflare challenge
    # signature. Don't false-positive.
    with patch("app.aoty.cffi_requests.get") as get:
        get.return_value = _FakeResponse(403)
        aoty._fetch("https://www.albumoftheyear.org/releases/this-week/")
    assert aoty.is_scraper_blocked() is False


def test_successful_fetch_keeps_flag_clear():
    with patch("app.aoty.cffi_requests.get") as get:
        ok = _FakeResponse(200)
        ok.text = "<html></html>"
        get.return_value = ok
        result = aoty._fetch("https://www.albumoftheyear.org/releases/this-week/")
    assert result == "<html></html>"
    assert aoty.is_scraper_blocked() is False
