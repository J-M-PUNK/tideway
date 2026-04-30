# -*- mode: python ; coding: utf-8 -*-
#
# Linux PyInstaller spec. Build with:
#   pyinstaller Tideway-linux.spec --noconfirm
#
# Produces dist/Tideway/ — a portable directory containing the
# `Tideway` binary plus its bundled Python runtime and shared libs.
# scripts/build_appimage.sh wraps that directory in an AppImage for
# distribution.
#
# Runtime system requirements (NOT bundled — relied upon from the
# host):
#   - GTK 3 + WebKit2GTK 4.1 (pywebview's GTK backend)
#   - PortAudio (sounddevice's output backend; libportaudio2)
#   - libnotify (desktop notifications)
# These are installed by default on most desktop Linux distros.
# Bundling them via linuxdeploy plugins is a real option for a
# follow-up release if "missing system dep" turns out to be a common
# install failure; for the experimental v1 we prefer a smaller
# AppImage and a documented dependency list.
#
# Prerequisite: run `npm --prefix web run build` first.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

repo_root = Path(SPECPATH).resolve()
dist_dir = repo_root / "web" / "dist"

# Single-source version, same pattern as the macOS / Windows specs so
# the frozen FastAPI server reports the right version through
# /api/version regardless of which platform built it.
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
# when running frozen.
if _version_file.is_file():
    datas.append((str(_version_file), "."))

# Tray icon asset — desktop.py's _find_tray_icon() probes
# <_MEIPASS>/assets/tray-icon.png first.
_tray_icon = repo_root / "assets" / "tray-icon.png"
if _tray_icon.is_file():
    datas.append((str(_tray_icon), "assets"))

binaries: list[tuple[str, str]] = []

# Same collect_all set as the macOS spec — these packages have native
# code or runtime-loaded modules PyInstaller's static analysis
# routinely misses. See Tideway-mac.spec for the per-package rationale.
for pkg in (
    "pydantic",
    "pydantic_core",
    "async_upnp_client",
    "aiohttp",
    "yarl",
    "multidict",
    "frozenlist",
    "defusedxml",
    "didl_lite",
    "voluptuous",
    "curl_cffi",
):
    try:
        _d, _b, _h = collect_all(pkg)
        datas += _d
        binaries += _b
    except Exception:
        pass

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
    # pywebview's Linux backend uses GTK + WebKit2GTK, loaded via
    # pygobject. PyInstaller's static analysis doesn't follow the
    # pywebview platform-dispatch import.
    "webview.platforms.gtk",
    # async-upnp-client + transitive hidden imports.
    "async_upnp_client",
    "async_upnp_client.search",
    "async_upnp_client.aiohttp",
    "async_upnp_client.client_factory",
    "aiohttp",
    "aiohttp.resolver",
    "yarl",
    "multidict",
    "frozenlist",
    "didl_lite",
    "voluptuous",
    "defusedxml",
    "defusedxml.ElementTree",
    # Global media-key listener — pynput's Linux backend requires X11.
    # Wayland sessions will fail to register the global hotkeys but
    # nothing else breaks; the rest of the player still works.
    "pynput",
    "pynput.keyboard",
    "pynput.keyboard._xorg",
    # Tray icon — pystray's Linux backend prefers libappindicator
    # (works under GNOME with the AppIndicator extension, KDE, XFCE),
    # falling back to a plain GTK status icon. We import both so the
    # backend dispatch finds whichever the host happens to provide.
    "pystray",
    "pystray._appindicator",
    "pystray._gtk",
    "PIL.Image",
    "app.spotify_curl_session",
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
        "tkinter",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
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

# No BUNDLE step — Linux doesn't have a .app equivalent. The COLLECT
# above produces dist/Tideway/, and scripts/build_appimage.sh wraps
# that directory into an AppImage for distribution.
_ = APP_VERSION  # Silence unused-name warning — APP_VERSION is here for
                 # parity with the other specs and may be used by a
                 # future linuxdeploy bundling step.
