import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

SETTINGS_FILE = Path("settings.json")


@dataclass
class Settings:
    output_dir: str = str(Path.home() / "Music" / "Tidal")
    quality: str = "high_lossless"  # low_96k | low_320k | high_lossless | hi_res_lossless
    filename_template: str = "{artist} - {title}"
    create_album_folders: bool = True
    skip_existing: bool = True
    # How many downloads may run in parallel. Gated by the Downloader's
    # semaphore so changing this doesn't require a process restart.
    concurrent_downloads: int = 3


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
        prefix=".settings.", suffix=".tmp", dir=str(target.parent) or "."
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
