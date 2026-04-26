"""Tests for _friendly_pkce_error.

The PKCE login error chain we see in the wild is
`ConnectionError("Connection aborted!", PermissionError(13, "..."))`,
which on Windows is almost always antivirus blocking the socket. The
401 the user gets back from /api/auth/pkce-complete needs to point at
their AV, not at Tidal — they were furious to spend an hour
re-pasting login URLs blaming Tidal when it never reached Tidal.
"""
from __future__ import annotations

from app.tidal_client import _friendly_pkce_error


def _wrap_args_style() -> Exception:
    """Simulate the requests/urllib3 shape: outer ConnectionError
    whose .args[1] is the underlying PermissionError."""
    inner = PermissionError(13, "Permission denied")

    class FakeRequestsConnError(Exception):
        pass

    return FakeRequestsConnError("Connection aborted!", inner)


def _wrap_cause_style() -> Exception:
    """Some libraries wrap with __cause__ rather than args."""
    inner = PermissionError(13, "Permission denied")
    outer = OSError("Connection aborted!")
    outer.__cause__ = inner
    return outer


def test_permission_error_in_args_is_friendlied():
    msg = _friendly_pkce_error(_wrap_args_style())
    assert msg is not None
    low = msg.lower()
    assert "antivirus" in low or "firewall" in low
    assert "allow" in low


def test_permission_error_in_cause_is_friendlied():
    msg = _friendly_pkce_error(_wrap_cause_style())
    assert msg is not None
    assert "antivirus" in msg.lower() or "firewall" in msg.lower()


def test_string_pattern_matches_when_no_actual_permissionerror():
    """Some impls stringify the inner exception, so the type is gone
    but the text still says 'Connection aborted' + 'Permission
    denied'. The string-pattern fallback should still catch it."""

    class Stringy(Exception):
        pass

    msg = _friendly_pkce_error(
        Stringy("('Connection aborted!', PermissionError(13, 'Permission denied'))")
    )
    assert msg is not None
    assert "antivirus" in msg.lower() or "firewall" in msg.lower()


def test_unrelated_exception_returns_none():
    """A 401 from Tidal proper (bad code, expired) shouldn't get the
    AV message — that would mislead users who actually did paste a
    stale URL. Falls through to the raw error string."""
    assert _friendly_pkce_error(ValueError("invalid_grant")) is None


def test_plain_oserror_without_eacces_returns_none():
    """A generic 'connection refused' is not the AV pattern."""
    assert _friendly_pkce_error(OSError("Connection refused")) is None


def test_handles_self_referential_cause_chain():
    """Defensive: if some upstream lib loops __context__ back on
    itself we shouldn't infinite-loop walking the chain."""

    class Loopy(Exception):
        pass

    e = Loopy("nope")
    e.__context__ = e
    # Just needs to return; result can be None.
    _friendly_pkce_error(e)
