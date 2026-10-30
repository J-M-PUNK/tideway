"""GET /api/events: player, download and hotkey events on one connection.

These used to be three EventSources, each holding one of the six
HTTP/1.1 connections Chromium allows per host for the life of the app,
which left cover images and route chunks queueing in the browser behind
ordinary API calls. The merged stream has to keep each source's
behaviour: an initial snapshot per source, live events as named SSE
events, a broker desync closing the stream so the client resubscribes,
and every source unsubscribing when the stream ends.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import server
from app.audio.player import PlayerSnapshot
from app.routers.hotkey import bus


class _FakePlayer:
    def __init__(self) -> None:
        self.listeners: list = []
        self.snap = PlayerSnapshot(
            state="paused",
            track_id="t1",
            position_ms=0,
            duration_ms=1000,
            volume=50,
            muted=False,
            seq=1,
        )

    def subscribe(self, fn):
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn)

    def snapshot(self) -> PlayerSnapshot:
        return self.snap


class _Request:
    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture
def env(monkeypatch):
    player = _FakePlayer()
    monkeypatch.setattr(server, "_native_player", lambda: player)
    monkeypatch.setattr(server, "_require_local_access", lambda: None)
    monkeypatch.setattr(server.broker, "_loop", server.broker._loop)
    monkeypatch.setattr(server.broker, "_subs", set())
    monkeypatch.setattr(bus, "_loop", bus._loop)
    monkeypatch.setattr(bus, "_subscribers", [])
    return player


async def _events(body, n: int) -> list[tuple[str, dict]]:
    """Read frames until `n` named events have arrived."""
    out: list[tuple[str, dict]] = []
    while len(out) < n:
        frame = await asyncio.wait_for(body.__anext__(), timeout=3)
        if frame.startswith("event: "):
            name_line, data_line = frame.strip().split("\n", 1)
            out.append(
                (name_line[len("event: ") :], json.loads(data_line[len("data: ") :]))
            )
    return out


def test_each_source_opens_with_its_snapshot(env):
    async def run():
        body = (await server.events(_Request())).body_iterator
        assert await body.__anext__() == "retry: 1000\n\n"
        got = dict(await _events(body, 3))
        await body.aclose()
        return got

    got = asyncio.run(run())
    assert got["player"]["track_id"] == "t1"
    assert got["download"] == {"type": "reset", "items": []}
    assert got["hotkey"] == {"action": "_ready"}


def test_live_events_arrive_as_named_events(env):
    player = env

    async def run():
        body = (await server.events(_Request())).body_iterator
        await _events(body, 3)  # the three opening snapshots
        bus.publish("next")
        player.snap = PlayerSnapshot(
            state="playing",
            track_id="t1",
            position_ms=500,
            duration_ms=1000,
            volume=50,
            muted=False,
            seq=2,
        )
        for listener in list(player.listeners):
            listener(player.snap)
        got = await _events(body, 2)
        await body.aclose()
        return got

    got = asyncio.run(run())
    names = {name: data for name, data in got}
    assert names["hotkey"] == {"action": "next"}
    assert names["player"]["state"] == "playing"


def test_a_broker_desync_closes_the_stream_and_everything_unsubscribes(env):
    player = env

    async def run():
        body = (await server.events(_Request())).body_iterator
        await _events(body, 3)
        (q,) = server.broker._subs
        q.put_nowait({"type": "__desync__"})
        rest = [frame async for frame in body]
        return rest

    rest = asyncio.run(run())
    assert not any(f.startswith("event: download") for f in rest)
    assert server.broker._subs == set()
    assert bus._subscribers == []
    assert player.listeners == []


def test_closing_the_stream_unsubscribes_every_source(env):
    # What the server does when the client goes away.
    player = env

    async def run():
        body = (await server.events(_Request())).body_iterator
        await _events(body, 3)
        await body.aclose()

    asyncio.run(run())
    assert server.broker._subs == set()
    assert bus._subscribers == []
    assert player.listeners == []


def test_the_three_old_streams_are_gone():
    routes = {getattr(r, "path", None) for r in server.app.routes}
    assert "/api/events" in routes
    assert not routes & {
        "/api/player/events",
        "/api/downloads/stream",
        "/api/hotkey/events",
    }
