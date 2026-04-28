"""Query CoreAudio directly for the same output-device list macOS
System Settings → Sound → Output shows.

PortAudio's enumeration is opaque about device kind — Microsoft
Teams Audio, ZoomAudioDevice, BlackHole, Loopback, aggregate /
multi-output devices, microphones with no output streams, all show
up alongside real hardware as fully-fledged "output" entries.
macOS itself filters them out internally using a CoreAudio property,
`kAudioDevicePropertyDeviceCanBeDefaultDevice` (with output scope).
That property is the literal filter System Settings uses to build
its Output picker.

This module asks CoreAudio that same question, per device, and
returns the set of device names that pass — Tideway's picker then
intersects PortAudio's enumeration with this set so the user sees
exactly what they'd see in System Settings, no more and no less.

Implementation: ctypes against the CoreAudio.framework +
CoreFoundation.framework C APIs. PyObjC's bridge for
`AudioObjectGetPropertyData` doesn't auto-allocate the output
buffer in a way that worked with PyObjC's metadata for this
function, so we go straight to ctypes — fewer moving parts and
the ABI is documented and stable across macOS versions.

No-ops on non-darwin (returns None). Errors during the query also
return None so callers can fall back to the unfiltered PortAudio
list rather than going dark.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char,
    c_int32,
    c_uint32,
    c_void_p,
    create_string_buffer,
)
from typing import Optional

log = logging.getLogger(__name__)

# CoreAudio FourCharCode constants (the four ASCII bytes of the
# selector packed into a UInt32). Values are stable across macOS
# versions; documented in the CoreAudio Apple docs and unchanged
# since 10.5. Hardcoding rather than importing from PyObjC's
# CoreAudio module so this module is self-contained ctypes — no
# PyObjC dance for the constants when we already need ctypes for
# the function calls.
_K_AUDIO_OBJECT_SYSTEM_OBJECT = 1
_K_AUDIO_HARDWARE_PROPERTY_DEVICES = 0x64657623            # 'dev#'
_K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL = 0x676C6F62         # 'glob'
_K_AUDIO_OBJECT_PROPERTY_SCOPE_OUTPUT = 0x6F757470         # 'outp'
_K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN = 0
_K_AUDIO_DEVICE_PROPERTY_DEVICE_CAN_BE_DEFAULT_DEVICE = 0x64666C74  # 'dflt'
_K_AUDIO_DEVICE_PROPERTY_DEVICE_NAME_CF_STRING = 0x6C6E616D        # 'lnam'
_K_CF_STRING_ENCODING_UTF8 = 0x08000100


class _AudioObjectPropertyAddress(Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


# Lazy-loaded framework handles. None on non-darwin / when load
# fails (very rare; both frameworks are part of the base macOS
# install). Cached so repeat calls don't re-dlopen.
_ca: Optional[ctypes.CDLL] = None
_cf: Optional[ctypes.CDLL] = None


def _frameworks() -> Optional[tuple[ctypes.CDLL, ctypes.CDLL]]:
    global _ca, _cf
    if sys.platform != "darwin":
        return None
    if _ca is not None and _cf is not None:
        return _ca, _cf
    try:
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            return None
        ca = ctypes.CDLL(ca_path)
        cf = ctypes.CDLL(cf_path)
    except Exception as exc:
        log.warning("ctypes load of CoreAudio/CoreFoundation failed: %r", exc)
        return None

    # Wire the function signatures. ctypes won't auto-detect them and
    # calling without argtypes set leads to silent wrong-arg-size
    # crashes on 64-bit pointers.
    ca.AudioObjectGetPropertyDataSize.argtypes = [
        c_uint32,
        POINTER(_AudioObjectPropertyAddress),
        c_uint32,
        c_void_p,
        POINTER(c_uint32),
    ]
    ca.AudioObjectGetPropertyDataSize.restype = c_int32
    ca.AudioObjectGetPropertyData.argtypes = [
        c_uint32,
        POINTER(_AudioObjectPropertyAddress),
        c_uint32,
        c_void_p,
        POINTER(c_uint32),
        c_void_p,
    ]
    ca.AudioObjectGetPropertyData.restype = c_int32

    cf.CFStringGetCString.argtypes = [
        c_void_p, ctypes.c_char_p, c_uint32, c_uint32,
    ]
    cf.CFStringGetCString.restype = c_uint32  # Boolean
    cf.CFStringGetLength.argtypes = [c_void_p]
    cf.CFStringGetLength.restype = c_uint32
    cf.CFRelease.argtypes = [c_void_p]
    cf.CFRelease.restype = None

    _ca = ca
    _cf = cf
    return _ca, _cf


def _property_size(
    ca: ctypes.CDLL, object_id: int, selector: int, scope: int, element: int
) -> Optional[int]:
    addr = _AudioObjectPropertyAddress(selector, scope, element)
    size = c_uint32(0)
    err = ca.AudioObjectGetPropertyDataSize(
        object_id, byref(addr), 0, None, byref(size)
    )
    if err != 0:
        return None
    return size.value


def _property_data(
    ca: ctypes.CDLL,
    object_id: int,
    selector: int,
    scope: int,
    element: int,
    size: int,
) -> Optional[bytes]:
    addr = _AudioObjectPropertyAddress(selector, scope, element)
    buf = (c_char * size)()
    sz = c_uint32(size)
    err = ca.AudioObjectGetPropertyData(
        object_id, byref(addr), 0, None, byref(sz), buf
    )
    if err != 0:
        return None
    return bytes(buf[: sz.value])


def _cfstring_to_str(cf: ctypes.CDLL, cfstring_ref: int) -> str:
    """Convert a CFStringRef pointer to a Python str. Empty when
    conversion fails; doesn't release the ref (caller's job)."""
    if not cfstring_ref:
        return ""
    length = cf.CFStringGetLength(cfstring_ref)
    # UTF-8 worst case is 4 bytes per UTF-16 code unit; +1 for null.
    cap = (length * 4) + 1
    buf = create_string_buffer(cap)
    if cf.CFStringGetCString(
        cfstring_ref, buf, cap, _K_CF_STRING_ENCODING_UTF8
    ):
        try:
            return buf.value.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def visible_output_device_names() -> Optional[set[str]]:
    """Return the set of device names that macOS considers user-
    pickable output devices — the same list System Settings →
    Sound → Output shows.

    Returns None on non-darwin or when the CoreAudio query fails.
    Callers should fall back to PortAudio's unfiltered list in
    that case (better degraded UX than a blank picker).

    The set is keyed on display name because that's what we have
    in PortAudio's enumeration to intersect against. Two devices
    with identical display names is rare enough we ignore the
    edge — if it happens, both pass or both fail together and
    the picker shows the duplicate, same as macOS itself does.
    """
    fw = _frameworks()
    if fw is None:
        return None
    ca, cf = fw

    # Step 1: enumerate all device IDs.
    total = _property_size(
        ca,
        _K_AUDIO_OBJECT_SYSTEM_OBJECT,
        _K_AUDIO_HARDWARE_PROPERTY_DEVICES,
        _K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
        _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    if total is None or total <= 0:
        return None
    raw = _property_data(
        ca,
        _K_AUDIO_OBJECT_SYSTEM_OBJECT,
        _K_AUDIO_HARDWARE_PROPERTY_DEVICES,
        _K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
        _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
        total,
    )
    if raw is None:
        return None
    n = len(raw) // 4
    device_ids = [
        int.from_bytes(raw[i * 4 : i * 4 + 4], "little") for i in range(n)
    ]

    # Step 2: per device, ask "can be default OUTPUT". That property
    # is the same one System Settings uses to filter its list, so
    # it cleanly excludes virtual devices, microphones, aggregate
    # devices the user hasn't enabled, etc.
    visible: set[str] = set()
    for did in device_ids:
        can_raw = _property_data(
            ca,
            did,
            _K_AUDIO_DEVICE_PROPERTY_DEVICE_CAN_BE_DEFAULT_DEVICE,
            _K_AUDIO_OBJECT_PROPERTY_SCOPE_OUTPUT,
            _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
            4,
        )
        if can_raw is None or len(can_raw) < 4:
            continue
        can_default = bool(int.from_bytes(can_raw, "little"))
        if not can_default:
            continue

        # Step 3: get the display name (CFStringRef = pointer = 8 bytes
        # on every macOS that runs Python today). Convert via CF and
        # release immediately.
        name_raw = _property_data(
            ca,
            did,
            _K_AUDIO_DEVICE_PROPERTY_DEVICE_NAME_CF_STRING,
            _K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
            _K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
            8,
        )
        if name_raw is None or len(name_raw) < 8:
            continue
        cfstr = int.from_bytes(name_raw, "little")
        if not cfstr:
            continue
        try:
            name = _cfstring_to_str(cf, cfstr)
        finally:
            cf.CFRelease(cfstr)
        if name:
            visible.add(name)
    return visible
