# -*- mode: python ; coding: utf-8 -*-
#
# Windows PyInstaller spec. Build with:
#   pyinstaller Tideway-win.spec --noconfirm
#
# Produces dist/Tideway/Tideway.exe (onedir). Onedir is
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
_version_file = repo_root / "VERSION"
APP_VERSION = _version_file.read_text().strip() if _version_file.is_file() else "0.0.0"

if not dist_dir.is_dir():
    raise SystemExit(
        f"web/dist not found at {dist_dir}. Run "
        f"`npm --prefix web run build` first."
    )

datas = [
    # (source, dest-inside-bundle)
    (str(dist_dir), "web/dist"),
]

# Ship the VERSION file so the server can read it at runtime.
if _version_file.is_file():
    datas.append((str(_version_file), "."))

# Tray icon asset.
_tray_icon = repo_root / "assets" / "tray-icon.png"
if _tray_icon.is_file():
    datas.append((str(_tray_icon), "assets"))

# Audio + video I/O is handled entirely by PyAV (libav), which
# ships its own libav binaries in its wheel — no external ffmpeg
# needed anywhere in the app.
binaries: list[tuple[str, str]] = []

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
    # Global media-key listener. pynput's Windows backend pulls Win32
    # APIs dynamically; declare the submodule explicitly.
    "pynput",
    "pynput.keyboard",
    "pynput.keyboard._win32",
    # Tray icon (Shell_NotifyIcon on Windows).
    "pystray",
    "pystray._win32",
    "PIL.Image",
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
    name="Tideway",
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
    name="Tideway",
)
