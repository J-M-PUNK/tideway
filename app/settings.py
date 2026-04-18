import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SETTINGS_FILE = Path("settings.json")


@dataclass
class Settings:
    output_dir: str = str(Path.home() / "Music" / "Tidal")
    quality: str = "high_lossless"  # high_lossless | hi_res | hi_res_lossless
    filename_template: str = "{artist} - {title}"
    create_album_folders: bool = True
    skip_existing: bool = True


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
    with open(SETTINGS_FILE, "w") as f:
        json.dump(asdict(settings), f, indent=2)
