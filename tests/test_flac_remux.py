"""Tests for the MP4-container FLAC → native .flac remux step.

Tidal's hi-res FLAC arrives as fragmented MP4 segments — the audio
codec inside is FLAC but the container is MP4, so tidalapi labels
the file extension `.m4a`. Without remuxing, users end up with
`.m4a` files that hold lossless FLAC audio: misleading extension,
non-standard container, players that prefer native FLAC framing
(metadata editors, hardware FLAC players) struggle.

These tests pin the call shape: the helper must use PyAV's
stream-copy pattern (no decode / encode), and the wrapper that
invokes it must only fire when the codec is FLAC and the hint
isn't already `.flac`.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import downloader
from app.downloader import _remux_mp4_to_flac


# ---------------------------------------------------------------------------
# _remux_mp4_to_flac — call-shape tests against a mocked `av` module.
# ---------------------------------------------------------------------------


def _build_fake_av(*, packets, has_audio_stream=True):
    """Construct a MagicMock standing in for the PyAV `av` module.

    `packets` is a list of objects whose `.dts` attribute we use to
    drive the demux loop. The output container's mux call records
    packets so the test can assert on them.
    """
    fake_av = MagicMock()

    audio_stream = MagicMock(name="in_audio_stream")
    streams_audio = [audio_stream] if has_audio_stream else []
    input_container = MagicMock(name="input_container")
    input_container.streams.audio = streams_audio
    input_container.demux.return_value = iter(packets)

    out_stream = MagicMock(name="out_stream")
    output_container = MagicMock(name="output_container")
    output_container.add_stream_from_template.return_value = out_stream

    # av.open(input_path) → input_container; av.open(out, mode='w', format='flac') → output
    def _open(path, mode="r", format=None):
        if mode == "w":
            return output_container
        return input_container

    fake_av.open.side_effect = _open
    fake_av._input_container = input_container
    fake_av._output_container = output_container
    fake_av._out_stream = out_stream
    return fake_av


def test_remux_uses_stream_copy_into_flac_container(tmp_path):
    """The output container must be opened in write mode with format
    'flac', and packets must be muxed verbatim into it (stream-copy)."""
    packets = [SimpleNamespace(dts=0), SimpleNamespace(dts=1024), SimpleNamespace(dts=2048)]
    fake_av = _build_fake_av(packets=packets)

    mp4_path = tmp_path / "in.mp4"
    flac_path = tmp_path / "out.flac"

    with patch.dict("sys.modules", {"av": fake_av}):
        _remux_mp4_to_flac(mp4_path, flac_path)

    # av.open called twice: input (read), output (write with format=flac).
    open_calls = fake_av.open.call_args_list
    assert len(open_calls) == 2
    # Input
    assert open_calls[0].args == (str(mp4_path),)
    # Output — keyword args because the helper passes mode + format by name.
    assert open_calls[1].args == (str(flac_path),)
    assert open_calls[1].kwargs.get("mode") == "w"
    assert open_calls[1].kwargs.get("format") == "flac"

    # add_stream_from_template called with the input audio stream.
    fake_av._output_container.add_stream_from_template.assert_called_once_with(
        fake_av._input_container.streams.audio[0]
    )

    # Each non-flush packet got muxed onto the output stream.
    mux_calls = fake_av._output_container.mux.call_args_list
    assert len(mux_calls) == len(packets)
    for call, pkt in zip(mux_calls, packets):
        assert call.args[0] is pkt
        assert pkt.stream is fake_av._out_stream


def test_remux_skips_flush_packets(tmp_path):
    """libav emits None-DTS flush packets at the end of demux; the
    remux loop must skip them or the FLAC muxer raises."""
    packets = [
        SimpleNamespace(dts=0),
        SimpleNamespace(dts=None),  # flush — must be skipped
        SimpleNamespace(dts=1024),
    ]
    fake_av = _build_fake_av(packets=packets)

    with patch.dict("sys.modules", {"av": fake_av}):
        _remux_mp4_to_flac(tmp_path / "in.mp4", tmp_path / "out.flac")

    # 3 packets in, 2 muxed (the None-DTS one was dropped).
    assert fake_av._output_container.mux.call_count == 2


def test_remux_raises_when_input_has_no_audio(tmp_path):
    fake_av = _build_fake_av(packets=[], has_audio_stream=False)
    with patch.dict("sys.modules", {"av": fake_av}):
        with pytest.raises(RuntimeError, match="no audio stream"):
            _remux_mp4_to_flac(tmp_path / "in.mp4", tmp_path / "out.flac")


def test_remux_closes_both_containers_even_on_error(tmp_path):
    """If muxing raises mid-loop, both input and output containers
    must close — otherwise temp files stay locked on Windows."""
    boom_packet = MagicMock()
    boom_packet.dts = 1024
    fake_av = _build_fake_av(packets=[boom_packet])
    fake_av._output_container.mux.side_effect = RuntimeError("muxer exploded")

    with patch.dict("sys.modules", {"av": fake_av}):
        with pytest.raises(RuntimeError, match="muxer exploded"):
            _remux_mp4_to_flac(tmp_path / "in.mp4", tmp_path / "out.flac")

    fake_av._input_container.close.assert_called_once()
    fake_av._output_container.close.assert_called_once()


# ---------------------------------------------------------------------------
# _fetch_stream_sources — codec is included in the return tuple.
# ---------------------------------------------------------------------------


def _build_downloader(*, is_pkce, manifest=None, direct_url=None):
    """Construct a Downloader with mocked tidalapi session, bypassing
    worker thread spawn (we only call _fetch_stream_sources)."""
    settings = SimpleNamespace(
        concurrent_downloads=1,
        download_rate_limit_mbps=0,
        output_dir=str(Path.cwd()),
    )
    fake_session = SimpleNamespace(
        is_pkce=is_pkce,
        config=SimpleNamespace(quality="high_lossless"),
    )
    fake_tidal = SimpleNamespace(session=fake_session)

    track = MagicMock(name="track")
    if manifest is not None:
        stream = MagicMock()
        stream.get_stream_manifest.return_value = manifest
        track.get_stream.return_value = stream
    if direct_url is not None:
        track.get_url.return_value = direct_url

    # Bypass __init__ to skip worker-thread spawn.
    dl = downloader.Downloader.__new__(downloader.Downloader)
    dl.tidal = fake_tidal
    dl.settings = settings
    dl.quality_lock = __import__("threading").Lock()
    dl._rate_limit_until = 0.0
    dl._rate_limit_lock = __import__("threading").Lock()
    dl._cancelled_ids = set()
    dl._cancelled_lock = __import__("threading").Lock()
    return dl, track


def test_fetch_stream_sources_returns_codec_for_pkce_dash_flac():
    manifest = SimpleNamespace(
        urls=["https://cdn/seg-init.mp4", "https://cdn/seg-1.mp4"],
        file_extension=".m4a",
        codecs="FLAC",
        is_encrypted=False,
    )
    dl, track = _build_downloader(is_pkce=True, manifest=manifest)

    urls, ext, codec = dl._fetch_stream_sources(track, quality=None)

    assert urls == manifest.urls
    assert ext == ".m4a"  # what tidalapi reported — caller decides what to do with it
    assert codec == "FLAC"


def test_fetch_stream_sources_uppercases_codec_string():
    """Defensive: tidalapi already uppercases on the BTS path, but
    the DASH path comes from raw MPD codec strings. The downloader
    compares against 'FLAC' — case must be normalized at the source."""
    manifest = SimpleNamespace(
        urls=["https://cdn/x.flac"],
        file_extension=".flac",
        codecs="flac",  # lowercase from a hypothetical MPD path
        is_encrypted=False,
    )
    dl, track = _build_downloader(is_pkce=True, manifest=manifest)

    _, _, codec = dl._fetch_stream_sources(track, quality=None)
    assert codec == "FLAC"


def test_fetch_stream_sources_returns_none_codec_for_device_code():
    """Device-code sessions don't have a manifest — codec is None."""
    dl, track = _build_downloader(
        is_pkce=False, direct_url="https://cdn/song.flac?token=x"
    )

    urls, ext, codec = dl._fetch_stream_sources(track, quality=None)

    assert urls == ["https://cdn/song.flac?token=x"]
    assert ext is None
    assert codec is None


def test_fetch_stream_sources_handles_non_string_codec_attr():
    """If tidalapi ever returns a non-string codec value (older or
    newer schema), don't crash — degrade to None."""
    manifest = SimpleNamespace(
        urls=["https://cdn/x.flac"],
        file_extension=".flac",
        codecs=None,
        is_encrypted=False,
    )
    dl, track = _build_downloader(is_pkce=True, manifest=manifest)

    _, _, codec = dl._fetch_stream_sources(track, quality=None)
    assert codec is None
