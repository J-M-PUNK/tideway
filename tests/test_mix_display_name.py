"""Mixes are named the way Tidal's app names them.

Tidal now gives each daily mix a descriptive name in `description`
("Contemporary Indie Pop and Bedroom Dream Pop") and keeps the generic
"My Mix 1" in `title`. Tidal's app shows the description; Tideway
showed the title because tidalapi's parsers drop `description`.
"""
import copy

import tidalapi.mix as tidal_mix

import server


def test_display_name_prefers_the_description():
    assert server._mix_display_name("My Mix 1", "Contemporary Indie Pop") == "Contemporary Indie Pop"
    assert server._mix_display_name("My Daily Discovery", None) == "My Daily Discovery"
    assert server._mix_display_name("My New Arrivals", "  ") == "My New Arrivals"


def test_parsers_keep_the_description():
    class Stub:
        def parse(self, json_obj):
            # Like tidalapi's Mix/MixV2.parse: fill in self, return a copy.
            self.title = json_obj["title"]
            return copy.copy(self)

    server._keep_mix_description(Stub)
    # v1 mix endpoints send a plain string.
    m = Stub().parse({"title": "My Mix 5", "description": "Japanese City Pop and Fusion"})
    assert m.description == "Japanese City Pop and Fusion"
    # The v2 home feed sends an object.
    m = Stub().parse({"title": "My Mix 1", "description": {
        "text": "Contemporary Indie Pop and Bedroom Dream Pop", "color": None}})
    assert m.description == "Contemporary Indie Pop and Bedroom Dream Pop"
    assert Stub().parse({"title": "My New Arrivals"}).description is None
    # Both of tidalapi's mix classes are wrapped.
    assert tidal_mix.Mix.parse.__name__ == tidal_mix.MixV2.parse.__name__ == "parse"


def test_raw_entry_uses_the_description():
    item = server._raw_entry_to_item({"type": "MIX", "data": {
        "id": "m1", "titleTextInfo": {"text": "My Mix 1"},
        "description": {"text": "Contemporary Indie Pop and Bedroom Dream Pop", "color": None},
    }})
    assert item["name"] == "Contemporary Indie Pop and Bedroom Dream Pop"


def _images():
    return [
        {"size": size, "url": f"https://images.tidal.com/{size}", "width": w, "height": w}
        for size, w in (("SMALL", 320), ("MEDIUM", 640), ("LARGE", 1500))
    ]


def test_real_home_feed_mix_entry():
    # Shape of a "My Mix" entry from the v2 home feed, trimmed.
    data = {
        "id": "0024d84b3b8f526bae2328bc855f80", "type": "DAILY_MIX",
        "titleTextInfo": {"text": "My Mix 1", "color": "#FFFFFF"},
        "subtitleTextInfo": {"text": "Tiffany Day, beabadoobee and more", "color": None},
        "shortSubtitleTextInfo": {"text": "Tiffany Day, beabadoobee", "color": None},
        "description": {"text": "Contemporary Indie Pop and Bedroom Dream Pop", "color": None},
        "mixImages": _images(), "detailMixImages": _images(),
        "artists": [], "mixNumber": 1, "updated": 1794815938491,
    }
    mix = server.tidal.session.parse_v2_mix(data)
    assert server._serialize_page_item(mix)["name"] == "Contemporary Indie Pop and Bedroom Dream Pop"
