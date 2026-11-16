"""Hi-res downconvert is off by default (fresh installs only).

Someone who picks Max quality expects the Max file. From 1.33.0 to
1.41.0 this was on by default, so MAX downloads quietly came out as
16-bit / 44.1 kHz and a user reported hi-res downloads as broken
(#490). Converting for an old player that can't decode hi-res is the
deliberate choice, so it is the one that takes a click.

The scope matters and is easy to get wrong: `save_settings` writes
every field, so any existing settings.json already pins this value.
Changing the default cannot and must not move it for someone who has
run the app before — including someone who deliberately turned it off.
"""
from __future__ import annotations

import json

from app.settings import Settings, load_settings, save_settings


def test_default_is_off():
    assert Settings().downconvert_hires_downloads is False


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
    """An install with no key for this takes the default, which is the
    only case the change is meant to reach."""
    import app.settings as settings_mod

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"download_lyrics": True}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", path)

    assert load_settings().downconvert_hires_downloads is False


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
