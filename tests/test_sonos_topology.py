"""Sonos zone-group topology parsing (app/audio/sonos_topology)."""
from __future__ import annotations

import pytest

from app.audio.sonos_topology import (
    group_of,
    parse_zone_group_state,
    udn_to_uuid,
)
from tests.sonos_fixtures import zone_group_state


def _household():
    return zone_group_state([
        ("RINCON_LR", [
            ("RINCON_LR", "192.168.1.60", "Living Room", False),
            ("RINCON_LR_R", "192.168.1.62", "Living Room", True),  # pair
            ("RINCON_K", "192.168.1.61", "Kitchen", False),
        ]),
        ("RINCON_O", [
            ("RINCON_O", "192.168.1.63", "Office", False),
        ]),
    ])


def test_parses_groups_and_members():
    groups = parse_zone_group_state(_household())
    assert [g.coordinator_uuid for g in groups] == ["RINCON_LR", "RINCON_O"]
    lr = groups[0]
    assert [m.uuid for m in lr.members] == ["RINCON_LR", "RINCON_LR_R", "RINCON_K"]
    assert lr.members[1].invisible is True
    assert lr.coordinator.location == (
        "http://192.168.1.60:1400/xml/device_description.xml"
    )


def test_older_firmware_root_is_zonegroups():
    doc = _household().replace("<ZoneGroupState>", "").replace(
        "</ZoneGroupState>", ""
    )
    assert len(parse_zone_group_state(doc)) == 2


def test_display_name_coordinator_first_hidden_and_pair_deduped():
    lr = parse_zone_group_state(_household())[0]
    assert lr.display_name() == "Living Room + Kitchen"


def test_group_of_and_udn_to_uuid():
    groups = parse_zone_group_state(_household())
    assert group_of(groups, udn_to_uuid("uuid:RINCON_K")).coordinator_uuid == (
        "RINCON_LR"
    )
    assert group_of(groups, "RINCON_NOPE") is None
    assert udn_to_uuid("RINCON_X") == "RINCON_X"


def test_invalid_xml_raises():
    with pytest.raises(ValueError):
        parse_zone_group_state("")
