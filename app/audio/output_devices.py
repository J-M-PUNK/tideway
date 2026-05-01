"""Output-device enumeration for the picker UI.

PortAudio's `sd.query_devices()` is a cross-platform list, but each
OS exposes audio devices differently and the raw enumeration is
either too noisy (Windows lists every device 4× under different host
APIs) or too quiet (macOS hides devices the user disabled in System
Settings, but PortAudio still reports them as available).

`list_output_devices(stream_active)` returns the picker-ready list:
each entry is `{"id": str, "name": str}`, with `id=""` reserved for
"System default". The first entry is always the system-default row
regardless of platform; everything after passes the platform filter.

Per-platform behaviour:

  macOS — filters to whatever CoreAudio's
    `kAudioDevicePropertyDeviceCanBeDefaultDevice` reports as visible
    (the same bit System Settings uses to populate its picker). Hides
    Microsoft Teams Audio, ZoomAudioDevice, BlackHole, microphones with
    no output streams, and aggregate devices the user hasn't enabled.

  Windows — filters to the WASAPI host API only. PortAudio also enumerates
    via MME, DirectSound, and WDM-KS; those produce duplicate listings
    AND ignore the IMMDevice DEVICE_STATE_ACTIVE bit, so they show
    devices the user disabled in Sound settings. WASAPI honors that bit.

  Linux — no filter applied. The typical PortAudio build only links
    ALSA, so the duplicate-host problem doesn't apply.

The function is read-only and stateless — it doesn't take any locks,
mutate anything, or care about the player's stream lifecycle. The
caller is responsible for the `sd._terminate()` / `sd._initialize()`
refresh dance that picks up newly-plugged devices, since that has to
be coordinated with the player's currently-open stream.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

import sounddevice as sd  # type: ignore

log = logging.getLogger(__name__)


def macos_visible_output_names() -> Optional[set[str]]:
    """CoreAudio's visibility set, or None on failure / non-darwin.

    Imports the macos_audio_devices module lazily so non-darwin
    builds don't pay the framework load cost. Logs once on import
    or query failure, then returns None so the caller falls
    through to "show every output-capable device".
    """
    if sys.platform != "darwin":
        return None
    try:
        from app.audio.macos_audio_devices import (
            visible_output_device_names,
        )
    except Exception:
        log.exception("macos_audio_devices import failed; falling back")
        return None
    try:
        return visible_output_device_names()
    except Exception:
        log.exception("visible_output_device_names raised; falling back")
        return None


def wasapi_host_api_index() -> Optional[int]:
    """PortAudio host-API index for WASAPI, or None elsewhere.

    None on non-Windows or when WASAPI isn't built into this
    PortAudio. Used to filter out the duplicate listings other
    Windows host APIs produce, and to honor IMMDevice's
    DEVICE_STATE_ACTIVE filter (which only WASAPI applies).
    """
    if sys.platform != "win32":
        return None
    try:
        for idx, ha in enumerate(sd.query_hostapis()):
            name = (ha.get("name") or "").lower()
            if name == "windows wasapi" or "wasapi" in name:
                return idx
    except Exception:
        log.exception(
            "query_hostapis failed; falling back to all-host-API listing"
        )
    return None


def list_output_devices(stream_active: bool) -> list[dict]:
    """Enumerate output devices for the picker, applying per-platform
    filters described in the module docstring.

    `stream_active` controls whether we run the PortAudio
    re-initialization dance. PortAudio caches its device list at
    init time; on macOS / Windows newly-plugged devices don't appear
    until we re-init. Doing that while a stream is open would tear
    down the live audio, so callers must pass `stream_active=True`
    when a stream is currently running.

    Logs the full enumeration via `[audio]` print lines so users
    debugging "my headphones aren't showing up" can see what
    PortAudio reports AND which entries each filter accepted vs.
    rejected.
    """
    out: list[dict] = [{"id": "", "name": "System default"}]

    if not stream_active:
        # Re-init PortAudio to pick up devices plugged in since the
        # last query. Wrapped in try/except because some PortAudio
        # builds raise on _terminate when nothing's been initialized
        # yet — non-fatal, the next query_devices still works.
        try:
            sd._terminate()
        except Exception:
            pass
        try:
            sd._initialize()
        except Exception:
            log.exception("sd._initialize after refresh failed")

    try:
        devices = sd.query_devices()
    except Exception:
        log.exception("sd.query_devices failed")
        print(
            "[audio] device enumeration failed — see traceback above",
            flush=True,
        )
        return out

    visible = macos_visible_output_names()
    if visible is not None:
        print(
            f"[audio] CoreAudio reports {len(visible)} visible output "
            f"device(s): {sorted(visible)!r}",
            flush=True,
        )

    wasapi_idx = wasapi_host_api_index()
    if wasapi_idx is not None:
        print(
            f"[audio] WASAPI host-api index = {wasapi_idx}; "
            "Windows picker will hide non-WASAPI entries",
            flush=True,
        )

    print(
        f"[audio] PortAudio enumerated {len(devices)} device(s) "
        f"(stream_active={stream_active}):",
        flush=True,
    )
    for i, d in enumerate(devices):
        ch_in = int(d.get("max_input_channels", 0) or 0)
        ch_out = int(d.get("max_output_channels", 0) or 0)
        try:
            ha_name = sd.query_hostapis(d["hostapi"])["name"]
        except Exception:
            ha_name = f"hostapi={d.get('hostapi')}"
        name = d.get("name") or f"Device {i}"
        kind = "OUT" if ch_out > 0 else ("IN " if ch_in > 0 else "?  ")

        if ch_out > 0:
            # Two filters, OR'd into a single accept decision. On
            # macOS the visibility set is the only signal; on
            # Windows the WASAPI host-api index is. On Linux both
            # are None and every output-capable device passes.
            accepted = True
            if visible is not None and name not in visible:
                accepted = False
            if (
                wasapi_idx is not None
                and d.get("hostapi") != wasapi_idx
            ):
                accepted = False
            tag = "OUT " if accepted else "HIDE"
        else:
            accepted = False
            tag = kind

        print(
            f"[audio]   [{i:2d}] {tag} ch={ch_out}/{ch_in} "
            f"hostapi={ha_name!r} name={name!r}",
            flush=True,
        )
        if accepted:
            out.append({"id": str(i), "name": name})
    return out
