"""Tests for the Windows frameless-window maximize geometry.

The interesting logic is `_maximized_rect`, the pure computation that
decides where a maximized WS_THICKFRAME window is placed so its client
area fills the monitor work area instead of sitting inset by the resize
border. The ctypes / WndProc plumbing around it can only run on Windows,
but the geometry is platform-independent and is where the bug lived.
"""

from app import window_controls as wc


def _client_rect(pos_x, pos_y, size_x, size_y, frame_cx, frame_cy):
    """The visible client rect = window rect shrunk by one frame on
    every edge, expressed relative to the monitor origin."""
    left = pos_x + frame_cx
    top = pos_y + frame_cy
    right = pos_x + size_x - frame_cx
    bottom = pos_y + size_y - frame_cy
    return left, top, right, bottom


def test_client_fills_work_area_on_primary_monitor():
    # 1920x1080 monitor with a 48px bottom taskbar, 8px resize frame.
    work = (0, 0, 1920, 1032)
    mon_origin = (0, 0)
    frame = 8

    pos_x, pos_y, size_x, size_y = wc._maximized_rect(
        work, mon_origin, frame, frame
    )

    # The client area must land flush with the work area on all sides.
    assert _client_rect(pos_x, pos_y, size_x, size_y, frame, frame) == (
        0,
        0,
        1920,
        1032,
    )


def test_resize_frame_hangs_off_the_work_area():
    # The window itself is grown by one frame per edge so the border
    # sits outside the work area (off-screen top/left, behind the
    # taskbar on the bottom) rather than eating into visible content.
    work = (0, 0, 1920, 1032)
    pos_x, pos_y, size_x, size_y = wc._maximized_rect(work, (0, 0), 8, 8)

    assert pos_x == -8 and pos_y == -8
    assert size_x == 1920 + 16
    assert size_y == 1032 + 16


def test_position_is_relative_to_monitor_origin():
    # On a secondary monitor offset to the right, ptMaxPosition is
    # expressed relative to that monitor's own top-left, not the
    # virtual-desktop origin. A taskbar-free monitor has work == bounds.
    work = (1920, 0, 3840, 1080)
    mon_origin = (1920, 0)
    frame = 8

    pos_x, pos_y, size_x, size_y = wc._maximized_rect(
        work, mon_origin, frame, frame
    )

    assert (pos_x, pos_y) == (-8, -8)
    # Client fills the whole (taskbar-free) monitor.
    assert _client_rect(pos_x, pos_y, size_x, size_y, frame, frame) == (
        0,
        0,
        1920,
        1080,
    )


def test_high_dpi_frame_thickness_still_lands_flush():
    # A scaled monitor reports a thicker resize border; the client must
    # still fill the work area exactly.
    work = (0, 0, 2560, 1440)
    frame = 11

    pos_x, pos_y, size_x, size_y = wc._maximized_rect(
        work, (0, 0), frame, frame
    )

    assert _client_rect(pos_x, pos_y, size_x, size_y, frame, frame) == (
        0,
        0,
        2560,
        1440,
    )


def test_no_taskbar_fills_entire_monitor():
    # When the work area equals the monitor bounds (no taskbar on this
    # edge), the client covers the full monitor.
    work = (0, 0, 1366, 768)
    frame = 6

    pos_x, pos_y, size_x, size_y = wc._maximized_rect(
        work, (0, 0), frame, frame
    )

    assert _client_rect(pos_x, pos_y, size_x, size_y, frame, frame) == (
        0,
        0,
        1366,
        768,
    )
