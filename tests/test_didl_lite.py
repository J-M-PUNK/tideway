"""Tests for the DIDL-Lite generator (slice 4).

DIDL-Lite is the UPnP-AV metadata XML embedded in the Metadata
argument of `Playlist.Insert`. The device parses it to display
title / artist / album / cover and to choose a renderer based on
the protocolInfo. These tests pin the output structure against
the requirements that real OpenHome devices enforce, derived from
the published UPnP-AV spec and observed Bluesound / Linn quirks
documented in scattered home-assistant issues.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from app.audio.openhome import TrackMetadata, build_didl_lite, _format_duration


def _track(**overrides) -> TrackMetadata:
    base = dict(
        title="Cry For Me",
        artist="The Weeknd",
        album="Hurry Up Tomorrow",
        duration_s=240,
        cover_url="http://images.tidal.com/cover.jpg",
        track_uri="http://stream.tidal.com/track.flac",
        mime_type="audio/flac",
    )
    base.update(overrides)
    return TrackMetadata(**base)


# ---------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------


class TestFormatDuration:
    def test_under_one_hour(self):
        """UPnP-AV format: H:MM:SS.fff. Single-digit hour is correct
        for tracks under an hour. Some devices reject `00:` prefix."""
        assert _format_duration(240) == "0:04:00.000"

    def test_over_one_hour(self):
        assert _format_duration(3700) == "1:01:40.000"

    def test_zero(self):
        assert _format_duration(0) == "0:00:00.000"

    def test_negative_clamped_to_zero(self):
        """A buggy caller passing a negative duration shouldn't
        produce something the device chokes on."""
        assert _format_duration(-5) == "0:00:00.000"

    def test_seconds_padded(self):
        assert _format_duration(5) == "0:00:05.000"

    def test_minutes_padded(self):
        assert _format_duration(65) == "0:01:05.000"


# ---------------------------------------------------------------------
# build_didl_lite — structure
# ---------------------------------------------------------------------


class TestBuildDidlLiteStructure:
    def test_is_well_formed_xml(self):
        """Output has to parse cleanly — devices reject malformed
        DIDL-Lite. Slice 2's _xml_escape escapes the whole document
        as text content for SOAP embedding, so DIDL-Lite itself
        must be a single valid document."""
        didl = build_didl_lite(_track())
        root = ET.fromstring(didl)
        assert root.tag.endswith("DIDL-Lite")

    def test_namespaces_declared(self):
        """All three required namespaces (DIDL-Lite default, dc,
        upnp) must be declared on the root element. Bluesound and
        Linn both reject documents that omit any of them."""
        didl = build_didl_lite(_track())
        assert (
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            in didl
        )
        assert 'xmlns:dc="http://purl.org/dc/elements/1.1/"' in didl
        assert (
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
            in didl
        )

    def test_item_attributes(self):
        """The single <item> element must have id, parentID, and
        restricted attributes per spec."""
        didl = build_didl_lite(_track())
        assert 'id="0"' in didl
        assert 'parentID="0"' in didl
        assert 'restricted="1"' in didl

    def test_class_is_audio_track(self):
        """upnp:class signals the renderer how to treat the item.
        For music tracks it must be object.item.audioItem.musicTrack
        — Linn devices in particular check this and fall back to a
        generic audio-item renderer (lower quality on some) when
        it's missing or wrong."""
        didl = build_didl_lite(_track())
        assert (
            "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            in didl
        )


# ---------------------------------------------------------------------
# build_didl_lite — content fields
# ---------------------------------------------------------------------


class TestBuildDidlLiteContent:
    def test_title_in_dc_title(self):
        didl = build_didl_lite(_track(title="Spring summer"))
        assert "<dc:title>Spring summer</dc:title>" in didl

    def test_artist_appears_twice(self):
        """Tidal Connect devices read either dc:creator or
        upnp:artist depending on firmware. We emit both so neither
        camp ends up with a blank label. Cost is negligible (one
        extra element)."""
        didl = build_didl_lite(_track(artist="Smerz"))
        assert "<dc:creator>Smerz</dc:creator>" in didl
        assert "<upnp:artist>Smerz</upnp:artist>" in didl

    def test_album_in_upnp_album(self):
        didl = build_didl_lite(_track(album="Big city life"))
        assert "<upnp:album>Big city life</upnp:album>" in didl

    def test_cover_url_when_present(self):
        didl = build_didl_lite(
            _track(cover_url="http://covers/x.jpg")
        )
        assert (
            "<upnp:albumArtURI>http://covers/x.jpg</upnp:albumArtURI>"
            in didl
        )

    def test_cover_omitted_when_empty(self):
        """Empty albumArtURI element is what some devices reject —
        better to omit the element entirely if we don't have a
        cover URL."""
        didl = build_didl_lite(_track(cover_url=""))
        assert "albumArtURI" not in didl

    def test_track_uri_in_res(self):
        """The streamable URL goes in the <res> element's text
        content. The device fetches THIS URL when Insert + Play
        runs."""
        didl = build_didl_lite(
            _track(track_uri="http://stream.tidal/track.flac")
        )
        assert "http://stream.tidal/track.flac" in didl

    def test_protocol_info_format(self):
        """protocolInfo is four fields colon-separated:
        protocol:network:contentFormat:additionalInfo. * means any
        in network/additional. Standard form for an http-fetched
        FLAC file."""
        didl = build_didl_lite(_track(mime_type="audio/flac"))
        assert "protocolInfo=" in didl
        # Note: the colons inside the protocolInfo value may be
        # escaped to entities — match liberally.
        assert "http-get" in didl
        assert "audio/flac" in didl

    def test_duration_in_res(self):
        didl = build_didl_lite(_track(duration_s=240))
        assert 'duration="0:04:00.000"' in didl

    def test_alternate_mime_type(self):
        """Slice 4 may need to hand HLS / DASH manifests instead of
        direct files. The mime_type field plumbs through to
        protocolInfo so the device knows how to handle the
        resource."""
        didl = build_didl_lite(_track(mime_type="audio/mp4"))
        assert "audio/mp4" in didl


# ---------------------------------------------------------------------
# build_didl_lite — XML escaping
# ---------------------------------------------------------------------


class TestBuildDidlLiteEscaping:
    def test_ampersand_in_title_escaped(self):
        """A track title with `&` must be escaped or the DIDL-Lite
        won't parse on the device. Slice 2's SOAP layer
        additionally re-escapes the whole DIDL-Lite document for
        SOAP embedding, so we just need to make sure THIS layer
        produces well-formed XML."""
        didl = build_didl_lite(_track(title="Yin & Yang"))
        # The literal `&` must be escaped to `&amp;` here.
        assert "<dc:title>Yin &amp; Yang</dc:title>" in didl
        # And the document must still parse.
        ET.fromstring(didl)

    def test_quotes_in_title(self):
        didl = build_didl_lite(_track(title='"Reflections Laughing"'))
        assert "&quot;" in didl
        ET.fromstring(didl)

    def test_lt_gt_in_title(self):
        didl = build_didl_lite(_track(title="<bracketed>"))
        assert "&lt;bracketed&gt;" in didl
        ET.fromstring(didl)

    def test_ampersand_in_url(self):
        """Tidal stream URLs frequently contain `&` (query-string
        separators in signed tokens). They MUST escape into the
        <res> text content or the URL the device tries to fetch
        will be split at the `&`."""
        didl = build_didl_lite(
            _track(track_uri="http://x?token=abc&signature=123")
        )
        assert "&amp;signature=123" in didl
        ET.fromstring(didl)


# ---------------------------------------------------------------------
# build_didl_lite — round-trip parsing
# ---------------------------------------------------------------------


class TestBuildDidlLiteRoundTrip:
    def test_can_extract_fields_from_output(self):
        """A device parses our DIDL-Lite to find the URL + title.
        Make sure ours is structured such that a real XML parser
        can find them — element-by-element verification."""
        track = _track(
            title="Cry For Me",
            artist="The Weeknd",
            album="Hurry Up Tomorrow",
            duration_s=240,
            track_uri="http://stream/x.flac",
        )
        didl = build_didl_lite(track)
        root = ET.fromstring(didl)
        # Find the <item> child (DIDL-Lite default namespace).
        item = root.find("{*}item")
        assert item is not None
        title = item.find("{*}title")
        assert title is not None
        assert title.text == "Cry For Me"
        res = item.find("{*}res")
        assert res is not None
        assert res.text == "http://stream/x.flac"
        assert res.attrib["duration"] == "0:04:00.000"
