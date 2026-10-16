"""Cloud queues must use server-assigned item IDs and receiver metadata units."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.audio.tidal_connect_queue import create_track_queue


def session():
    s = SimpleNamespace(
        access_token='test-access', refresh_token='test-refresh', locale='en_US',
        request=Mock(), track=Mock(),
    )
    s.track.return_value = SimpleNamespace(
        name='Test track', duration=272, artists=[SimpleNamespace(name='Artist')],
        album=SimpleNamespace(name='Album', cover='abc-def'),
    )
    s.request.basic_request.return_value = Mock(
        ok=True, json=lambda: {'id':'cloud-queue','items':[{'id':'cloud-item'}]},
    )
    return s


def test_cloud_queue_uses_assigned_ids_and_receiver_metadata():
    s = session()
    payload = create_track_queue(s, 123, 'HI_RES_LOSSLESS')
    call = s.request.basic_request.call_args
    assert call.args == ('POST', 'https://connectqueue.tidal.com/v1/queues')
    body = json.loads(call.kwargs['data'])
    assert body['items'][0]['media_id'] == '123'
    assert body['repeat_mode'] == 'off'
    assert payload['queueInfo']['queueId'] == 'cloud-queue'
    media = payload['currentMediaInfo']
    assert media['itemId'] == 'cloud-item'
    assert media['metadata']['duration'] == 272000
    assert media['metadata']['artists'] == ['Artist']
    assert media['metadata']['images']['high']['width'] == 1280
    assert 'srcUrl' not in media
    for name in ('contentServerInfo','queueServerInfo'):
        oauth = payload[name]['authInfo']['oauthServerInfo']
        assert oauth['formParameters'] == {'grant_type':'switch_client','scope':'r_usr'}
        assert oauth['authInfo']['oauthParameters']['accessToken'] == 'test-access'
    assert payload['contentServerInfo']['queryParameters']['audioquality'] == 'HI_RES_LOSSLESS'


def test_credentials_are_read_after_refresh():
    s = session()
    def request(*args, **kwargs):
        s.access_token = 'refreshed-access'
        return Mock(ok=True, json=lambda: {'id':'q','items':[{'id':'i'}]})
    s.request.basic_request.side_effect = request
    payload = create_track_queue(s, 123, 'LOSSLESS')
    assert payload['contentServerInfo']['authInfo']['oauthServerInfo']['authInfo']['headerAuth'] == 'Bearer refreshed-access'


@pytest.mark.parametrize('status', [401,403,429,500])
def test_queue_rejection_is_safe(status):
    s = session()
    s.request.basic_request.return_value = Mock(ok=False,status_code=status,text='secret-token')
    with pytest.raises(RuntimeError, match=f'HTTP {status}') as exc:
        create_track_queue(s,123,'LOSSLESS')
    assert 'secret-token' not in str(exc.value)


@pytest.mark.parametrize('body',[{}, {'id':'q','items':[]}, {'id':'q','items':[{}]}])
def test_incomplete_queue_is_rejected(body):
    s = session()
    s.request.basic_request.return_value = Mock(ok=True, json=lambda: body)
    with pytest.raises(RuntimeError, match='incomplete'):
        create_track_queue(s,123,'LOSSLESS')
