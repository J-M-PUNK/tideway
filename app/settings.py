import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from app.paths import user_data_dir

SETTINGS_FILE = user_data_dir() / "settings.json"


def _default_videos_dir() -> str:
    """Reasonable per-OS default for music-video downloads.

    macOS / Linux → ~/Movies/Tidal (macOS convention; Linux also
    uses ~/Movies in recent XDG setups and falls back to ~/Videos
    via the user-dirs config if they're redirected it). Windows →
    %USERPROFILE%\\Videos\\Tidal.

    Kept separate from the audio `output_dir` so video files don't
    intermix with album folders and iTunes-style music libraries that
    expect an audio-only tree.
    """
    home = Path.home()
    videos = home / "Videos"
    movies = home / "Movies"
    if sys.platform == "darwin":
        return str(movies / "Tidal")
    if sys.platform.startswith("win"):
        return str(videos / "Tidal")
    # Linux — pick whichever exists, prefer Videos which is XDG standard.
    if videos.is_dir() or not movies.is_dir():
        return str(videos / "Tidal")
    return str(movies / "Tidal")


@dataclass
class Settings:
    output_dir: str = str(Path.home() / "Music" / "Tidal")
    videos_dir: str = field(default_factory=_default_videos_dir)
    filename_template: str = "{artist} - {title}"
    create_album_folders: bool = True
    skip_existing: bool = True
    # How many downloads may run in parallel. Gated by the Downloader's
    # semaphore so changing this doesn't require a process restart.
    # Default 1 — serial download is the safest baseline against
    # Tidal's per-account rate-limit. Users with stable accounts can
    # raise this in Settings; the slider goes up to 10.
    concurrent_downloads: int = 1
    # Per-track download rate cap in MB/s. 0 = unlimited. Default 20
    # MB/s — fast enough that a Max-quality 4-minute track finishes in
    # ~2 seconds on a normal connection, slow enough that the CDN sees
    # a steady streaming-shaped fetch instead of a "scrape as fast as
    # possible" pattern that would attract anti-abuse attention.
    download_rate_limit_mbps: int = 20
    # When True, the UI hides everything that needs a live Tidal session
    # (search, editorial, favorites, streaming fallback) and the server
    # stops requiring auth on the handful of endpoints that only touch
    # local state. Lets users play / manage files they already downloaded
    # without signing in.
    offline_mode: bool = False
    # Opt-in desktop notifications when a download burst finishes.
    # Fires both the browser-side toast and a native OS notification
    # (via app/notify.py) — the OS path is what reaches the user
    # when the window is minimized / in another Space. Off by default
    # because browsers require an explicit permission prompt the first
    # time, and ambushing every new user with one is rude.
    notify_on_complete: bool = False
    # Opt-in OS-level notifications when the current track changes.
    # Mirrors what Tidal / Spotify desktops do. Off by default — the
    # window is the primary "what's playing" surface when it's open,
    # and an unsolicited bezel every 3 minutes would be noise. Users
    # who leave the window minimized will want it on.
    notify_on_track_change: bool = False
    # --- Audio engine settings ------------------------------------------
    # Master enable for the equalizer. When False, the filter chain
    # is bypassed regardless of `eq_bands` — we still keep the
    # stored bands/preamp so flipping this back on restores the
    # user's curve. Defaults off so new users aren't unknowingly
    # listening through a flat-but-present filter chain.
    eq_enabled: bool = False
    # 10-band equalizer amplitudes in dB, -20..+20. Empty list =
    # flat / off. Replaces any prior EQ when re-applied. When
    # `eq_preamp` is None no preamp gain is applied.
    eq_bands: list[float] = field(default_factory=list)
    eq_preamp: Optional[float] = None
    # AutoEQ headphone-profile mode (see
    # docs/autoeq-headphone-profiles-scope.md).
    #   "off"     — EQ stage bypassed regardless of eq_enabled.
    #   "manual"  — uses eq_bands / eq_preamp (the existing path).
    #   "profile" — uses eq_active_profile_id from the bundled
    #               AutoEQ catalog.
    # `eq_enabled` is the master gate for backward compat; this
    # mode field selects which curve runs when enabled.
    eq_mode: str = "manual"
    # Identifier of the currently-loaded AutoEQ profile, formatted
    # as "<source>/<headphone-dir>" (e.g. "oratory1990/Sennheiser
    # HD 600"). Empty string = no profile selected. Used only when
    # eq_mode == "profile"; preserved across mode switches so
    # toggling profile/manual/profile keeps the user's pick.
    eq_active_profile_id: str = ""
    # sounddevice output-device index (stringified, matches what
    # /api/player/output-devices returns). Empty string means "use
    # the system default". Persisted so USB DAC / Bluetooth
    # choices survive relaunch.
    audio_output_device: str = ""
    # Exclusive Mode — bypass the OS mixer and push PCM straight at
    # the device at the track's native rate / bit depth. On macOS
    # this sets CoreAudio's change_device_parameters +
    # fail_if_conversion_required so the device is reconfigured for
    # bit-perfect playback and the stream fails loudly rather than
    # silently resampling. On Windows it opens WASAPI in exclusive
    # mode. No effect on Linux (ALSA routes straight to hardware
    # already once PulseAudio is out of the way). Some devices
    # (certain USB DACs at certain rates) will refuse — stream open
    # raises and the player surfaces the error.
    exclusive_mode: bool = False
    # Force Volume — pin the software volume at 100 % and rely on the
    # user's DAC, speakers, or OS volume to attenuate. Pairs naturally
    # with Exclusive Mode: once you're pushing bit-perfect samples to
    # an external converter, any software volume scaling re-introduces
    # bit-depth loss. Implemented by clamping set_volume() to 100 and
    # hiding the slider in the UI.
    force_volume: bool = False
    # When the user's queue runs out — last track on an album,
    # playlist, mix, single-track play, anything — take over with an
    # Artist Radio mix seeded from the last track's primary artist.
    # On by default to match Spotify / Apple Music's "autoplay" /
    # "Continuous Play" behavior. When off the player falls back to
    # the per-source default: stop the stream and clear the now-
    # playing bar (or, for albums specifically, prime track 0 paused
    # so one tap of Play repeats the album).
    #
    # Renamed from `continue_with_artist_radio_after_album`. Older
    # settings.json files that still carry the old key get the new
    # default (load_settings filters unknown keys), which means a
    # user who had the old toggle off ends up with the new toggle on
    # — they can flip it off again if they didn't want it.
    continue_playing_after_queue_ends: bool = True
    # Don't restore the main window on launch; go straight to the
    # tray. Useful for "Launch on login" users who want Tideway
    # running without grabbing focus each reboot.
    start_minimized: bool = False
    # Spotify Developer app client_id, used by the Spotify → Tidal
    # playlist importer. Users register their own app at
    # developer.spotify.com and paste the id here. PKCE OAuth so we
    # don't need a secret. Empty string = import feature hidden in UI.
    spotify_client_id: str = ""
    # When Tidal returns both an explicit and a clean edit of the same
    # album / track, the UI would otherwise show both side by side
    # (e.g. "Rodeo" and "Rodeo" by Travis Scott). Match Tidal's own
    # client behaviour and collapse the pair.
    #  - "explicit": keep the explicit edit when both exist (default).
    #  - "clean":    keep the clean edit when both exist.
    #  - "both":     show both, as the raw API returned them.
    explicit_content_preference: str = "explicit"


def _migrate_default_paths(s: Settings) -> bool:
    """Update persisted settings whose download folders still point at
    the old "Tideway" default subfolder. The app-rename default was
    "Tidal" to match Tidal's own desktop convention, but existing
    installs already had the old value saved. Only touches paths that
    match a previously-shipped default — custom paths stay untouched.
    Returns True if anything changed so the caller knows to re-save."""
    changed = False
    home = Path.home()

    legacy_music = str(home / "Music" / "Tideway")
    new_music = str(home / "Music" / "Tidal")
    if s.output_dir == legacy_music:
        s.output_dir = new_music
        changed = True

    legacy_videos = {
        str(home / "Movies" / "Tideway"),
        str(home / "Videos" / "Tideway"),
    }
    if s.videos_dir in legacy_videos:
        s.videos_dir = _default_videos_dir()
        changed = True

    return changed


def load_settings() -> Settings:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            valid = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
            settings = Settings(**valid)
            if _migrate_default_paths(settings):
                try:
                    save_settings(settings)
                except Exception:
                    # Migration is cosmetic — if we can't re-save
                    # right now, let the next genuine save pick up
                    # the updated values.
                    pass
            return settings
        except Exception:
            pass
    return Settings()


def save_settings(settings: Settings):
    """Atomic write so a crash mid-save can't corrupt settings.json.

    `open("w")` truncates first, so a SIGKILL / power loss between truncate
    and json.dump finishing leaves an empty file. `load_settings` silently
    falls back to defaults on JSON decode failure — meaning the user's
    entire config disappears. Write to a sibling tmp file then os.replace
    for an atomic rename.
    """
    target = SETTINGS_FILE
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".settings.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(asdict(settings), f, indent=2)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
