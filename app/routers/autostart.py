"""`/api/autostart` — launch-at-login toggle.

Self-contained: the routes delegate everything to `app.autostart`
(which knows the platform-specific specifics — LaunchAgent on macOS,
HKCU\\Run on Windows, .desktop file on Linux). The router exists
mostly to expose the get/set pair under loopback auth.

First domain extracted as part of the gradual server.py → routers/
split. See `app/routers/__init__.py` for the playbook.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


router = APIRouter(prefix="/api/autostart", tags=["autostart"])


class _AutostartRequest(BaseModel):
    enabled: bool


@router.get("")
def autostart_status() -> dict:
    """Report whether the app is registered to launch at login.

    `available` is False in dev mode (no frozen exe path); the UI
    grays out the toggle in that case.
    """
    # Lazy imports avoid a circular `server` ↔ router dependency;
    # `server.py` populates these on startup and registers this
    # router with `app.include_router`, so the symbols are
    # available by the time any request lands here.
    from server import _require_local_access
    from app import autostart

    _require_local_access()
    return autostart.status()


@router.put("")
def autostart_set(req: _AutostartRequest) -> dict:
    from server import _require_local_access
    from app import autostart

    _require_local_access()
    try:
        return autostart.set_enabled(req.enabled)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
