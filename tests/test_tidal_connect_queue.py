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


def test_receiver_queue_preserves_duplicate_positions_and_repeat():
    from app.audio.tidal_connect_queue import create_playback_queue
    s = session()
    s.request.basic_request.return_value = Mock(ok=True, headers={'etag': 'v1'}, json=lambda: {
        'id': 'queue', 'items': [{'id': 'first'}, {'id': 'second'}, {'id': 'third'}],
    })
    queue = create_playback_queue(s, [123, 456, 123], 2, 'LOSSLESS', 'one', False, 9000)
    assert queue.item_ids == ('first', 'second', 'third')
    assert queue.payload['currentMediaInfo']['itemId'] == 'third'
    assert queue.payload['queueInfo']['repeatMode'] == 'ONE'
    assert queue.payload['autoplay'] is False
    assert queue.payload['position'] == 9000
    assert json.loads(s.request.basic_request.call_args.kwargs['data'])['repeat_mode'] == 'one'
    assert 'test-access' not in repr(queue)


def test_large_queue_uploads_every_item_before_handoff():
    from app.audio.tidal_connect_queue import create_playback_queue
    s = session()
    calls = []
    def request(method, url, **kwargs):
        body = json.loads(kwargs['data'])
        calls.append((method, body, kwargs['headers'].get('If-Match')))
        return Mock(ok=True, headers={'etag': str(len(calls))}, json=lambda: {
            'id': 'queue', 'items': [{'id': f"item-{i['media_id']}"} for i in body['items']],
        })
    s.request.basic_request.side_effect = request
    queue = create_playback_queue(s, list(range(1, 351)), 175, 'LOSSLESS')
    assert queue.item_ids == tuple(f'item-{i}' for i in range(1, 351))
    assert queue.payload['currentMediaInfo']['itemId'] == 'item-176'
    assert calls[0][1]['properties']['position'] == 50
    assert all(len(body['items']) <= 100 for _, body, _ in calls)
    assert [etag for _, _, etag in calls] == [None, '1', '2', '3', '4']


def test_queue_edit_preserves_actual_duplicate_and_uses_conditional_mutations():
    from app.audio.tidal_connect_queue import CloudQueue, update_playback_queue
    s = session()
    cloud = CloudQueue({'queueInfo': {'queueId': 'queue'}}, ('a', 'b', 'c'), (123, 456, 123), 2)
    responses = [
        Mock(ok=True, headers={'etag': '1'}),
        Mock(ok=True, headers={'etag': '2'}, json=lambda: {'items': [{'id': 'new'}]}),
        Mock(ok=True, headers={'etag': '3'}),
        Mock(ok=True, headers={'etag': '4'}),
    ]
    s.request.basic_request.side_effect = responses
    edited = update_playback_queue(s, cloud, [123, 789, 123], 0, 'c')
    assert edited.item_ids == ('c', 'new', 'a')
    calls = s.request.basic_request.call_args_list
    assert [c.args[0] for c in calls] == ['GET', 'PUT', 'DELETE', 'PATCH']
    assert calls[2].args[1].endswith('/items/b')
    assert json.loads(calls[3].kwargs['data']) == {'after': 'c', 'ids': ['new', 'a']}
    assert [c.kwargs['headers'].get('If-Match') for c in calls] == [None, '1', '2', '3']


def test_sleep_queue_truncates_shuffled_future():
    from app.audio.tidal_connect_queue import QueueContext
    ctx = QueueContext((123, 456, 789), (2, 0, 1), 0, 'all', 'revision', stop_after_current=True)
    assert ctx.ordered_ids == [789, 123]


@pytest.mark.parametrize('order,index', [((0, 0), 0), ((0, 1), 2), ((1,), 0)])
def test_invalid_queue_context_is_rejected(order, index):
    from app.audio.tidal_connect_queue import QueueContext
    with pytest.raises(ValueError):
        QueueContext((123, 456), order, index, 'off', 'revision')


def test_large_queue_reorder_batches_moves_and_chains_etags():
    from app.audio.tidal_connect_queue import CloudQueue, update_playback_queue
    s = session()
    ids = tuple(range(1, 251))
    items = tuple(f'item-{i}' for i in ids)
    cloud = CloudQueue({'queueInfo': {'queueId': 'queue'}}, items, ids, 0)
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, kwargs))
        return Mock(ok=True, headers={'etag': str(len(calls))})
    s.request.basic_request.side_effect = request
    desired = list(reversed(ids))
    edited = update_playback_queue(s, cloud, desired, 0, items[-1])
    assert edited.item_ids == tuple(reversed(items))
    moves = [json.loads(kwargs['data']) for method, kwargs in calls if method == 'PATCH']
    assert [len(move['ids']) for move in moves] == [100, 100, 49]
    assert moves[0]['after'] == edited.item_ids[0]
    assert moves[1]['after'] == moves[0]['ids'][-1]
    assert moves[2]['after'] == moves[1]['ids'][-1]
    assert [kwargs['headers'].get('If-Match') for _, kwargs in calls] == [None, '1', '2', '3']
