"""COM initialisation for threads that start WASAPI output streams.

PortAudio's WASAPI host API starts a stream through COM (IAudioClient),
which needs COM initialised on the calling thread. PortAudio only does
that on the thread that ran Pa_Initialize. Tideway starts streams from
HTTP worker threads, the gapless bridge, device recovery and so on, so
a start on any of those failed with

    Error starting stream: Unanticipated host error [PaErrorCode -9999]:
    'WdmSyncIoctl: DeviceIoControl GLE = 0x00000490 ...'

The WDM-KS text in it is just PortAudio's last recorded host error; the
failure is the WASAPI start. Whether a given worker thread happened to
have COM initialised by something else is luck, which is why it looked
like a race: the same open/start cycle always succeeds from the main
thread. Opening, stopping, aborting and closing a stream don't need it;
only starting one does. MME (PortAudio's own Windows default) doesn't
use COM at all, so this only ever bit WASAPI devices.

Each thread is initialised once, into the multithreaded apartment (a
worker thread has no message pump for a single-threaded one), and left
initialised: pool threads live as long as the process, so there's no
point to pair a CoUninitialize with.
"""
from __future__ import annotations

import sys
import threading

_COINIT_MULTITHREADED = 0x0
_S_OK = 0
_S_FALSE = 1  # already initialised on this thread
# The thread already joined a single-threaded apartment. COM is usable
# there and WASAPI starts fine from it, so that's success too.
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106 as a signed HRESULT

_initialised = threading.local()


def ensure_com_initialized() -> None:
    """Make COM usable on the calling thread before it starts a stream.
    No-op off Windows and after the first call on a thread."""
    if sys.platform != "win32" or getattr(_initialised, "done", False):
        return
    import ctypes

    hr = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
    if hr not in (_S_OK, _S_FALSE, _RPC_E_CHANGED_MODE):
        raise OSError(f"CoInitializeEx failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    _initialised.done = True
