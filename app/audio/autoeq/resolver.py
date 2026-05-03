"""Per-device AutoEQ profile resolver — Phase 3 of the scope doc.

When the active output device changes, this module decides which
profile (if any) to apply, given:

- The device's fingerprint (its `sounddevice` name).
- The user's `eq_device_mappings` settings.
- The user's `eq_fallback_when_unmapped` choice.
- The current `eq_active_profile_id` (used by the
  `use_last_profile` fallback).

The resolver is intentionally small: just decision logic,
returning a `ResolverDecision`. Callers (server.py) apply the
decision to the player and persist any state changes — keeps the
package free of circular dependencies on server module state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .index import AutoEqIndex
from .profiles import AutoEqProfile


@dataclass
class ResolverDecision:
    """What the resolver thinks should happen on a device change.

    `profile` is the profile to apply (None means "bypass — clear
    the EQ"). `active_profile_id` is the new value to persist into
    settings; may differ from the input when the resolver picked
    a fallback or cleared the mapping. `reason` is a short label
    for logging / SSE telemetry."""

    profile: Optional[AutoEqProfile]
    active_profile_id: str
    reason: str


def resolve_for_device(
    fingerprint: str,
    *,
    device_mappings: dict[str, Optional[str]],
    fallback: str,
    current_active_profile_id: str,
    index: AutoEqIndex,
) -> ResolverDecision:
    """Pick the right profile for the given device.

    Decision order:

    1. If `device_mappings[fingerprint]` is a known profile_id,
       apply that profile.
    2. If `device_mappings[fingerprint]` is explicitly `None`
       (user mapped this device to "no EQ"), bypass.
    3. If the device isn't in the map, apply `fallback`:
       - `"bypass"`: clear the EQ.
       - `"use_last_profile"`: keep `current_active_profile_id`.

    The fingerprint is normalised to its trimmed form so trailing
    whitespace from `sounddevice` (yes, it happens) doesn't break
    lookups."""
    fp = (fingerprint or "").strip()

    # Case 1+2: explicit mapping (including explicit None).
    if fp in device_mappings:
        mapped_id = device_mappings[fp]
        if mapped_id is None:
            return ResolverDecision(
                profile=None,
                active_profile_id="",
                reason="device explicitly mapped to bypass",
            )
        profile = index.get(mapped_id)
        if profile is None:
            # Mapped to a profile that no longer exists (catalog
            # update removed it, file rename, etc.). Treat as
            # unmapped — fall through to the fallback branch.
            mapped_missing = True
        else:
            return ResolverDecision(
                profile=profile,
                active_profile_id=mapped_id,
                reason=f"device mapped to {mapped_id}",
            )
    else:
        mapped_missing = False

    # Case 3: no mapping — fall back per user preference.
    if fallback == "use_last_profile" and current_active_profile_id:
        last = index.get(current_active_profile_id)
        if last is not None:
            reason = (
                "no mapping; keeping last profile"
                if not mapped_missing
                else f"mapped profile {device_mappings[fp]} missing; "
                f"keeping last profile"
            )
            return ResolverDecision(
                profile=last,
                active_profile_id=current_active_profile_id,
                reason=reason,
            )

    # Default fallback: bypass.
    base_reason = (
        "no mapping" if not mapped_missing else "mapped profile missing"
    )
    return ResolverDecision(
        profile=None,
        active_profile_id="",
        reason=f"{base_reason}; bypassing",
    )
