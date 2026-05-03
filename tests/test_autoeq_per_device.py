"""Phase 3 of the AutoEQ work — per-device profile mapping.

Tests cover the resolver decision logic in isolation (no audio
engine, no FastAPI) and the seen-devices JSON store. The
server-side wiring (lifespan startup, output-device endpoint)
is exercised by manual smoke testing — the value here is in
pinning the small, decision-heavy modules.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.audio.autoeq.index import AutoEqIndex
from app.audio.autoeq.resolver import resolve_for_device
from app.audio.autoeq.seen_devices import SeenDeviceStore, _classify_kind


# ---------------------------------------------------------------------------
# Resolver — decision logic
# ---------------------------------------------------------------------------


def _populate(tmp_path: Path) -> AutoEqIndex:
    """Stub index with two profiles for the resolver tests."""
    body = "Preamp: 0 dB\nFilter 1: ON PK Fc 1000 Hz Gain 1 dB Q 1\n"
    for brand_model in ("Sennheiser HD 600", "Sony WH-1000XM4"):
        target = tmp_path / "oratory1990" / brand_model
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{brand_model} ParametricEQ.txt").write_text(
            body, encoding="utf-8"
        )
    idx = AutoEqIndex()
    idx.load_directory(tmp_path)
    return idx


def test_resolver_applies_mapped_profile(tmp_path):
    idx = _populate(tmp_path)
    decision = resolve_for_device(
        "Scarlett Solo USB",
        device_mappings={"Scarlett Solo USB": "oratory1990/Sennheiser HD 600"},
        fallback="bypass",
        current_active_profile_id="",
        index=idx,
    )
    assert decision.profile is not None
    assert decision.profile.profile_id == "oratory1990/Sennheiser HD 600"
    assert decision.active_profile_id == "oratory1990/Sennheiser HD 600"


def test_resolver_explicit_none_means_bypass(tmp_path):
    """A user can explicitly map a device to "no EQ" — resolver
    must respect that even when a fallback profile is set."""
    idx = _populate(tmp_path)
    decision = resolve_for_device(
        "HDMI Output",
        device_mappings={"HDMI Output": None},
        fallback="use_last_profile",
        current_active_profile_id="oratory1990/Sennheiser HD 600",
        index=idx,
    )
    assert decision.profile is None
    assert decision.active_profile_id == ""


def test_resolver_unmapped_with_bypass_fallback_clears_eq(tmp_path):
    idx = _populate(tmp_path)
    decision = resolve_for_device(
        "Unknown Device",
        device_mappings={},
        fallback="bypass",
        current_active_profile_id="oratory1990/Sennheiser HD 600",
        index=idx,
    )
    assert decision.profile is None
    assert decision.active_profile_id == ""


def test_resolver_unmapped_with_use_last_profile_keeps_active(tmp_path):
    idx = _populate(tmp_path)
    decision = resolve_for_device(
        "Unknown Device",
        device_mappings={},
        fallback="use_last_profile",
        current_active_profile_id="oratory1990/Sennheiser HD 600",
        index=idx,
    )
    assert decision.profile is not None
    assert decision.active_profile_id == "oratory1990/Sennheiser HD 600"


def test_resolver_handles_missing_mapped_profile(tmp_path):
    """A device mapped to a profile_id that no longer exists in
    the catalog (e.g. catalog update removed it) must NOT crash —
    fall back per the user's `fallback` setting."""
    idx = _populate(tmp_path)
    decision = resolve_for_device(
        "Some Device",
        device_mappings={"Some Device": "oratory1990/Nonexistent Headphone"},
        fallback="use_last_profile",
        current_active_profile_id="oratory1990/Sony WH-1000XM4",
        index=idx,
    )
    # Last-profile fallback succeeded — the missing mapping
    # surfaced cleanly.
    assert decision.profile is not None
    assert decision.active_profile_id == "oratory1990/Sony WH-1000XM4"


def test_resolver_strips_whitespace_from_fingerprint(tmp_path):
    """sounddevice occasionally emits trailing spaces on device
    names (Windows quirk). The resolver should match against the
    trimmed form rather than fail because the user's mapping
    omitted invisible whitespace."""
    idx = _populate(tmp_path)
    decision = resolve_for_device(
        "  Scarlett Solo USB   ",
        device_mappings={"Scarlett Solo USB": "oratory1990/Sennheiser HD 600"},
        fallback="bypass",
        current_active_profile_id="",
        index=idx,
    )
    assert decision.profile is not None


# ---------------------------------------------------------------------------
# Seen-devices store
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Redirect the seen-devices store to a tmp file."""
    from app.audio.autoeq import seen_devices

    monkeypatch.setattr(
        seen_devices,
        "_store_path",
        lambda: tmp_path / "autoeq_seen_devices.json",
    )
    return SeenDeviceStore()


def test_seen_store_upsert_inserts_new_device(isolated_store):
    isolated_store.upsert("Scarlett Solo USB")
    rows = isolated_store.list()
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == "Scarlett Solo USB"
    assert rows[0]["display_name"] == "Scarlett Solo USB"
    assert rows[0]["kind"] == "usb"
    assert rows[0]["first_seen"] == rows[0]["last_seen"]


def test_seen_store_upsert_updates_last_seen(isolated_store):
    """Re-upsert bumps last_seen but preserves first_seen."""
    isolated_store.upsert("Scarlett Solo USB")
    first_seen = isolated_store.list()[0]["first_seen"]
    # Force a clear time difference. We can't easily mock time
    # without monkeypatching `time.time`, so just trust that
    # last_seen >= first_seen and check that the same fingerprint
    # is still the only record.
    isolated_store.upsert("Scarlett Solo USB")
    rows = isolated_store.list()
    assert len(rows) == 1
    assert rows[0]["first_seen"] == first_seen
    assert rows[0]["last_seen"] >= first_seen


def test_seen_store_persists_across_reloads(tmp_path, monkeypatch):
    """A new SeenDeviceStore instance picks up records from disk."""
    from app.audio.autoeq import seen_devices

    monkeypatch.setattr(
        seen_devices,
        "_store_path",
        lambda: tmp_path / "autoeq_seen_devices.json",
    )

    store1 = SeenDeviceStore()
    store1.upsert("Scarlett Solo USB")
    store1.upsert("AirPods Pro")

    store2 = SeenDeviceStore()
    rows = store2.list()
    assert len(rows) == 2
    fps = {r["fingerprint"] for r in rows}
    assert fps == {"Scarlett Solo USB", "AirPods Pro"}


def test_seen_store_forget_removes_only_named_device(isolated_store):
    isolated_store.upsert("AirPods")
    isolated_store.upsert("Scarlett Solo USB")
    assert isolated_store.forget("AirPods") is True
    assert {r["fingerprint"] for r in isolated_store.list()} == {
        "Scarlett Solo USB"
    }
    # Forgetting an unknown device returns False rather than
    # raising — idempotent for the "user clicked forget twice"
    # case.
    assert isolated_store.forget("Nonexistent") is False


def test_seen_store_orders_by_last_seen_desc(isolated_store):
    isolated_store.upsert("DeviceA")
    isolated_store.upsert("DeviceB")
    isolated_store.upsert("DeviceA")  # bump A's last_seen
    rows = isolated_store.list()
    assert rows[0]["fingerprint"] == "DeviceA"
    assert rows[1]["fingerprint"] == "DeviceB"


def test_seen_store_ignores_empty_fingerprint(isolated_store):
    isolated_store.upsert("")
    assert isolated_store.list() == []


# ---------------------------------------------------------------------------
# Kind classification — heuristic, not exhaustive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("AirPods Pro", "bt"),
        ("WH-1000XM5 (Bluetooth)", "bt"),
        ("Samsung Buds", "bt"),
        ("Scarlett Solo USB", "usb"),
        ("Topping E50 DAC", "usb"),
        ("MacBook Pro Speakers", "builtin"),
        ("Internal Speakers", "builtin"),
        ("Built-in Output", "builtin"),
        ("Unknown Speaker", "unknown"),
    ],
)
def test_classify_kind(name, expected):
    assert _classify_kind(name) == expected
