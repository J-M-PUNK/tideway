"""`/api/hotkey/*` — global media-key event bus.

Global hotkeys (play-pause / next / previous) fire on a pynput
thread in the backend. We publish each to a thread-safe bus; the
frontend subscribes via SSE and runs the corresponding action
through its player hook — that way queue/shuffle/repeat decisions
stay in the frontend instead of being re-implemented server-side.

Two surfaces:
  - POST `/api/hotkey/{play_pause,next,previous}` — the global-key
    listener (`app.global_keys`) HTTP-POSTs here when it sees a
    media key.
  - GET `/api/hotkey/events` — SSE stream the frontend subscribes
    to from `usePlayer`.

`bus.bind_loop(loop)` must be called once on startup with the
running asyncio event loop so `publish()` (called from the pynput
thread) can `call_soon_threadsafe` payloads onto subscribers'
queues. `server.py`'s `lifespan` does this.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse


router = APIRouter(prefix="/api/hotkey", tags=["hotkey"])


class _HotkeyBus:
    """Thread-safe fan-out from the pynput listener thread to any
    number of SSE subscribers running on the asyncio loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def publish(self, action: str) -> None:
        """Safe to call from any thread (including pynput's listener
        thread). Schedules the payload put on the FastAPI event loop."""
        with self._lock:
            loop = self._loop
            subs = list(self._subscribers)
        if loop is None:
            return
        for q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, action)
            except Exception:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


# Module-level singleton — same instance for every subscriber + every
# publish. `server.py` imports `bus` for the startup `bind_loop` call.
bus = _HotkeyBus()


def _emit(action: str) -> dict:
    bus.publish(action)
    return {"ok": True, "action": action}


@router.post("/play_pause")
def play_pause() -> dict:
    from server import _require_local_access
    _require_local_access()
    return _emit("play_pause")


@router.post("/next")
def next_track() -> dict:
    from server import _require_local_access
    _require_local_access()
    return _emit("next")


@router.post("/previous")
def previous_track() -> dict:
    from server import _require_local_access
    _require_local_access()
    return _emit("previous")


@router.get("/events")
async def events(request: Request):
    """SSE stream of hotkey events. The frontend's usePlayer hook
    subscribes and maps each action onto its own toggle/next/prev so
    queue state + advance logic stay in one place."""
    from server import _require_local_access
    _require_local_access()
    bus.bind_loop(asyncio.get_running_loop())
    q = bus.subscribe()

    async def _gen():
        try:
            # Initial ping so the frontend knows the subscription is up.
            yield "data: {\"action\": \"_ready\"}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    action = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # Keepalive comment — prevents proxies from closing
                    # a silent connection.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps({'action': action})}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
