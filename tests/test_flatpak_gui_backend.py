"""The Flatpak launcher must pin pywebview to the GTK backend.

pywebview picks a Linux backend itself, and its heuristic is wrong for
this build (`webview/guilib.py`):

    forced_gui = 'qt' if 'KDE_FULL_SESSION' in os.environ else None

Any KDE session therefore tries Qt first. The GNOME runtime this
Flatpak ships against provides GTK 3 and WebKit2 4.1 and no Qt bindings
at all, so Qt can only fail — it imports `qtpy`, which isn't present,
and logs a ModuleNotFoundError traceback that reads like a fatal
startup error while being a recovered one. That is what #424 reported
from KDE Plasma 6.

`PYWEBVIEW_GUI=gtk` in the launcher removes KDE as a special case. It
does not remove the Qt fallback: pywebview still tries GTK then Qt,
which is the order every non-KDE desktop already gets.

These are file-content assertions rather than behavioural ones because
the launcher only ever runs inside the Flatpak sandbox, which CI builds
but cannot run a window in. The value here is that deleting the export
fails a test instead of silently restoring the bug.
"""
from __future__ import annotations

from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "flatpak" / "tideway-launcher.sh"


@pytest.fixture(scope="module")
def launcher_text() -> str:
    assert LAUNCHER.is_file(), f"missing {LAUNCHER}"
    return LAUNCHER.read_text(encoding="utf-8")


def test_launcher_pins_the_gtk_backend(launcher_text: str) -> None:
    assert "PYWEBVIEW_GUI=gtk" in launcher_text, (
        "the Flatpak launcher must export PYWEBVIEW_GUI=gtk, or pywebview "
        "forces Qt on KDE sessions and this runtime has no Qt bindings (#424)"
    )
    assert "export PYWEBVIEW_GUI=gtk" in launcher_text, (
        "PYWEBVIEW_GUI has to be exported, not just assigned — pywebview "
        "reads it from the environment of the python3 process"
    )


def test_gtk_is_a_value_pywebview_actually_accepts() -> None:
    """Guard against pinning a backend name pywebview ignores.

    `PYWEBVIEW_GUI` is only honoured when its value is in pywebview's
    own `GUI_TYPES`; anything else is dropped silently and the KDE
    heuristic wins again. A typo here would look exactly like the bug
    we are fixing, so assert the value is one pywebview knows.
    """
    from typing import Literal, get_args

    # Read the literal from pywebview rather than hardcoding the list,
    # so a future release renaming its backends fails loudly here.
    import webview

    guilib_src = (
        Path(webview.__file__).parent / "guilib.py"
    ).read_text(encoding="utf-8")
    decl = next(
        line for line in guilib_src.splitlines() if line.startswith("GUIType")
    )
    namespace: dict = {"TypeAlias": object, "Literal": Literal}
    exec(decl.replace(": TypeAlias", ""), namespace)  # noqa: S102
    accepted = list(get_args(namespace["GUIType"]))

    assert "gtk" in accepted, (
        f"pywebview no longer accepts 'gtk' as PYWEBVIEW_GUI; accepted "
        f"values are {accepted}. The launcher's pin is now a no-op."
    )


def test_launcher_still_execs_desktop_py(launcher_text: str) -> None:
    # Cheap guard that the added export didn't displace the entry point.
    assert "exec python3 desktop.py" in launcher_text
