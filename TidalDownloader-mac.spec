# -*- mode: python ; coding: utf-8 -*-
#
# macOS PyInstaller spec. Build with:
#   pyinstaller TidalDownloader-mac.spec --noconfirm
#
# Produces dist/TidalDownloader.app — a proper .app bundle. Distributing
# outside the App Store still requires:
#   1. A Developer ID Application certificate.
#   2. codesign --deep --options runtime --sign "<Team ID>" dist/TidalDownloader.app
#   3. Notarization via `xcrun notarytool submit ... --wait` then
#      `xcrun stapler staple dist/TidalDownloader.app`.
# Without notarization, Gatekeeper blocks the app on a user's first
# launch and the only bypass is right-click → Open.
#
# Prerequisite: run `npm --prefix web run build` first.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

repo_root = Path(SPECPATH).resolve()
dist_dir = repo_root / "web" / "dist"

# Single-source version read from the repo-root VERSION file so the
# spec's Info.plist, the FastAPI /api/version endpoint, and the
# frontend update-check banner all agree. Defaults to "0.0.0" if the
# file is missing (keeps the build running on a fresh checkout).
_version_file = repo_root / "VERSION"
APP_VERSION = _version_file.read_text().strip() if _version_file.is_file() else "0.0.0"

if not dist_dir.is_dir():
    raise SystemExit(
        f"web/dist not found at {dist_dir}. Run "
        f"`npm --prefix web run build` first."
    )

datas = [
    (str(dist_dir), "web/dist"),
]

# Ship the VERSION file so server.py's _read_app_version() can find it
# when running frozen. Lives at the bundle root; spec path "." stages
# into <bundle>/Contents/Frameworks at runtime (that's _MEIPASS).
if _version_file.is_file():
    datas.append((str(_version_file), "."))

# Tray icon asset — desktop.py's _find_tray_icon() probes
# <_MEIPASS>/assets/tray-icon.png first.
_tray_icon = repo_root / "assets" / "tray-icon.png"
if _tray_icon.is_file():
    datas.append((str(_tray_icon), "assets"))

# Bundled ffmpeg — used by the video downloader for HLS → MP4 remux.
# Shipping it inside the bundle means end users don't have to install
# anything (no `brew install ffmpeg` prompt on first video download).
# Populate vendor/ffmpeg/macos/ffmpeg by running:
#   scripts/fetch_ffmpeg.sh
# The spec stages the binary as an executable so subprocess can exec
# it; app/video_downloader.py's _find_ffmpeg() checks <_MEIPASS>/ffmpeg
# first so the bundled copy wins over any system install.
_ffmpeg_bin = repo_root / "vendor" / "ffmpeg" / "macos" / "ffmpeg"
if _ffmpeg_bin.is_file():
    binaries_ffmpeg = [(str(_ffmpeg_bin), "ffmpeg")]
else:
    binaries_ffmpeg = []
    print(
        "[spec] WARNING: vendor/ffmpeg/macos/ffmpeg missing — video "
        "downloads will require ffmpeg on the user's system. Run "
        "scripts/fetch_ffmpeg.sh to bundle it."
    )

binaries = list(binaries_ffmpeg)

# Bundle libvlc so the native audio engine works on machines without
# VLC installed. We copy the dylibs + plugin directory straight out of
# a locally-installed VLC.app (the spec errors out below if VLC isn't
# there — the user is expected to install it before building). The
# runtime bootstrap in desktop.py points python-vlc at these paths via
# PYTHON_VLC_LIB_PATH / PYTHON_VLC_MODULE_PATH env vars.
vlc_root = Path("/Applications/VLC.app/Contents/MacOS")
if not vlc_root.is_dir():
    raise SystemExit(
        "VLC.app not found at /Applications/VLC.app. Install VLC "
        "(https://www.videolan.org/vlc/) before building — the bundled "
        "libvlc powers Atmos/MQA/360 playback for users without VLC."
    )
for _lib in ("libvlc.dylib", "libvlc.5.dylib", "libvlccore.dylib", "libvlccore.9.dylib"):
    _src = vlc_root / "lib" / _lib
    if _src.is_file():
        datas.append((str(_src), "vlc/lib"))
# Plugin directory — 80MB, ~340 files. PyInstaller's datas walk this
# recursively and mirror the tree under Contents/Resources/vlc/plugins.
datas.append((str(vlc_root / "plugins"), "vlc/plugins"))

# Pull pydantic_core's Rust extension in explicitly — default static
# analysis misses the .so and the app crashes on import at launch.
for pkg in ("pydantic", "pydantic_core"):
    _d, _b, _h = collect_all(pkg)
    datas += _d
    binaries += _b

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
    "webview.platforms.cocoa",
    # Global media-key listener dependencies. pynput's macOS backend
    # pulls Quartz via pyobjc; PyInstaller's static analysis misses
    # the dynamic import.
    "pynput",
    "pynput.keyboard",
    "pynput.keyboard._darwin",
    "Quartz",
    "AppKit",
    "Foundation",
    # Tray icon (NSStatusItem on macOS). pystray's darwin backend
    # pulls AppKit + pyobjc bridges via dynamic import.
    "pystray",
    "pystray._darwin",
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
    excludes=[
        "customtkinter",
        "tkinter",
        "app.gui",
        "app.image_cache",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon_path = repo_root / "assets" / "icon.icns"
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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
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

app = BUNDLE(
    coll,
    name="TidalDownloader.app",
    icon=icon_arg,
    bundle_identifier="com.tidaldownloader.app",
    info_plist={
        # LSUIElement=False means a proper Dock icon + menu bar; the
        # default True would make us a background-only agent.
        "LSUIElement": False,
        "CFBundleName": "Tidal Downloader",
        "CFBundleDisplayName": "Tidal Downloader",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        # Required by pywebview's WKWebView backend on modern macOS.
        "NSHighResolutionCapable": True,
    },
)
