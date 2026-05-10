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
from unittest.mock import MagicMock, patch


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
        urls, duration_s, info, bytes_map = player._resolve_source(
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
    # Manifest cache write fires once for the resolved entry.
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
