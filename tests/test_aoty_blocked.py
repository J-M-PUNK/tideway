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

from tests.conftest import known_to_curl_cffi

from app import aoty
from app import aoty_clearance


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""
        self.encoding = "utf-8"


@pytest.fixture(autouse=True)
def _reset_block_state():
    # Each test starts from a clean slate; both modules are singletons
    # so leaked state would silently turn the second test green. The
    # clearance cache especially: a cleared fetch in one test would let
    # the next one skip the challenge path entirely.
    aoty._blocked_at = None
    aoty_clearance.reset_for_tests()
    yield
    aoty._blocked_at = None
    aoty_clearance.reset_for_tests()


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


def test_challenge_without_a_solver_blocks_after_one_request():
    # Outside the desktop shell there is no webview to clear the
    # challenge with, so there is nothing to retry. Exactly one request
    # goes out — the old fingerprint rotation would have sent three to
    # learn the same thing, which against a *managed* challenge is
    # three identical answers.
    with patch("app.aoty.cffi_requests.get") as get:
        get.return_value = _FakeResponse(403, {"cf-mitigated": "challenge"})
        result = aoty._fetch(
            "https://www.albumoftheyear.org/releases/this-week/"
        )
    assert result is None
    assert aoty.is_scraper_blocked() is True
    assert get.call_count == 1


def test_clearance_recovers_from_challenge():
    # The real recovery path: the bare request is challenged, the
    # webview solves it, and the retry carrying `cf_clearance` gets
    # through. The blocked flag must stay clear.
    ok = _FakeResponse(200)
    ok.text = "<html>cleared</html>"
    aoty_clearance.register_solver(
        lambda url, rejected=None: ({"cf_clearance": "tok"}, "TestUA/1.0")
    )
    with patch("app.aoty.cffi_requests.get") as get:
        get.side_effect = [_FakeResponse(403, {"cf-mitigated": "challenge"}), ok]
        result = aoty._fetch(
            "https://www.albumoftheyear.org/releases/this-week/"
        )
    assert result == "<html>cleared</html>"
    assert aoty.is_scraper_blocked() is False
    assert get.call_count == 2


def test_cleared_request_sends_cookie_and_matching_user_agent():
    # `cf_clearance` is bound to the User-Agent that earned it, so the
    # retry has to replay both. Sending the cookie under curl_cffi's own
    # impersonated UA gets it rejected — this is the one place the
    # module's "never send custom headers" rule is deliberately broken.
    ok = _FakeResponse(200)
    ok.text = "<html>cleared</html>"
    aoty_clearance.register_solver(
        lambda url, rejected=None: ({"cf_clearance": "tok"}, "TestUA/1.0")
    )
    with patch("app.aoty.cffi_requests.get") as get:
        get.side_effect = [_FakeResponse(403, {"cf-mitigated": "challenge"}), ok]
        aoty._fetch("https://www.albumoftheyear.org/releases/this-week/")
    first, retry = get.call_args_list
    # The uncleared request carries no overrides at all.
    assert "headers" not in first.kwargs
    assert "cookies" not in first.kwargs
    assert retry.kwargs["cookies"] == {"cf_clearance": "tok"}
    assert retry.kwargs["headers"] == {"User-Agent": "TestUA/1.0"}


def test_solver_that_cannot_clear_sets_blocked_flag():
    # A webview that never gets past the interstitial is a real block —
    # an IP-level ban, or a challenge the engine can't satisfy. Flip the
    # flag so the Home page says so.
    aoty_clearance.register_solver(lambda url, rejected=None: None)
    with patch("app.aoty.cffi_requests.get") as get:
        get.return_value = _FakeResponse(403, {"cf-mitigated": "challenge"})
        result = aoty._fetch(
            "https://www.albumoftheyear.org/releases/this-week/"
        )
    assert result is None
    assert aoty.is_scraper_blocked() is True
    # One request, then the failed solve. No retry without a clearance.
    assert get.call_count == 1


def test_rejected_clearance_token_is_handed_to_the_solver():
    # The solver can only recover from a stale clearance if it is told
    # which value failed. pywebview keeps a persistent profile, so an
    # expired `cf_clearance` from an earlier run is already in the jar
    # when the window opens; without the rejected token the solver hands
    # that same dud straight back and the retry fails for the same
    # reason. This is the contract that lets it wait for a new one.
    seen = []

    def solver(url, rejected=None):
        seen.append(rejected)
        return ({"cf_clearance": "first" if rejected is None else "second"},
                "TestUA/1.0")

    aoty_clearance.register_solver(solver)
    ok = _FakeResponse(200)
    ok.text = "<html>cleared</html>"
    challenge = _FakeResponse(403, {"cf-mitigated": "challenge"})

    with patch("app.aoty.cffi_requests.get") as get:
        # First fetch: no clearance, solve, succeed. Caches "first".
        get.side_effect = [challenge, ok]
        assert aoty._fetch("https://www.albumoftheyear.org/genre.php") is not None

    with patch("app.aoty.cffi_requests.get") as get:
        # Second fetch reuses the cached clearance, which has since
        # expired. The re-solve must name it so the solver knows not to
        # hand the same value back.
        get.side_effect = [challenge, ok]
        assert aoty._fetch("https://www.albumoftheyear.org/genre.php") is not None

    assert seen == [None, "first"], seen


def test_a_rejected_clearance_is_resolved_exactly_once():
    # An expired clearance earns a challenge on a request we thought was
    # cleared. Solve again and retry — but only once. If the fresh
    # clearance is also refused the problem isn't staleness, and looping
    # would open a webview window per attempt.
    aoty_clearance.register_solver(
        lambda url, rejected=None: ({"cf_clearance": "tok"}, "TestUA/1.0")
    )
    challenge = _FakeResponse(403, {"cf-mitigated": "challenge"})
    with patch("app.aoty.cffi_requests.get") as get:
        get.return_value = challenge
        result = aoty._fetch(
            "https://www.albumoftheyear.org/releases/this-week/"
        )
    assert result is None
    assert aoty.is_scraper_blocked() is True
    assert get.call_count == 2


def test_an_unknown_profile_is_rejected():
    """The guards above are only worth anything if the check can fail.
    A misspelling has to be caught here rather than at request time,
    where it degrades into a silent 403 on every retry."""
    assert not known_to_curl_cffi("chorme")
    assert not known_to_curl_cffi("")


def test_impersonate_profile_is_current_and_known_to_curl_cffi():
    """Guard against shipping a Cloudflare-blocked impersonate
    profile. `chrome120` is confirmed-403'd by AOTY's Cloudflare;
    don't let a revert reintroduce it. A fixture test can't verify
    Cloudflare *accepts* a profile (that needs the live site — see
    the PR's probe), but it can pin that the configured value isn't
    the known-bad one and is a profile curl_cffi actually recognises
    (so a typo like "chorme" fails loudly instead of silently 403'ing
    every request)."""
    assert aoty._CFFI_IMPERSONATE != "chrome120"
    # Resolves either as an alias or as a concrete target; an unknown
    # string is neither.
    assert known_to_curl_cffi(aoty._CFFI_IMPERSONATE)
