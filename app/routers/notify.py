"""`/api/notify` — fire an OS-level notification.

The frontend owns the "should I notify?" decision because it has
the context the backend doesn't — track title/artist, whether the
window is focused, which user preference is set. The server is just
a thin shim that exposes the platform-specific notification shell
so this can run from inside a sandbox where the browser
Notification API isn't available (pywebview's WKWebView doesn't
surface it as system-level).

Loopback-only: rejects requests from anything other than 127.0.0.1
/ ::1 / localhost. The endpoint is hidden from OpenAPI for the
same reason — there's no legitimate caller from outside the app.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


router = APIRouter(prefix="/api/notify", tags=["notify"])


class _NotifyRequest(BaseModel):
    title: str
    body: str
    subtitle: Optional[str] = None


@router.post("", include_in_schema=False)
def fire_notification(req: _NotifyRequest, request: Request) -> dict:
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403)
    from app.notify import notify as _notify
    _notify(req.title, req.body, req.subtitle)
    return {"ok": True}
