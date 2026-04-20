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

if not dist_dir.is_dir():
    raise SystemExit(
        f"web/dist not found at {dist_dir}. Run "
        f"`npm --prefix web run build` first."
    )

datas = [
    (str(dist_dir), "web/dist"),
]

binaries = []

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
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        # Required by pywebview's WKWebView backend on modern macOS.
        "NSHighResolutionCapable": True,
    },
)
