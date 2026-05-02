"""End-to-end test for `POST /api/update/install` with the
signature-verification path enabled.

These tests mock the network and the launch shell-out so we can
exercise the verification step against real minisign fixtures
without actually downloading from GitHub or trying to open a DMG.

The fixtures (pubkey + signature) are the same ones used by
`tests/test_release_verify.py` — see that file's docstring for how
to regenerate them. The artifact bytes are the literal `_SAMPLE_BYTES`
written to a tmp file and served back when the endpoint asks for
the "download URL."
"""
from __future__ import annotations

import copy
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# Real minisign fixture — see tests/test_release_verify.py for how
# to regenerate. Reused here verbatim so the two test files stay in
# sync about what a "good" signature looks like.
_SAMPLE_BYTES = b"hello world for tideway tests"
_GOOD_PUBKEY_B64 = "RWRv+hrD4n6IHnBYbNuii5cI+6kIjlObSklkq05gr6FAAv3jT9E2ZgzU"
_GOOD_SIG = """untrusted comment: signature from minisign secret key
RURv+hrD4n6IHihct4sqL4Rv4Eevp2LS1nvlxnKXi2hQf7Lu8jdSbQjBbFtXm1yBeMte4ea9kyxuC+yh413J14omlxLqfKpTfQg=
trusted comment: tideway test fixture v1
VORlSSWFJiAY+ATCrhb/7aIN38iGXCMAXqAgFe1L2iKiJcf1UQsp0H9iAoF3YBfOuupLYDVCm7L9mEq+GcKYAA==
"""

_FAKE_ASSET_URL = "https://example.test/releases/Tideway-1.3.0-test.bin"


class _FakeResponse:
    """Minimal stand-in for the parts of `requests.Response` the
    update install endpoint touches: streaming chunked iteration for
    the artifact, `.text` for the signature companion, and
    `raise_for_status` based on a presupplied status code."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status_code = status
        self.text = body.decode("utf-8", errors="replace")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _r

            raise _r.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 8192):
        view = memoryview(self._body)
        for i in range(0, len(view), chunk_size):
            yield bytes(view[i : i + chunk_size])


@pytest.fixture
def signed_install_client(tmp_path, monkeypatch):
    """TestClient with the update install path wired up for tests:

    - `~/Downloads` redirected into a per-test tmp dir.
    - Network mocked: requests.get returns canned bytes / sig text.
    - subprocess.Popen mocked so we don't actually launch anything.
    - sys.platform pinned to darwin so the launch branch under test
      is the same one the macOS user would hit.
    - server.TRUSTED_RELEASE_PUBKEYS populated with the real test
      key so verification has something to check against.

    Each test can override the URL-handler closure on the returned
    object to flip individual responses (e.g. signature missing).
    """
    import server
    from app.release_verify import TrustedKey

    # Capture original module state so each test starts clean.
    original_settings = copy.deepcopy(server.settings)
    server.settings.offline_mode = True

    # Pin platform to macOS so the darwin launch branch (subprocess.Popen
    # with `open`) is what runs. Avoids needing a no-op for os.startfile
    # on Windows or xdg-open on Linux when the test host is one of those.
    monkeypatch.setattr(server.sys, "platform", "darwin")

    # Send the staged downloads at a per-test tmp directory.
    fake_downloads = tmp_path / "Downloads"
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: tmp_path))

    # Skip the actual launch — subprocess.Popen would try to spawn
    # `open` against the staged file, which exists but isn't a real
    # DMG. The verification path is what the test cares about.
    launched = []

    def _fake_popen(args, **kwargs):
        launched.append(args)

        class _NullProc:
            pid = 0

        return _NullProc()

    monkeypatch.setattr(server.subprocess, "Popen", _fake_popen)

    # Pre-populate the asset url so update_install doesn't hit the
    # GitHub API. The cache TTL guard means the endpoint will return
    # this synchronously rather than re-fetching.
    monkeypatch.setattr(server, "_update_asset_url", lambda: _FAKE_ASSET_URL)

    # Real trusted key for the verifier to match against. The bound
    # name in `server` is what the endpoint actually looks at —
    # patching `app.release_keys.TRUSTED_RELEASE_PUBKEYS` wouldn't
    # rebind the symbol that server.py imported by name.
    monkeypatch.setattr(
        server,
        "TRUSTED_RELEASE_PUBKEYS",
        [TrustedKey(pubkey_b64=_GOOD_PUBKEY_B64, label="test-primary")],
    )

    # Default URL handler: artifact returns the good bytes, sig
    # returns the good sig. Tests can override `state["responses"]`
    # to flip individual cases.
    state = {
        "responses": {
            _FAKE_ASSET_URL: _FakeResponse(_SAMPLE_BYTES),
            _FAKE_ASSET_URL + ".minisig": _FakeResponse(_GOOD_SIG.encode()),
        },
    }

    def _fake_get(url, **kwargs):
        if url not in state["responses"]:
            raise AssertionError(f"unexpected GET {url}")
        return state["responses"][url]

    import requests as _real_requests

    monkeypatch.setattr(_real_requests, "get", _fake_get)

    with TestClient(server.app) as c:
        # Attach the mutable bits so individual tests can poke them.
        c.state_responses = state["responses"]  # type: ignore[attr-defined]
        c.fake_downloads_dir = fake_downloads  # type: ignore[attr-defined]
        c.launched = launched  # type: ignore[attr-defined]
        yield c

    server.settings = original_settings


# --- happy path ---------------------------------------------------------


def test_install_with_valid_signature_launches_installer(signed_install_client):
    r = signed_install_client.post("/api/update/install")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # File should exist on disk after the call returns, and the
    # launch shim should have been invoked exactly once.
    staged = Path(body["downloaded_to"])
    assert staged.exists()
    assert staged.read_bytes() == _SAMPLE_BYTES
    # Verified sig should be persisted alongside so the user can
    # re-verify with the minisign CLI.
    assert (staged.parent / (staged.name + ".minisig")).exists()
    assert len(signed_install_client.launched) == 1


# --- failure modes ------------------------------------------------------


def test_install_refuses_when_signature_missing(signed_install_client):
    """A missing .minisig (404 from the signature URL) should:
    1. Return 502 rather than launching the unverified binary.
    2. Delete the partially-downloaded artifact so a curious user
       browsing ~/Downloads can't run it by hand thinking it's safe.
    """
    signed_install_client.state_responses[
        _FAKE_ASSET_URL + ".minisig"
    ] = _FakeResponse(b"not found", status=404)

    r = signed_install_client.post("/api/update/install")
    assert r.status_code == 502
    assert "signature file" in r.json()["detail"].lower()

    # Nothing should have been launched, and the staged binary
    # should be gone.
    assert signed_install_client.launched == []
    staged_files = list(signed_install_client.fake_downloads_dir.glob("*.bin"))
    assert staged_files == []


def test_install_refuses_when_artifact_tampered(signed_install_client):
    """Same threat model as above but for the case where someone
    swapped the binary content while leaving an old (now mismatching)
    signature in place. The verifier should catch it; the endpoint
    should clean up and refuse to launch."""
    signed_install_client.state_responses[_FAKE_ASSET_URL] = _FakeResponse(
        _SAMPLE_BYTES + b"tampered"
    )

    r = signed_install_client.post("/api/update/install")
    assert r.status_code == 502
    detail = r.json()["detail"].lower()
    assert "signature verification" in detail
    assert "deleted" in detail

    assert signed_install_client.launched == []
    # Both the binary AND the minisig sidecar should be cleaned up.
    leftovers = list(signed_install_client.fake_downloads_dir.glob("*"))
    assert leftovers == []


def test_install_refuses_when_no_trusted_keys_configured(
    signed_install_client, monkeypatch
):
    """A build that ships with `TRUSTED_RELEASE_PUBKEYS = []` (the
    placeholder state before the maintainer pastes in the real
    pubkeys) should refuse to install anything. The wrong fix would
    be to silently skip verification — that's how every install on
    every user's machine becomes vulnerable to a leaked GitHub
    token."""
    import server

    monkeypatch.setattr(server, "TRUSTED_RELEASE_PUBKEYS", [])

    r = signed_install_client.post("/api/update/install")
    assert r.status_code == 502
    assert "no trusted release-signing keys" in r.json()["detail"].lower()
    assert signed_install_client.launched == []
