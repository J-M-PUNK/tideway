"""The packaged app must not run uvicorn on uvloop (#308).

python-zeroconf's QueryScheduler keeps uvloop's idle handle armed, so the
loop busy-spins rather than sleeping between queries. Cast and Tidal
Connect each start a service browser at launch, and the pair burned ~20%
of a core on an idle install with no client attached — measured on the
same machine as 24.8% (uvloop) against 0.1% (asyncio).

`uvicorn[standard]` installs uvloop and uvicorn's default `loop="auto"`
picks it whenever it imports, so this reverts silently the moment the
explicit setting is dropped — and it reverts into a symptom nobody sees
locally unless they happen to open Activity Monitor. Hence a test.
"""
import uvicorn


def _config_kwargs(monkeypatch):
    """Capture the kwargs desktop.py hands uvicorn.Config."""
    import desktop

    seen = {}

    class _FakeConfig:
        def __init__(self, app, **kwargs):
            seen["app"] = app
            seen.update(kwargs)

    class _FakeServer:
        # `started` short-circuits the readiness wait in
        # _run_uvicorn_in_thread so the test doesn't sit through it.
        started = True

        def __init__(self, config):
            self.config = config

        def run(self):  # pragma: no cover - never started in the test
            raise AssertionError("server should not run under test")

    monkeypatch.setattr(uvicorn, "Config", _FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    # The real thread would try to serve; swap it for an inert stand-in.
    monkeypatch.setattr(
        desktop.threading, "Thread",
        lambda *a, **kw: type("T", (), {"start": lambda self: None})(),
    )
    desktop._run_uvicorn_in_thread()
    return seen


def test_packaged_app_pins_the_asyncio_loop(monkeypatch):
    assert _config_kwargs(monkeypatch).get("loop") == "asyncio"


def test_loop_is_not_left_to_uvicorn_auto(monkeypatch):
    """`auto` is the failure mode, not a neutral default: it resolves to
    uvloop whenever uvloop is importable, which it always is here."""
    loop = _config_kwargs(monkeypatch).get("loop")
    assert loop != "auto"
    assert loop != "uvloop"


def test_uvloop_is_importable_so_auto_would_have_picked_it():
    """Guards the premise. If uvloop ever stops shipping, the pin above
    becomes harmless rather than load-bearing, and this test says so by
    failing instead of leaving a stale comment behind."""
    import importlib.util

    assert importlib.util.find_spec("uvloop") is not None, (
        "uvloop is gone from the dependency tree — the loop pin in "
        "desktop.py and the reasoning in #308 can be revisited"
    )
