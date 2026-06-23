"""Cross-platform contract for the macOS windowDidMove_ startup-crash
guard (issue #215).

The fix itself — a guarded pywebview WindowDelegate subclass — only
*does* anything on macOS, where it overrides the delegate callback that
crashes during window creation on macOS 26. What these tests pin is the
contract that protects every *other* platform: the guard must be a
safe, best-effort no-op off macOS, and must never let a Cocoa/PyObjC
import failure escape and brick startup. The actual macOS dispatch
(does AppKit call our override?) needs manual validation on a Mac.
"""
from __future__ import annotations

import sys

import desktop


def test_guard_is_a_noop_off_darwin(monkeypatch):
    # On Windows/Linux the guard must return immediately, before it ever
    # reaches the Cocoa import — startup on those platforms can't regress.
    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop._guard_cocoa_window_move() is None


def test_guard_swallows_cocoa_import_failure(monkeypatch):
    # Force the darwin branch on this non-mac box: importing
    # webview.platforms.cocoa pulls in PyObjC/AppKit, which isn't present
    # here, so the import raises. The guard must swallow that and return
    # rather than take down the app at launch (best-effort contract).
    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop._guard_cocoa_window_move() is None
