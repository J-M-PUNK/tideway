# -*- mode: python ; coding: utf-8 -*-
#
# Windows PyInstaller spec. Build with:
#   pyinstaller TidalDownloader-win.spec --noconfirm
#
# Produces dist/TidalDownloader/TidalDownloader.exe (onedir). Onedir is
# preferred over onefile here because a user opening 20 tracks in quick
# succession shouldn't incur the onefile unpack tax on each child
# process, and AV scanners flag onefile executables more often.
#
# Prerequisite: run `npm --prefix web run build` first so web/dist/
# exists. PyInstaller bundles whatever is on disk at spec-load time.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

repo_root = Path(SPECPATH).resolve()
dist_dir = repo_root / "web" / "dist"

if not dist_dir.is_dir():
    raise SystemExit(
        f"web/dist not found at {dist_dir}. Run "
        f"`npm --prefix web run build` first."
    )

datas = [
    # (source, dest-inside-bundle)
    (str(dist_dir), "web/dist"),
]

binaries = []

# Bundle libvlc + plugins so the native audio engine (Atmos / MQA /
# Sony 360) works on machines without VLC installed. The runtime
# bootstrap in desktop.py points python-vlc at these paths via
# PYTHON_VLC_LIB_PATH / PYTHON_VLC_MODULE_PATH.
#
# VLC's Windows installer defaults to C:\Program Files\VideoLAN\VLC.
# Environment override VLC_INSTALL_DIR lets CI / non-default installs
# point us elsewhere. The spec errors out if neither is present —
# the build machine is expected to have VLC installed.
import os as _os

_vlc_candidates = []
if _os.environ.get("VLC_INSTALL_DIR"):
    _vlc_candidates.append(Path(_os.environ["VLC_INSTALL_DIR"]))
_vlc_candidates += [
    Path(r"C:\Program Files\VideoLAN\VLC"),
    Path(r"C:\Program Files (x86)\VideoLAN\VLC"),
]
vlc_root = next((p for p in _vlc_candidates if p.is_dir()), None)
if vlc_root is None:
    raise SystemExit(
        "VLC install not found. Install from https://www.videolan.org/vlc/ "
        "or set VLC_INSTALL_DIR to the VLC directory. Required for "
        "bundling libvlc into the app."
    )
for _dll in ("libvlc.dll", "libvlccore.dll"):
    _src = vlc_root / _dll
    if _src.is_file():
        datas.append((str(_src), "vlc"))
if (vlc_root / "plugins").is_dir():
    datas.append((str(vlc_root / "plugins"), "vlc/plugins"))

# pydantic v2's core is a Rust extension (`pydantic_core._pydantic_core`)
# plus a metadata dir. PyInstaller's default static analysis misses the
# compiled .pyd; collect_all grabs data + binaries + submodules in one
# call. Same pattern for any other package with a native extension that
# the default hook doesn't cover.
for pkg in ("pydantic", "pydantic_core"):
    _d, _b, _h = collect_all(pkg)
    datas += _d
    binaries += _b

# tidalapi and uvicorn use dynamic imports that PyInstaller's static
# analysis misses. Collect their submodules explicitly.
hiddenimports = [
    "tidalapi",
    "tidalapi.album",
    "tidalapi.artist",
    "tidalapi.media",
    "tidalapi.page",
    "tidalapi.playlist",
    "tidalapi.user",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "sse_starlette",
    "webview.platforms.edgechromium",
]

a = Analysis(
    ["desktop.py"],
    pathex=[str(repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # customtkinter / app.gui are the legacy Tk UI; exclude so the bundle
    # doesn't drag in tkinter + ~15MB of unused assets.
    excludes=[
        "customtkinter",
        "tkinter",
        "app.gui",
        "app.image_cache",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Icon file is optional — drop a 256x256 .ico at assets/icon.ico to
# customize. Falls back to the generic PyInstaller icon if missing.
icon_path = repo_root / "assets" / "icon.ico"
icon_arg = str(icon_path) if icon_path.is_file() else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TidalDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TidalDownloader",
)
