"""Tests for the real Tidal Connect controller skeleton.

The wire-level pieces (TLS context, command frame shapes, request/
response correlation, discovery service constant) are testable
without hardware. The full WSS round-trip against a real device
isn't, and waits for a contributor with hardware.

Once captured-fixture frames from a real session are available,
add a `tests/fixtures/tidal_connect_real/` tree of JSON frames and
extend these tests to drive the receiver loop against them.
"""
from __future__ import annotations

import asyncio
import json
import ssl
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.audio import tidal_connect_real as tcr


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------


def test_service_type_matches_desktop_client():
    """The desktop client browses `_tidalconnect._tcp.local`. Anything
    else and our discovery sees zero devices."""
    assert tcr.TIDAL_CONNECT_SERVICE == "_tidalconnect._tcp.local."


def test_ca_bundle_loads_and_carries_two_certs():
    """The desktop client embeds TIDAL Root CA + TIDAL TS CA. If
    either is missing or malformed, every device connection fails
    TLS validation. Loading the bundle into an SSLContext is the
    cheapest way to confirm both certs parse and chain."""
    ctx = tcr._build_ssl_context()
    certs = ctx.get_ca_certs()
    assert len(certs) == 2
    cns = []
    for c in certs:
        # Subject is a tuple of RDN tuples; flatten and pull CN.
        for rdn in c.get("subject", ()):
            for attr, value in rdn:
                if attr == "commonName":
                    cns.append(value)
    assert "TIDAL Root CA" in cns
    assert "TIDAL TS CA" in cns


def test_ssl_context_skips_hostname_verification():
    """Tidal Connect devices serve certs with IP-based identities,
    not DNS names. Hostname verification has to be off, otherwise
    the WSS handshake fails the moment we connect."""
    ctx = tcr._build_ssl_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_REQUIRED


# ---------------------------------------------------------------------------
# Frame builders (via a connection in isolation)
# ---------------------------------------------------------------------------


def _make_connection_with_fake_socket() -> tuple[
    tcr.TidalConnectConnection, MagicMock
]:
    """Construct a connection wired to a mock WebSocket so we can
    observe outbound frames without opening a real connection."""
    device = tcr.DiscoveredDevice(
        id="test", name="Test Device", address="192.168.1.50", port=12345
    )
    conn = tcr.TidalConnectConnection(
        device=device,
        token_provider=lambda: "test-token",
        on_notification=lambda _: None,
    )
    fake_socket = MagicMock()
    fake_socket.send = AsyncMock()
    conn._websocket = fake_socket
    return conn, fake_socket


def _last_sent_frame(fake_socket: MagicMock) -> dict:
    args, _ = fake_socket.send.call_args
    return json.loads(args[0])


def _drive_send(coro):
    """Run a connection coroutine to first await without waiting on
    the response future. The test resolves the future manually."""
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(coro)
        # Yield once so the await self._websocket.send(...) lands.
        loop.run_until_complete(asyncio.sleep(0))
        return loop, task
    except BaseException:
        loop.close()
        raise


def test_play_frame_shape():
    conn, fake = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.play())
    try:
        frame = _last_sent_frame(fake)
        assert frame["command"] == "play"
        assert "requestId" in frame
        # Resolve the pending future so the task can complete.
        rid = frame["requestId"]
        conn._pending[rid].set_result({"requestId": rid, "ok": True})
        loop.run_until_complete(task)
    finally:
        loop.close()


def test_seek_frame_carries_position():
    conn, fake = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.seek(45_000))
    try:
        frame = _last_sent_frame(fake)
        assert frame["command"] == "seek"
        assert frame["position"] == 45_000
        rid = frame["requestId"]
        conn._pending[rid].set_result({"requestId": rid})
        loop.run_until_complete(task)
    finally:
        loop.close()


def test_set_volume_converts_percent_to_protocol_fraction():
    """UI percent must not be sent as a receiver volume above 1."""
    conn, fake = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.set_volume(33.7))  # type: ignore[arg-type]
    try:
        frame = _last_sent_frame(fake)
        assert frame["command"] == "setVolume"
        assert frame["level"] == 0.33
        rid = frame["requestId"]
        conn._pending[rid].set_result({"requestId": rid})
        loop.run_until_complete(task)
    finally:
        loop.close()


def test_request_id_increments_per_send():
    """Each command must carry a fresh requestId so concurrent
    in-flight requests can be matched to their responses without
    collision."""
    conn, fake = _make_connection_with_fake_socket()
    loop = asyncio.new_event_loop()
    try:
        ids_seen: list[int] = []
        for _ in range(3):
            task = loop.create_task(conn.play())
            loop.run_until_complete(asyncio.sleep(0))
            frame = json.loads(fake.send.call_args[0][0])
            ids_seen.append(frame["requestId"])
            conn._pending[frame["requestId"]].set_result(
                {"requestId": frame["requestId"]}
            )
            loop.run_until_complete(task)
        assert ids_seen == sorted(ids_seen)
        assert len(set(ids_seen)) == 3
    finally:
        loop.close()


def test_set_repeat_mode_uses_string_enum():
    """The desktop client's RepeatMode is a string enum: OFF / ALL
    / SINGLE. We forward whatever the caller passes; this test
    just confirms the field name and that the value is sent
    verbatim."""
    conn, fake = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.set_repeat_mode("ALL"))
    try:
        frame = _last_sent_frame(fake)
        assert frame["command"] == "setRepeatMode"
        assert frame["repeatMode"] == "ALL"
        rid = frame["requestId"]
        conn._pending[rid].set_result({"requestId": rid})
        loop.run_until_complete(task)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Manager singleton
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_singleton():
    tcr._manager = None
    yield
    if tcr._manager is not None:
        try:
            tcr._manager.stop()
        except Exception:
            pass
        tcr._manager = None


def test_get_manager_returns_none_until_started():
    assert tcr.get_manager() is None


def test_start_manager_is_idempotent():
    m1 = tcr.start_manager(
        token_provider=lambda: None,
        on_notification=lambda _: None,
    )
    m2 = tcr.start_manager(
        token_provider=lambda: None,
        on_notification=lambda _: None,
    )
    assert m1 is m2
    assert tcr.get_manager() is m1


class _ReplaySocket:
    """Replay sanitized device frames without a network connection."""

    def __init__(self, frames):
        self.frames = frames
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        pass

    def __aiter__(self):
        async def replay():
            for frame in self.frames:
                yield json.dumps(frame)
        return replay()


def test_wiim_pause_rejection_is_not_success():
    async def check():
        conn, _ = _make_connection_with_fake_socket()
        conn._websocket = _ReplaySocket([
            {"command": "notifyRequestResult", "requestId": 0, "resultCode": 1002}
        ])
        request = asyncio.create_task(conn.pause())
        await asyncio.sleep(0)
        await conn._receive_loop()
        with pytest.raises(tcr.TidalConnectProtocolError, match="pause.*1002"):
            await request
        assert not conn._pending
    asyncio.run(check())


def test_request_timeout_removes_pending_future(monkeypatch):
    monkeypatch.setattr(tcr, "REQUEST_TIMEOUT", 0.01)

    async def check():
        conn, _ = _make_connection_with_fake_socket()
        with pytest.raises(TimeoutError, match="play timed out"):
            await conn.play()
        assert not conn._pending
    asyncio.run(check())


def test_send_failure_removes_pending_future():
    async def check():
        conn, socket = _make_connection_with_fake_socket()
        socket.send.side_effect = ConnectionError("closed")
        with pytest.raises(ConnectionError):
            await conn.play()
        assert not conn._pending
    asyncio.run(check())


def test_socket_close_fails_pending_and_marks_connection_inactive():
    async def check():
        conn, _ = _make_connection_with_fake_socket()
        conn._websocket = _ReplaySocket([])
        conn._session_id = "test-session"
        request = asyncio.create_task(conn.play())
        await asyncio.sleep(0)
        await conn._receive_loop()
        with pytest.raises(ConnectionError, match="connection closed"):
            await request
        assert not conn.is_active()
        assert not conn._pending
    asyncio.run(check())


def test_session_error_rejects_handshake_without_exposing_details():
    async def check():
        conn, _ = _make_connection_with_fake_socket()
        conn._websocket = _ReplaySocket([{
            "command": "notifySessionError", "requestedCommand": "startSession",
            "errorCode": 1001, "details": "sensitive-server-body",
        }])
        request = asyncio.create_task(conn._start_session())
        await asyncio.sleep(0)
        await conn._receive_loop()
        with pytest.raises(tcr.TidalConnectProtocolError) as err:
            await request
        assert "1001" in str(err.value)
        assert "sensitive" not in str(err.value)
        assert not conn._pending
    asyncio.run(check())


def test_close_uses_session_id_without_request_id():
    async def check():
        conn, socket = _make_connection_with_fake_socket()
        socket.close = AsyncMock()
        conn._session_id = "test-session"
        task = asyncio.create_task(conn.close())
        await asyncio.sleep(0)
        frame = _last_sent_frame(socket)
        assert frame == {
            "command": "endSession", "sessionId": "test-session", "stopCasting": False,
        }
        conn._pending["notifySessionEnded"].set_result({"command": "notifySessionEnded"})
        await task
        assert not conn.is_active()
    asyncio.run(check())


def test_replay_wiim_state_uses_nested_volume_and_milliseconds():
    manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
    manager._record_notification({
        "command": "notifyDeviceStatusChanged", "volume": {"level": 0.2, "mute": False},
    })
    manager._record_notification({
        "command": "notifyPlayerStatusChanged", "playerState": "PLAYING",
        "progress": 12345, "duration": 180000,
    })
    manager._record_notification({
        "command": "notifyMediaChanged", "mediaInfo": {
            "mediaId": "123", "metadata": {"duration": 180000},
        },
    })
    state = manager.remote_state()
    assert state["volume"] == 20
    assert state["muted"] is False
    assert state["position_ms"] == 12345
    assert state["duration_ms"] == 180000
    assert state["media_info"]["mediaId"] == "123"


def test_remote_errors_retain_only_safe_codes_and_do_not_unmute_local_audio():
    manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
    silencer = MagicMock()
    manager.set_local_silencer(silencer)
    manager._record_notification({
        "command": "notifyContentServerError", "errorCode": 1012,
        "details": {"description": "sensitive-server-body"},
    })
    manager._record_notification({"command": "connectionLost"})
    assert manager.remote_state()["error"] == {"command": "connectionLost", "code": None}
    assert "sensitive" not in str(manager.remote_state())
    silencer.assert_not_called()


@pytest.mark.parametrize("percent, wire_level", [(-1, 0.0), (0, 0.0), (20, 0.2), (100, 1.0), (150, 1.0)])
def test_volume_never_exceeds_receiver_range(percent, wire_level):
    conn, socket = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.set_volume(percent))
    try:
        frame = _last_sent_frame(socket)
        assert frame["level"] == wire_level
        conn._pending[frame["requestId"]].set_result({"resultCode": 0})
        loop.run_until_complete(task)
    finally:
        loop.close()


def test_cancelling_request_removes_pending_future():
    async def check():
        conn, _ = _make_connection_with_fake_socket()
        task = asyncio.create_task(conn.play())
        await asyncio.sleep(0)
        assert conn._pending
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not conn._pending
    asyncio.run(check())


def test_manager_requires_live_receiver_for_active_state():
    async def check():
        manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
        conn, _ = _make_connection_with_fake_socket()
        manager._connection = conn
        conn._session_id = "test-session"
        assert not manager.is_active()
        conn._receiver_task = asyncio.create_task(asyncio.sleep(3600))
        assert manager.is_active()
        conn._receiver_task.cancel()
        await asyncio.gather(conn._receiver_task, return_exceptions=True)
        assert not manager.is_active()
    asyncio.run(check())


def _cloud_queue_payload():
    """Sanitized shape accepted by the WiiM cloud-queue probe."""
    return {
        "autoplay": True,
        "position": 0,
        "queueInfo": {
            "queueId": "fixture-queue", "repeatMode": "OFF", "shuffled": False,
            "maxBeforeSize": 10, "maxAfterSize": 10,
        },
        "currentMediaInfo": {
            "itemId": "fixture-item", "mediaId": "123", "mediaType": 0,
            "metadata": {"title": "Fixture", "duration": 180000},
        },
        "contentServerInfo": {
            "serverUrl": "https://content.invalid/v1",
            "authInfo": {"headerAuth": "Bearer fake-fixture-only"},
        },
        "queueServerInfo": {
            "serverUrl": "https://queue.invalid/v1/queues",
            "authInfo": {"headerAuth": "Bearer fake-fixture-only"},
        },
    }


def test_cloud_queue_sends_complete_payload_without_mutation_or_logging(caplog):
    import copy
    import logging

    conn, socket = _make_connection_with_fake_socket()
    payload = _cloud_queue_payload()
    original = copy.deepcopy(payload)
    with caplog.at_level(logging.DEBUG):
        loop, task = _drive_send(conn.load_cloud_queue(payload))
        try:
            frame = _last_sent_frame(socket)
            assert frame == {**original, "command": "loadCloudQueue", "requestId": 0}
            assert payload == original
            conn._pending[0].set_result({
                "command": "notifyRequestResult", "requestId": 0, "resultCode": 0,
            })
            assert loop.run_until_complete(task)["resultCode"] == 0
        finally:
            loop.close()
    assert "fake-fixture-only" not in caplog.text
    assert "content.invalid" not in caplog.text


def test_load_track_routes_resolved_payload_to_cloud_queue():
    manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
    payload = _cloud_queue_payload()
    resolver = MagicMock(return_value=payload)
    manager.set_track_url_resolver(resolver)
    manager.load_cloud_queue = MagicMock(return_value={"resultCode": 0})
    assert manager.load_track(123) == {"resultCode": 0}
    resolver.assert_called_once_with(123)
    manager.load_cloud_queue.assert_called_once_with(payload)


def test_manager_load_cloud_queue_calls_connection():
    manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
    conn, _ = _make_connection_with_fake_socket()
    conn.load_cloud_queue = AsyncMock(return_value={"resultCode": 0})
    manager._connection = conn
    manager._call = lambda factory: asyncio.run(factory())
    payload = _cloud_queue_payload()
    assert manager.load_cloud_queue(payload) == {"resultCode": 0}
    conn.load_cloud_queue.assert_awaited_once_with(payload)


def test_legacy_load_media_remains_available():
    conn, socket = _make_connection_with_fake_socket()
    media_info = {"itemId": "fixture-item", "mediaId": "123"}
    loop, task = _drive_send(conn.load_media(media_info))
    try:
        frame = _last_sent_frame(socket)
        assert frame["command"] == "loadMediaInfo"
        assert frame["mediaInfo"] == media_info
        conn._pending[frame["requestId"]].set_result({"resultCode": 0})
        loop.run_until_complete(task)
    finally:
        loop.close()


def test_new_media_clears_a_previous_track_error():
    manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
    manager._record_notification({'command': 'notifyPlaybackError', 'errorCode': 1002})
    manager._record_notification({
        'command': 'notifyMediaChanged', 'mediaInfo': {'mediaId': '456'},
    })
    assert manager.remote_state()['error'] is None


def test_session_error_survives_media_notifications_and_socket_close():
    manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
    manager._record_notification({'command': 'notifySessionError', 'errorCode': 1001})
    manager._record_notification({'command': 'notifyMediaChanged', 'mediaInfo': {'mediaId': '456'}})
    manager._record_notification({'command': 'connectionLost'})
    assert manager.remote_state()['error'] == {'command': 'notifySessionError', 'code': 1001}


def test_failed_reconnect_keeps_output_selected_and_allows_retry(monkeypatch):
    async def check():
        manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
        manager._loop = asyncio.get_running_loop()
        old, _ = _make_connection_with_fake_socket()
        old.close = AsyncMock()
        manager._connection = old
        failed, _ = _make_connection_with_fake_socket()
        failed.open = AsyncMock(side_effect=ConnectionError('offline'))
        failed.close = AsyncMock()
        recovered, _ = _make_connection_with_fake_socket()
        recovered.open = AsyncMock()
        recovered.play = AsyncMock(return_value={'resultCode': 0})
        monkeypatch.setattr(tcr, 'TidalConnectConnection', MagicMock(side_effect=[failed, recovered]))

        with pytest.raises(ConnectionError, match='offline'):
            await asyncio.to_thread(manager.play)
        assert manager.get_connection() is failed
        assert manager.remote_state()['device_id'] == old.device.id
        assert manager.remote_state()['error']['command'] == 'connectionLost'

        assert await asyncio.to_thread(manager.play) == {'resultCode': 0}
        recovered.play.assert_awaited_once()
    asyncio.run(check())


def test_concurrent_commands_share_one_reconnect(monkeypatch):
    async def check():
        manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
        manager._loop = asyncio.get_running_loop()
        old, _ = _make_connection_with_fake_socket()
        old.close = AsyncMock()
        manager._connection = old
        recovered, _ = _make_connection_with_fake_socket()
        opened = asyncio.Event()
        release = asyncio.Event()

        async def open_connection():
            opened.set()
            await release.wait()
            recovered._session_id = 'reconnected'
            recovered._receiver_task = asyncio.create_task(asyncio.sleep(3600))

        recovered.open = AsyncMock(side_effect=open_connection)
        recovered.play = AsyncMock(return_value={'resultCode': 0})
        recovered.set_volume = AsyncMock(return_value={'resultCode': 0})
        factory = MagicMock(return_value=recovered)
        monkeypatch.setattr(tcr, 'TidalConnectConnection', factory)
        first = asyncio.create_task(asyncio.to_thread(manager.play))
        await opened.wait()
        second = asyncio.create_task(asyncio.to_thread(manager.set_volume, 20))
        release.set()
        try:
            await asyncio.gather(first, second)
            assert factory.call_count == 1
            recovered.play.assert_awaited_once()
            recovered.set_volume.assert_awaited_once_with(20)
        finally:
            recovered._receiver_task.cancel()
            await asyncio.gather(recovered._receiver_task, return_exceptions=True)
    asyncio.run(check())


@pytest.mark.parametrize('remote_state,reconnect', [('PAUSED', True), ('IDLE', False)])
def test_resume_restores_receiver_track_and_position_after_session_loss(monkeypatch, remote_state, reconnect):
    async def check():
        manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
        manager._loop = asyncio.get_running_loop()
        conn, _ = _make_connection_with_fake_socket()
        manager._connection = conn
        conn.is_active = MagicMock(return_value=not reconnect)
        conn.play = AsyncMock(return_value={'resultCode': 0})
        conn.load_cloud_queue = AsyncMock(return_value={'resultCode': 0})
        manager._remote_state.update(
            player_state=remote_state, position_ms=12000, duration_ms=180000,
            media_info={'mediaId': '123'}, device_id=conn.device.id,
        )
        payload = _cloud_queue_payload()
        resolver = MagicMock(return_value=payload)
        manager.set_track_url_resolver(resolver)

        async def reconnect_device(device):
            replacement, _ = _make_connection_with_fake_socket()
            replacement.play = conn.play
            replacement.load_cloud_queue = conn.load_cloud_queue
            manager._connection = replacement
            manager._record_notification({'command': 'notifyPlayerStatusChanged', 'playerState': 'IDLE', 'progress': 0})

        manager._connect_device = reconnect_device
        await asyncio.to_thread(manager.resume, 456)
        resolver.assert_called_once_with(123)
        conn.load_cloud_queue.assert_awaited_once_with({**payload, 'position': 12000})
        conn.play.assert_awaited_once()
        assert payload['position'] == 0
    asyncio.run(check())


def test_resume_of_live_paused_queue_does_not_reload():
    async def check():
        manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
        manager._loop = asyncio.get_running_loop()
        conn, _ = _make_connection_with_fake_socket()
        conn.is_active = MagicMock(return_value=True)
        conn.play = AsyncMock(return_value={'resultCode': 0})
        manager._connection = conn
        manager._remote_state['player_state'] = 'PAUSED'
        resolver = MagicMock()
        manager.set_track_url_resolver(resolver)
        await asyncio.to_thread(manager.resume, 456)
        resolver.assert_not_called()
        conn.play.assert_awaited_once()
    asyncio.run(check())


def _receiver_queue_manager():
    from app.audio.tidal_connect_queue import CloudQueue, QueueContext, QueueProvider
    manager = tcr.TidalConnectRealManager(lambda: None, lambda _: None)
    conn, _ = _make_connection_with_fake_socket()
    for method in ('load_cloud_queue', 'select_queue_item', 'play', 'refresh_queue', 'set_repeat_mode'):
        setattr(conn, method, AsyncMock(return_value={'resultCode': 0}))
    manager._connection = conn
    manager._call = lambda factory: asyncio.run(factory())
    ctx = QueueContext((123, 456, 123), (0, 1, 2), 0, 'off', 'revision')
    cloud = CloudQueue({'queueInfo': {'queueId': 'queue'}}, ('a', 'b', 'c'), (123, 456, 123), 0)
    provider = QueueProvider(MagicMock(return_value=cloud), MagicMock(return_value=cloud), MagicMock(return_value={'itemId': 'c'}))
    manager.set_queue_provider(provider)
    manager._store_queue(ctx, cloud)
    manager._remote_state.update(media_info={'mediaId': '123', 'itemId': 'c'}, player_state='PAUSED', position_ms=5000)
    return manager, conn, ctx, cloud, provider


def test_receiver_item_identity_selects_duplicate_queue_position():
    manager, _, _, _, _ = _receiver_queue_manager()
    state = manager.remote_state()
    assert state['queue_index'] == 2
    assert state['queue_has_next'] is False
    assert state['queue_token'] == 'revision'


def test_skip_inside_owned_queue_does_not_create_or_load_new_queue():
    from dataclasses import replace
    manager, conn, ctx, _, provider = _receiver_queue_manager()
    manager.play_queue(replace(ctx, index=2))
    provider.media_info.assert_called_once_with(123, 'c')
    conn.select_queue_item.assert_awaited_once_with({'itemId': 'c'})
    conn.play.assert_awaited_once()
    provider.create.assert_not_called()
    conn.load_cloud_queue.assert_not_awaited()


def test_paused_queue_edit_refreshes_without_play_or_reload():
    from dataclasses import replace
    manager, conn, ctx, cloud, provider = _receiver_queue_manager()
    edited = replace(ctx, repeat='all', index=2, token='edited')
    manager.update_queue(edited)
    provider.update.assert_called_once_with(cloud, [123, 456, 123], 2, 'c')
    conn.set_repeat_mode.assert_awaited_once_with('ALL')
    conn.refresh_queue.assert_awaited_once_with('queue')
    conn.play.assert_not_awaited()
    conn.load_cloud_queue.assert_not_awaited()
    assert manager.remote_state()['queue_has_next'] is True


def test_failed_queue_load_restores_previous_ownership():
    from dataclasses import replace
    manager, conn, ctx, cloud, _ = _receiver_queue_manager()
    conn.load_cloud_queue.side_effect = RuntimeError('receiver rejected load')
    with pytest.raises(RuntimeError, match='receiver rejected'):
        manager.play_queue(replace(ctx, token='new-queue'))
    assert manager._queue_context is ctx
    assert manager._cloud_queue is cloud


def test_shutdown_closes_session_without_issuing_a_playback_stop(monkeypatch):
    from concurrent.futures import Future
    manager, conn, _, _, _ = _receiver_queue_manager()
    conn.close = AsyncMock()
    conn.stop = AsyncMock()
    loop = MagicMock()
    manager._loop = loop
    def schedule(coroutine, target):
        assert target is loop
        future = Future()
        future.set_result(asyncio.run(coroutine))
        return future
    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe', schedule)
    manager.stop()
    conn.close.assert_awaited_once()
    conn.stop.assert_not_awaited()
    assert manager.get_connection() is None
    loop.call_soon_threadsafe.assert_called_once_with(loop.stop)
