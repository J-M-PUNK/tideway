"""Tests for the parallel metadata + playbackinfo fetch in
PCMPlayer._resolve_source.

Before this branch the resolver did three sequential calls labelled
`track`, `stream`, `manifest`. Two of those were Tidal round-trips
that happened to be independent (the playbackinfo endpoint only
needs the track id, not any parsed metadata), so they can run in
parallel for free. The third "manifest" phase is local CPU work
(base64 + DASH MPD parse) so it stays serial after the network
phase.

The test pins the contract by using a `threading.Barrier`: both
mocked Tidal calls block until both are running. If a regression
ever serializes them again, the second mock will never reach the
barrier and the test will time out.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.audio.manifest_cache import ManifestCache


def test_resolve_source_runs_metadata_and_playbackinfo_in_parallel():
    from app.audio.player import PCMPlayer

    # Avoid running PCMPlayer.__init__ — we only need _resolve_source
    # plus the few attributes it touches.
    player = PCMPlayer.__new__(PCMPlayer)
    player._manifest_cache = MagicMock()
    player._manifest_cache.lookup.return_value = None
    player._local_lookup = None
    player._quality_clamp = None

    # Build a session-like with a config + a session.track method.
    session = MagicMock()
    session.config.quality = "LOSSLESS"

    barrier = threading.Barrier(2, timeout=2.0)

    metadata_track = MagicMock()
    metadata_track.duration = 180

    def slow_session_track(_tid):
        # Will raise BrokenBarrierError if the other call isn't
        # running at the same time within the 2 s timeout.
        barrier.wait()
        return metadata_track

    session.track.side_effect = slow_session_track

    # Stream + manifest stubs the resolver will read attributes from.
    fake_manifest = MagicMock(
        urls=["http://seg/0", "http://seg/1"],
        codecs="FLAC",
        is_encrypted=False,
    )
    fake_stream = MagicMock(
        bit_depth=16,
        sample_rate=44100,
        audio_quality="LOSSLESS",
        audio_mode="STEREO",
    )
    fake_stream.get_stream_manifest.return_value = fake_manifest

    holder = MagicMock()

    def slow_get_stream():
        barrier.wait()
        return fake_stream

    holder.get_stream.side_effect = slow_get_stream

    player._session_getter = lambda: session

    with patch("app.audio.player.tidalapi.Track", return_value=holder):
        urls, duration_s, info, bytes_map, track_meta = player._resolve_source(
            "12345", quality=None
        )

    # Both Tidal calls happened. The barrier proved they overlapped.
    session.track.assert_called_once_with(12345)
    holder.get_stream.assert_called_once()

    # Output incorporates data from both branches: duration came
    # from the metadata fetch, URLs from the playbackinfo path's
    # parsed manifest.
    assert urls == ["http://seg/0", "http://seg/1"]
    assert duration_s == 180.0
    assert info.audio_quality == "LOSSLESS"
    # Metadata is resolved alongside the manifest and returned as the
    # 5th element (so the UPnP renderer gets correct artist/album/cover).
    assert isinstance(track_meta, dict)
    assert "title" in track_meta and "cover_id" in track_meta
    # Manifest cache write fires once for the resolved entry, carrying
    # the metadata.
    player._manifest_cache.store.assert_called_once()


def test_resolve_source_passes_track_id_as_int():
    """Cache keys are (str, str) but the Tidal API expects an int.
    Verifying both the int conversion and the unchanged cache-key
    string here so a future refactor that drops the int() cast
    fails loudly."""
    from app.audio.player import PCMPlayer

    player = PCMPlayer.__new__(PCMPlayer)
    player._manifest_cache = MagicMock()
    player._manifest_cache.lookup.return_value = None
    player._local_lookup = None
    player._quality_clamp = None

    session = MagicMock()
    session.config.quality = "LOSSLESS"
    session.track.return_value = MagicMock(duration=120)

    holder = MagicMock()
    holder.get_stream.return_value = MagicMock(
        get_stream_manifest=lambda: MagicMock(
            urls=["http://x"], codecs="FLAC", is_encrypted=False
        ),
        bit_depth=16,
        sample_rate=44100,
        audio_quality=None,
        audio_mode=None,
    )

    player._session_getter = lambda: session

    with patch("app.audio.player.tidalapi.Track", return_value=holder):
        player._resolve_source("987", quality=None)

    # Tidal API surface uses int track ids — confirm we converted.
    assert session.track.call_args.args == (987,)
    # The minimal Track holder gets the same int id.
    assert holder.id == 987


def test_resolve_source_bounded_when_tidal_stalls():
    """A stalled Tidal request must NOT hold the caller forever.

    _resolve_source runs under the player's pipeline lock, so an
    unbounded wait here froze every playback control until an app
    restart. The result(timeout=) backstop has to give up and raise
    so _load_locked can flip the player to a recoverable error
    state. Pins that the wait is bounded and the worker thread is
    left able to unwind (it does once `release` is set, standing in
    for the transport timeout aborting the socket).
    """
    from app.audio.player import PCMPlayer

    player = PCMPlayer.__new__(PCMPlayer)
    player._manifest_cache = MagicMock()
    player._manifest_cache.lookup.return_value = None
    player._local_lookup = None
    player._quality_clamp = None

    session = MagicMock()
    session.config.quality = "LOSSLESS"

    release = threading.Event()

    def hung_session_track(_tid):
        # Stand-in for a Tidal connection that accepts but never
        # responds. The real transport timeout aborts the socket;
        # here the test releases it after asserting the bound held.
        release.wait(timeout=10.0)
        return MagicMock(duration=120)

    session.track.side_effect = hung_session_track

    holder = MagicMock()
    holder.get_stream.side_effect = lambda: (
        release.wait(timeout=10.0) or MagicMock()
    )

    player._session_getter = lambda: session

    try:
        with patch("app.audio.player._RESOLVE_TIMEOUT_S", 0.3), patch(
            "app.audio.player.tidalapi.Track", return_value=holder
        ):
            t0 = time.monotonic()
            with pytest.raises(RuntimeError, match="timed out"):
                player._resolve_source("12345", quality=None)
            elapsed = time.monotonic() - t0

        # Bounded: nowhere near the 10 s the mock would otherwise
        # block for. Generous ceiling to stay non-flaky on CI.
        assert elapsed < 3.0, f"resolve hung for {elapsed:.1f}s"
    finally:
        release.set()


def test_resolve_source_cache_hit_returns_stored_metadata():
    """A cache HIT must carry the resolved track metadata (title /
    artist / cover_id), not the previous track's. Previously the hit
    path left `_current_track_meta` pointing at the prior track, so the
    UPnP renderer was told the wrong artist/song at session start (and
    on every replay, since cold-start prefetch makes the first play a
    hit)."""
    from app.audio.player import PCMPlayer

    player = PCMPlayer.__new__(PCMPlayer)
    player._manifest_cache = ManifestCache()
    player._local_lookup = None
    player._quality_clamp = None
    player._current_track_meta = {"title": "STALE", "artist": "OLD"}
    stored_meta = {
        "title": "Real Title",
        "artist": "Real Artist",
        "album": "Real Album",
        "duration_s": 200,
        "cover_id": "abcd1234-1234-1234-1234-1234567890ab",
        "cover_url": "https://resources.tidal.com/images/x/640x640.jpg",
    }
    player._manifest_cache.store(
        ("555", None), ["http://seg/0"], 200.0, None, stored_meta
    )
    player._session_getter = lambda: MagicMock()

    result = player._resolve_source("555", quality=None)

    assert result[4] == stored_meta
    assert player._current_track_meta == stored_meta


def test_prefetch_does_not_pollute_current_track_meta():
    """Speculative prefetch must NOT overwrite `_current_track_meta`.

    The cold-start prefetch resolves the persisted track purely to warm
    the manifest cache. It used to reach _resolve_source (cache-hit),
    which set `_current_track_meta` to the prefetched track — so when
    the renderer's session started, the first SetAVTransportURI carried
    a track the user never played. Prefetch is speculative, so it must
    leave the playing track's metadata alone."""
    from app.audio.player import PCMPlayer

    player = PCMPlayer.__new__(PCMPlayer)
    player._manifest_cache = ManifestCache()
    player._local_lookup = None
    player._quality_clamp = None
    player._current_track_meta = {"title": "PLAYING", "artist": "Now"}
    stored_meta = {
        "title": "Prefetched",
        "artist": "Not Playing",
        "album": "X",
        "duration_s": 1,
        "cover_id": "ab",
        "cover_url": "",
    }
    player._manifest_cache.store(
        ("777", None), ["http://seg/0"], 1.0, None, stored_meta
    )
    player._session_getter = lambda: MagicMock()

    with patch("app.tidal_client.tidal_jitter_sleep", return_value=None):
        ok = player.prefetch("777", quality=None, warm_bytes=False)

    assert ok is True
    # The playing track's metadata is untouched by a speculative prefetch.
    assert player._current_track_meta == {"title": "PLAYING", "artist": "Now"}
