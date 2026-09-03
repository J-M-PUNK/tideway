"""Hi-res downconvert is on by default (fresh installs only).

A downloaded file that plays on the device it was downloaded for is a
better starting point than one that is bit-exact and silent there —
old iPods running Rockbox and most legacy DAPs cannot decode 24-bit or
high-sample-rate FLAC in real time. Wanting the hi-res master is the
more deliberate choice of the two, so it is the one that takes a
click.

The scope matters and is easy to get wrong: `save_settings` writes
every field, so any existing settings.json already pins this value.
Changing the default cannot and must not move it for someone who has
run the app before — including someone who deliberately turned it off.
"""
from __future__ import annotations

import json

from app.settings import Settings, load_settings, save_settings


def test_default_is_on():
    assert Settings().downconvert_hires_downloads is True


def test_an_existing_off_choice_survives(tmp_path, monkeypatch):
    """The important half. Someone who turned this off — or ran any
    earlier version, which wrote False — keeps their value."""
    import app.settings as settings_mod

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"downconvert_hires_downloads": False}), encoding="utf-8"
    )
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", path)

    assert load_settings().downconvert_hires_downloads is False


def test_an_existing_on_choice_survives(tmp_path, monkeypatch):
    import app.settings as settings_mod

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"downconvert_hires_downloads": True}), encoding="utf-8"
    )
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", path)

    assert load_settings().downconvert_hires_downloads is True


def test_a_settings_file_predating_the_field_gets_the_new_default(
    tmp_path, monkeypatch
):
    """An install old enough to have no key for this takes the default,
    which is the only case the change is meant to reach."""
    import app.settings as settings_mod

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"download_lyrics": True}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", path)

    assert load_settings().downconvert_hires_downloads is True


def test_the_value_round_trips_through_a_save(tmp_path, monkeypatch):
    """Guards the CLAUDE.md foot-gun from the other direction: a field
    that saves but doesn't reload would look like the setting resetting
    itself."""
    import app.settings as settings_mod

    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", path)

    s = Settings()
    s.downconvert_hires_downloads = False
    save_settings(s)

    assert load_settings().downconvert_hires_downloads is False
    assert "downconvert_hires_downloads" in json.loads(
        path.read_text(encoding="utf-8")
    )
