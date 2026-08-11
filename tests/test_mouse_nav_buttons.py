"""The mouse's Back/Forward side buttons need a native handler on GTK (#316).

WebKitGTK does not deliver the 4th and 5th mouse buttons to the DOM —
MDN: "On Linux (GTK), the 4th button and the 5th button are not
supported." So the frontend's `mouseup` listener, which is fine on
WebView2 and WKWebView, can never fire on Linux. GDK does report them,
as buttons 8 and 9 on `button-press-event`, so that is where the Linux
handler has to live.

These cover the mapping and the wiring. They do not cover GTK itself:
there is no GTK backend on the machine this is developed on, so the
signal actually firing is verified against a stand-in BrowserView rather
than a real widget.
"""
import sys
import types

import pytest

import desktop


# ----------------------------------------------------------------------
# Button mapping
# ----------------------------------------------------------------------


def test_side_buttons_map_to_history_moves():
    assert desktop.gdk_history_move(8) == "back"
    assert desktop.gdk_history_move(9) == "forward"


@pytest.mark.parametrize("button", [0, 1, 2, 3, 4, 5, 6, 7, 10])
def test_other_buttons_are_left_alone(button):
    """Left/middle/right and the scroll-tilt buttons (6 and 7 on X11)
    must fall through, or normal clicking breaks."""
    assert desktop.gdk_history_move(button) is None


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------


class _FakeWebView:
    def __init__(self, can_back=True, can_forward=True):
        self.connected = {}
        self.moves = []
        self._can_back = can_back
        self._can_forward = can_forward

    def connect(self, signal, handler):
        self.connected[signal] = handler

    def can_go_back(self):
        return self._can_back

    def can_go_forward(self):
        return self._can_forward

    def go_back(self):
        self.moves.append("back")

    def go_forward(self):
        self.moves.append("forward")


def _install_fake_gtk_backend(monkeypatch):
    """Stand in for webview.platforms.gtk, which does not import on a
    machine with no GTK."""
    inits = []

    class BrowserView:
        def __init__(self, window):
            self.window = window
            self.webview = _FakeWebView()
            inits.append(self)

    module = types.ModuleType("webview.platforms.gtk")
    module.BrowserView = BrowserView
    monkeypatch.setitem(sys.modules, "webview.platforms.gtk", module)
    return BrowserView


def test_does_nothing_off_linux(monkeypatch):
    """The Cocoa and WebView2 backends deliver these buttons to the DOM,
    where the frontend hook already handles them."""
    monkeypatch.setattr(sys, "platform", "darwin")
    BrowserView = _install_fake_gtk_backend(monkeypatch)
    original = BrowserView.__init__

    desktop._wire_gtk_mouse_nav()

    assert BrowserView.__init__ is original


def _wired_webview(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    BrowserView = _install_fake_gtk_backend(monkeypatch)
    desktop._wire_gtk_mouse_nav()
    return BrowserView(window=object()).webview


def test_connects_the_gdk_button_signal(monkeypatch):
    webview = _wired_webview(monkeypatch)
    assert "button-press-event" in webview.connected


@pytest.mark.parametrize("button,move", [(8, "back"), (9, "forward")])
def test_side_buttons_drive_history(monkeypatch, button, move):
    webview = _wired_webview(monkeypatch)
    handler = webview.connected["button-press-event"]

    handled = handler(None, types.SimpleNamespace(button=button))

    assert webview.moves == [move]
    # True marks the event consumed, so WebKitGTK doesn't also act on it.
    assert handled is True


def test_ordinary_clicks_pass_through(monkeypatch):
    webview = _wired_webview(monkeypatch)
    handler = webview.connected["button-press-event"]

    handled = handler(None, types.SimpleNamespace(button=1))

    assert webview.moves == []
    assert handled is False


@pytest.mark.parametrize("button", [8, 9])
def test_ends_of_history_are_a_no_op(monkeypatch, button):
    """Pressing Back on the first page must not navigate, but must still
    be consumed rather than falling back to GTK's own handling."""
    webview = _wired_webview(monkeypatch)
    webview._can_back = False
    webview._can_forward = False
    handler = webview.connected["button-press-event"]

    handled = handler(None, types.SimpleNamespace(button=button))

    assert webview.moves == []
    assert handled is True


def test_the_original_constructor_still_runs(monkeypatch):
    """The patch wraps pywebview's __init__ rather than replacing it, so
    the window is still built."""
    monkeypatch.setattr(sys, "platform", "linux")
    BrowserView = _install_fake_gtk_backend(monkeypatch)
    desktop._wire_gtk_mouse_nav()

    sentinel = object()
    view = BrowserView(window=sentinel)

    assert view.window is sentinel
    assert view.webview is not None
