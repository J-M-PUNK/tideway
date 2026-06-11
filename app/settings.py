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
    # When `create_album_folders` is on, prefix the folder name with
    # the album artist: "<Artist> - <Album>/" instead of just
    # "<Album>/". Matches the "Artist - Album" convention Plex / Roon /
    # foobar use for sideloaded libraries. Off by default so existing
    # users' libraries don't fragment on upgrade — flipping it on only
    # affects subsequent downloads. Album artist is preferred over the
    # per-track artist so compilation entries don't pick up a featured
    # name from track 1; falls back to the per-track artist when no
    # album artist is set.
    album_folder_includes_artist: bool = False
    # When downloading a playlist, group its tracks under a folder
    # named after the playlist (parallel to create_album_folders for
    # albums). The {playlist_num} template token then numbers them in
    # playlist order rather than by their album track number.
    create_playlist_folders: bool = True
    # Downconvert hi-res (24-bit and/or >48 kHz) downloads to
    # 16-bit / 44.1 kHz FLAC so they play on hardware that can't
    # decode hi-res in real time (old iPods running Rockbox, most
    # legacy DAPs). Off by default — downloads stay bit-exact unless
    # you opt in. CD-quality and lossy sources are never touched;
    # the resample is high quality and the bit-depth reduction uses
    # TPDF dither.
    downconvert_hires_downloads: bool = False
    skip_existing: bool = True
    # How many downloads may run in parallel. Gated by the Downloader's
    # semaphore so changing this doesn't require a process restart.
    # Default 1 — serial download is the safest baseline against
    # Tidal's per-account rate-limit. Users with stable accounts can
    # raise this in Settings; the slider goes up to 10.
    concurrent_downloads: int = 1
    # Per-track download rate cap in MB/s. 0 = unlimited. Default 10
    # MB/s — fast enough that a Max-quality 4-minute track finishes
    # in a few seconds on a normal connection, but well clear of the
    # "saturate fiber, obviously not listening" pattern that the
    # previous 20 MB/s default could produce on fast connections.
    # Existing users keep their previously-saved value; this only
    # affects fresh installs and explicit "reset to defaults".
    download_rate_limit_mbps: int = 10
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
    # EQ mode. Defaults off so a fresh install plays bit-perfect
    # audio — the user opts into the EQ stage explicitly via the
    # Settings picker rather than discovering one is silently
    # applying a flat curve.
    #   "off"     — EQ stage bypassed regardless of eq_enabled.
    #   "manual"  — uses eq_bands / eq_preamp (the existing path).
    #   "profile" — uses eq_active_profile_id from a user-imported
    #               AutoEQ profile.
    # `eq_enabled` is the master gate for backward compat; this
    # mode field selects which curve runs when enabled.
    eq_mode: str = "off"
    # Identifier of the currently-loaded AutoEQ profile, formatted
    # as "<source>/<headphone-dir>" (e.g. "oratory1990/Sennheiser
    # HD 600"). Empty string = no profile selected. Used only when
    # eq_mode == "profile"; preserved across mode switches so
    # toggling profile/manual/profile keeps the user's pick.
    eq_active_profile_id: str = ""
    # A/B bypass — momentarily disable the active EQ stage (manual
    # OR profile) without losing the configuration. Phase 4 of the
    # scope doc adds a player-UI button + keyboard shortcut for
    # this so the user can compare correction-on vs correction-off
    # without unsetting their pick. Persisted so the user can
    # leave it bypassed and have that survive a relaunch.
    eq_bypass: bool = False
    # Per-device AutoEQ profile mapping (Phase 3 of the scope doc).
    # Key = device fingerprint as `sounddevice` reports it; value =
    # profile_id, or None to explicitly skip mapping for that device
    # (e.g. an HDMI output to a TV where EQ doesn't make sense).
    # When the active output device changes, the resolver looks up
    # this map and applies the matching profile.
    eq_device_mappings: dict[str, Optional[str]] = field(default_factory=dict)
    # Behaviour when the active device has no mapping entry:
    #   "bypass" — clear the EQ (safest; user opts in by mapping).
    #   "use_last_profile" — keep the last-applied profile active
    #     even on unmapped devices. Convenient for users who only
    #     ever listen on one pair.
    eq_fallback_when_unmapped: str = "bypass"
    # Phase 5 user-tilt: shelves stacked after the profile bands +
    # a master preamp offset. User-global (not per-device) — these
    # are taste preferences that travel with the listener, not
    # headphone-specific corrections. Range -12..+12 dB enforced
    # at the API layer; raw floats persisted here.
    eq_tilt_preamp_offset_db: float = 0.0
    eq_tilt_bass_db: float = 0.0
    eq_tilt_treble_db: float = 0.0
    # Bauer crossfeed strength (0-100 percent). 0 = bypass, leaving
    # the audio path bit-perfect. Non-zero engages a 700 Hz low-pass
    # bleed between channels — preserves high-frequency stereo image
    # while pulling bass toward the centre, the standard headphone-
    # listening fix for hard-panned mixes. Off by default; user opts
    # in via the Settings → Playback slider.
    crossfeed_amount: int = 0
    # ReplayGain loudness leveling. Off by default to preserve
    # bit-perfect output for users who haven't asked for leveling.
    #   "off"   — bypass; audio plays at the source's native level.
    #   "track" — apply per-track gain. Best for shuffle / mixed
    #             playback where each track's loudness is
    #             independent.
    #   "album" — apply album-wide gain. Best for listening to whole
    #             albums; preserves the artist's intended loudness
    #             relationships between tracks within the album.
    replaygain_mode: str = "off"
    # User offset in dB on top of the ReplayGain value. Useful when
    # the EBU R128 reference is too quiet for a particular setup
    # (low-output DAC + high-impedance headphones). Range -10..+10
    # enforced at the API layer; raw float persisted here.
    replaygain_preamp_db: float = 0.0
    # When on, the gain is clamped so the peak * gain ≤ 1.0 to
    # prevent clipping. Off lets the user push past that for
    # quieter masters that won't actually clip with the chosen
    # preamp, at the cost of risk if the math is off.
    replaygain_prevent_clipping: bool = True
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
    # Last software volume (0..100), persisted so a restart doesn't
    # blast the user at 100 %. Restored onto the player at startup;
    # the /api/player/volume endpoint writes it back on every change.
    volume: int = 100
    # Scroll-wheel volume step in percent (1..25). One wheel tick over
    # the player bar's volume control changes the volume by this much;
    # holding Shift always steps by 1 % for fine adjustment. 5 matches
    # what most desktop players use per tick.
    volume_scroll_step_pct: int = 5
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
    # Pause Tideway when another device on the same Tidal account
    # starts playing. Matches Spotify's "playback transferred to
    # other device" behaviour and the official Tidal client's
    # cross-device handoff. Default on; users who explicitly want
    # multi-device-fighting playback can flip it off. Wired to
    # `app/tidal_realtime.py`'s on_other_device_started callback,
    # which the desktop launcher binds to PCMPlayer.pause(). The
    # listener itself is opt-in at the protocol level too: if the
    # realtime bus protocol hasn't been captured yet (Phase 1 of
    # the cross-device-pause feature), the listener stays disabled
    # regardless of this setting.
    pause_on_other_device: bool = True
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
    # Desktop window geometry, persisted on close and restored on the
    # next launch. -1 means "not set yet" so the first run uses the
    # platform default size + centred position instead of forcing a
    # stored 0,0. Negative x/y are otherwise legitimate on
    # multi-monitor setups, so only the sentinel is special-cased.
    window_x: int = -1
    window_y: int = -1
    window_width: int = -1
    window_height: int = -1


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
