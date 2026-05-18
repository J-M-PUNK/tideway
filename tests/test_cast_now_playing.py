"""Unit coverage for the Chromecast now-playing + latency fix.

The end-to-end behaviour (card on the TV, A/V latency) can only be
confirmed on a real Chromecast. What is verifiable here is the logic
that drives it: the metadata card shape, the live-edge buffer flush,
and that a track change resets the encoder + flushes the buffer +
re-issues play_media exactly once per real change (not per position
tick).
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from app.audio.cast import CastManager, _music_metadata
from app.audio.http_stream import RingBuffer


def test_ring_buffer_flush_drops_backlog_keeps_open():
    rb = RingBuffer(max_bytes=1024)
    rb.write(b"old-backlog-bytes")
    rb.flush()
    assert not rb.is_closed
    rb.write(b"live")
    assert rb.read(64, timeout=0.1) == b"live"


def test_music_metadata_shape_and_fallbacks():
    full = _music_metadata("Song", "Artist", "Album", "http://art/x.jpg")
    assert full == {
        "metadataType": 3,
        "title": "Song",
        "artist": "Artist",
        "albumName": "Album",
        "images": [{"url": "http://art/x.jpg"}],
    }
    # Empty fields are omitted; title never blank.
    assert _music_metadata("", "", "", "") == {
        "metadataType": 3,
        "title": "Tideway",
    }


def _fake_session():
    calls = []
    mc = SimpleNamespace(
        play_media=lambda *a, **k: calls.append(("play_media", a, k)),
        block_until_active=lambda timeout=0: calls.append(("wait", timeout)),
    )
    buf = SimpleNamespace(
        flushed=0,
    )
    buf.flush = lambda: setattr(buf, "flushed", buf.flushed + 1)
    sess = SimpleNamespace(
        encoder=object(),
        encoder_lock=threading.Lock(),
        encoder_rate=44100,
        encoder_channels=2,
        encoder_dtype="int16",
        buffer=buf,
        now_playing=None,
        stream_url="http://lan:9/cast/stream",
        cast=SimpleNamespace(media_controller=mc),
    )
    return sess, calls, buf


def test_set_now_playing_no_session_is_noop_but_caches():
    m = CastManager()
    m.set_now_playing("T", "A", "Al", "art")
    assert m._last_np == _music_metadata("T", "A", "Al", "art")  # seeded


def test_track_change_resets_encoder_flushes_and_reloads_once():
    m = CastManager()
    sess, calls, buf = _fake_session()
    m._session = sess

    m.set_now_playing("Song 1", "Artist", "Album", "art1")
    assert sess.encoder is None, "encoder dropped so a fresh header leads"
    assert sess.encoder_rate == 0
    assert buf.flushed == 1, "buffer flushed to the live edge"
    assert sess.now_playing == _music_metadata(
        "Song 1", "Artist", "Album", "art1"
    )
    play_calls = [c for c in calls if c[0] == "play_media"]
    assert len(play_calls) == 1
    assert play_calls[0][2]["metadata"]["title"] == "Song 1"
    assert play_calls[0][2]["stream_type"] == "LIVE"

    # Same track again (position ticks) -> no second reload/flush.
    m.set_now_playing("Song 1", "Artist", "Album", "art1")
    assert buf.flushed == 1
    assert len([c for c in calls if c[0] == "play_media"]) == 1

    # Real change -> exactly one more reset + reload.
    m.set_now_playing("Song 2", "Artist", "Album", "art2")
    assert buf.flushed == 2
    assert len([c for c in calls if c[0] == "play_media"]) == 2
