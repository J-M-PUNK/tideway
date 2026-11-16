"""Mixes are named the way Tidal's app names them.

Tidal now gives each daily mix a descriptive name in `description`
("Contemporary Indie Pop and Bedroom Dream Pop") and keeps the generic
"My Mix 1" in `title`. Tidal's app shows the description; Tideway
showed the title because tidalapi's parsers drop `description`.
"""
import tidalapi.mix as tidal_mix

import server


def test_display_name_prefers_the_description():
    assert server._mix_display_name("My Mix 1", "Contemporary Indie Pop") == "Contemporary Indie Pop"
    assert server._mix_display_name("My Daily Discovery", None) == "My Daily Discovery"
    assert server._mix_display_name("My New Arrivals", "  ") == "My New Arrivals"


def test_parsers_keep_the_description():
    class Stub:
        def parse(self, json_obj):
            self.title = json_obj["title"]
            return self

    server._keep_mix_description(Stub)
    m = Stub().parse({"title": "My Mix 5", "description": "Japanese City Pop and Fusion"})
    assert m.description == "Japanese City Pop and Fusion"
    assert Stub().parse({"title": "My New Arrivals"}).description is None
    # Both of tidalapi's mix classes are wrapped.
    assert tidal_mix.Mix.parse.__name__ == tidal_mix.MixV2.parse.__name__ == "parse"


def test_raw_entry_uses_the_description():
    item = server._raw_entry_to_item({"type": "MIX", "data": {
        "id": "m1", "titleTextInfo": {"text": "My Mix 1"},
        "description": "Contemporary Indie Pop and Bedroom Dream Pop",
    }})
    assert item["name"] == "Contemporary Indie Pop and Bedroom Dream Pop"
