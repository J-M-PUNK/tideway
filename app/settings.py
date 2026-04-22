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
    concurrent_downloads: int = 3
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
    # --- Audio engine (libvlc) settings ---------------------------------
    # Master enable for the equalizer. When False, libvlc runs with no
    # filtering regardless of `eq_bands` — we still keep the stored
    # bands/preamp so flipping this back on restores the user's curve.
    # Defaults off so new users aren't unknowingly listening through
    # a flat-but-present filter chain.
    eq_enabled: bool = False
    # 10-band equalizer amplitudes in dB, -20..+20. Empty list = flat /
    # off. Replaces any prior EQ when re-applied. When `eq_preamp` is
    # None the equalizer's preamp stays at libvlc's default.
    eq_bands: list[float] = field(default_factory=list)
    eq_preamp: Optional[float] = None
    # Libvlc output-device id (from `audio_output_device_enum`). Empty
    # string means "use the system default". Persisted so USB DAC /
    # Bluetooth choices survive relaunch.
    audio_output_device: str = ""
    # Spotify Developer app client_id, used by the Spotify → Tidal
    # playlist importer. Users register their own app at
    # developer.spotify.com and paste the id here. PKCE OAuth so we
    # don't need a secret. Empty string = import feature hidden in UI.
    spotify_client_id: str = ""
    # Which audio engine drives playback. "vlc" (default) uses the
    # long-shipping libvlc path. "pcm" uses the PyAV + sounddevice
    # engine that supports sample-accurate gapless transitions and
    # bit-perfect output. Some features (EQ, device selection) are
    # not yet ported to pcm; the UI surfaces warnings when relevant.
    audio_engine: str = "vlc"


def load_settings() -> Settings:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            valid = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
            return Settings(**valid)
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
