# -*- mode: python ; coding: utf-8 -*-
#
# macOS PyInstaller spec. Build with:
#   pyinstaller Tideway-mac.spec --noconfirm
#
# Produces dist/Tideway.app — a proper .app bundle. Distributing
# outside the App Store still requires:
#   1. A Developer ID Application certificate.
#   2. codesign --deep --options runtime --sign "<Team ID>" dist/Tideway.app
#   3. Notarization via `xcrun notarytool submit ... --wait` then
#      `xcrun stapler staple dist/Tideway.app`.
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

# Audio + video I/O is handled entirely by PyAV (libav), which
# ships its own libav binaries in its wheel — no external ffmpeg
# needed anywhere in the app.
binaries: list[tuple[str, str]] = []

# Pull pydantic_core's Rust extension in explicitly — default static
# analysis misses the .so and the app crashes on import at launch.
# Same applies to async-upnp-client's transitive stack (aiohttp + its
# C extensions, yarl, multidict, frozenlist, defusedxml,
# python-didl-lite, voluptuous) — the UPnP discovery path imports
# them on a daemon thread at startup and missing submodules there
# show up as a SIGTRAP on Thread-N with no other clue.
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
    # curl-cffi ships its own libcurl-impersonate dylib + a CFFI
    # binding that PyInstaller's static analysis routinely misses.
    # Without this the packaged app falls back to plain requests
    # silently, losing the TLS-fingerprint match.
    "curl_cffi",
    # tls-client ships per-arch native binaries
    # (tls-client-arm64.dylib, tls-client-amd64.so, etc.) under
    # tls_client/dependencies/. spotapi loads one of those via
    # ctypes at runtime, but PyInstaller's static analysis only
    # sees the python imports — without collect_all the dylibs
    # don't ship and every Spotify call dies with
    # "Failed to load dynlib/dll '.../tls-client-<arch>.dylib'."
    # That's the missing-monthly-listeners + zero-playcount bug
    # from v0.4.8.
    "tls_client",
):
    try:
        _d, _b, _h = collect_all(pkg)
        datas += _d
        binaries += _b
    except Exception:
        # Optional deps the project will boot without (async-upnp-client
        # is flagged optional in app/audio/upnp.py) shouldn't break the
        # build if the user hasn't installed them.
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
    "webview.platforms.cocoa",
    # async-upnp-client + transitive hidden imports. collect_all
    # above pulls most of these as data, but several submodules
    # are loaded dynamically (async_upnp_client.search picks a
    # backend at runtime) and need to be spelled out explicitly.
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
    # tkinter isn't imported by anything we ship, but Python's stdlib
    # _includes_ it, so PyInstaller will happily bundle it unless we
    # say otherwise. Exclude to keep the bundle lean.
    excludes=[
        "tkinter",
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
    name="Tideway",
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
    name="Tideway",
)

app = BUNDLE(
    coll,
    name="Tideway.app",
    icon=icon_arg,
    bundle_identifier="com.tidaldownloader.app",
    info_plist={
        # LSUIElement=False means a proper Dock icon + menu bar; the
        # default True would make us a background-only agent.
        "LSUIElement": False,
        "CFBundleName": "Tideway",
        "CFBundleDisplayName": "Tideway",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        # Required by pywebview's WKWebView backend on modern macOS.
        "NSHighResolutionCapable": True,
    },
)
