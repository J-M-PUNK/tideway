"""Receiver state must replace the muted local player's state."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import server
from app.audio import tidal_connect_real

@pytest.fixture
def receiver(monkeypatch):
    mgr = Mock()
    mgr.is_active.return_value = True
    mgr.get_connection.return_value = object()
    mgr.remote_state.return_value = {
        'player_state': 'PAUSED', 'position_ms': 12000, 'duration_ms': 180000,
        'volume': 0, 'muted': True, 'seq': 3, 'device_id': 'wiim-test',
        'media_info': {'mediaId': '123'}, 'error': None,
    }
    monkeypatch.setattr(tidal_connect_real, 'get_manager', lambda: mgr)
    monkeypatch.setattr(server, '_require_local_access', lambda: None)
    return mgr

def test_state_reports_paused_receiver_instead_of_playing_local(receiver, monkeypatch):
    local = Mock()
    local.snapshot.return_value = SimpleNamespace(state='playing', seq=400)
    monkeypatch.setattr(server, '_native_player', lambda: local)
    snap = server.player_state()
    assert snap['state'] == 'paused'
    assert snap['track_id'] == '123'
    assert snap['output_id'] == 'wiim-test'
    assert snap['position_ms'] == 12000
    assert snap['duration_ms'] == 180000
    assert snap['volume'] == 0
    local.snapshot.assert_not_called()

def test_volume_and_mute_target_receiver(receiver, monkeypatch):
    local = Mock()
    monkeypatch.setattr(server, '_native_player', lambda: local)
    server.player_volume(server._PlayerVolumeRequest(volume=20))
    server.player_muted(server._PlayerMutedRequest(muted=True))
    receiver.set_volume.assert_called_once_with(20)
    receiver.set_mute.assert_called_once_with(True)
    local.set_volume.assert_not_called()
    local.set_muted.assert_not_called()

def test_receiver_error_is_reported_without_details(receiver):
    receiver.remote_state.return_value.update(error={'command':'play','code':1002})
    snap = server.player_state()
    assert snap['state'] == 'error'
    assert snap['error'] == 'Tidal Connect play failed (code 1002)'

def test_dead_receiver_selection_does_not_fall_back_to_local(receiver, monkeypatch):
    receiver.is_active.return_value = False
    receiver.remote_state.return_value.update(error={'command':'connection','code':None})
    local = Mock()
    monkeypatch.setattr(server, '_native_player', lambda: local)
    assert server.player_state()['state'] == 'error'
    local.snapshot.assert_not_called()


@pytest.mark.parametrize('command,scope', [
    ('connectionLost', 'session'), ('notifySessionError', 'session'),
    ('notifyQueueServerError', 'session'), ('notifyPlaybackError', 'track'),
    ('notifyContentServerError', 'track'),
])
def test_receiver_error_scope(receiver, command, scope):
    receiver.remote_state.return_value['error'] = {'command': command, 'code': 1002}
    assert server.player_state()['error_scope'] == scope


def test_healthy_receiver_has_no_error_scope(receiver):
    assert server.player_state()['error_scope'] is None


@pytest.mark.parametrize('endpoint', [server.player_play, server.player_resume])
def test_resume_passes_persisted_fallback_to_manager(receiver, monkeypatch, endpoint):
    monkeypatch.setattr(server.now_playing_state, 'read_state', lambda: {'track': {'id': '456'}})
    endpoint()
    receiver.resume.assert_called_once_with(456)
    receiver.load_track.assert_not_called()


@pytest.mark.parametrize('player_state,position,duration,expected', [
    ('PAUSED', 237653, 237000, 'ended'),  # observed WiiM completion
    ('PAUSED', 237000, 237000, 'ended'),
    ('PAUSED', 236999, 237000, 'paused'),
    ('PAUSED', 12000, 237000, 'paused'),
    ('PAUSED', 0, 0, 'paused'),
    ('PLAYING', 237653, 237000, 'playing'),
    ('IDLE', 237653, 237000, 'idle'),
])
def test_receiver_completion_requires_pause_at_known_track_end(receiver, player_state, position, duration, expected):
    receiver.remote_state.return_value.update(
        player_state=player_state, position_ms=position, duration_ms=duration,
    )
    assert server.player_state()['state'] == expected


def test_connection_loss_at_track_end_does_not_complete_or_advance(receiver):
    receiver.remote_state.return_value.update(
        player_state='IDLE', position_ms=237653, duration_ms=237000,
        error={'command': 'connectionLost', 'code': None},
    )
    snapshot = server.player_state()
    assert snapshot['state'] == 'error'
    assert snapshot['error_scope'] == 'session'


def test_paused_receiver_without_media_identity_does_not_complete(receiver):
    receiver.remote_state.return_value.update(
        media_info=None, position_ms=237653, duration_ms=237000,
    )
    assert server.player_state()['state'] == 'paused'


@pytest.mark.parametrize('has_next,expected', [(True, 'paused'), (False, 'ended')])
def test_native_queue_does_not_request_frontend_advance_between_items(receiver, has_next, expected):
    receiver.remote_state.return_value.update(
        player_state='PAUSED', position_ms=180000, duration_ms=180000,
        queue_owner='receiver', queue_index=2, queue_token='revision', queue_has_next=has_next,
    )
    snap = server.player_state()
    assert snap['state'] == expected
    assert snap['queue_index'] == 2
    assert snap['queue_token'] == 'revision'


def test_queue_context_must_match_requested_track(receiver):
    req = server._PlayerLoadRequest(track_id='123', queue={
        'track_ids': [123, 456], 'order': [0, 1], 'index': 1,
        'repeat': 'off', 'token': 'revision',
    })
    with pytest.raises(server.HTTPException) as exc:
        server.player_play_track(req)
    assert exc.value.status_code == 422
    receiver.play_queue.assert_not_called()
