# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build spec for snipux.exe (SNX-96): a standalone Windows
# executable that runs with no Python installed. Built via
# `packaging/windows/build.ps1`, which is the documented command --
# running `pyinstaller` on this file directly also works, provided
# `pip install .[build-windows]` (pyproject.toml) has already put
# PyInstaller itself somewhere on PATH; see build.ps1's own comments for
# why that stays out of requirements.txt.
#
# What ships in the bundle, and why, is entirely a data-placement problem:
# every design asset (icons, the app logo, the .desktop template) is
# ordinary Python code and data that already works from a source checkout
# and a plain `pip install` (`snipux/design/__init__.py`'s `PACKAGE_DIR`,
# SNX-96) -- PyInstaller just needs to be told to place that data at the
# same `snipux/...`-relative paths inside the bundle that `PACKAGE_DIR`
# resolves to under `sys._MEIPASS`. Nothing about capture, the overlay,
# annotation or the platform seam needs anything spec- or
# PyInstaller-specific at all.

import sys
from pathlib import Path

# SPECPATH is PyInstaller's own name for this file's directory, always
# defined in the namespace a .spec is exec'd in -- used instead of
# `Path(__file__)` because a .spec is exec'd as a string, not imported as a
# module, and so has no `__file__` of its own to read.
_REPO_ROOT = Path(SPECPATH).resolve().parent.parent
_SNIPUX_DIR = _REPO_ROOT / "snipux"

# One (source, destination) pair per asset directory PACKAGE_DIR-based code
# reads at runtime -- design/__init__.py's icons/fonts, app.py's tray/window
# logo, and setup_desktop.py's .desktop template (SNX-73), even though only
# Linux's install_desktop_integration() ever actually renders it; bundling
# it unconditionally here is simpler and cheaper than teaching this spec
# which platform it is building for, and it is a handful of bytes of text.
_datas = [
    (str(_SNIPUX_DIR / "design" / "icons"), "snipux/design/icons"),
    (str(_SNIPUX_DIR / "design" / "logo"), "snipux/design/logo"),
    (str(_SNIPUX_DIR / "snipux.desktop"), "snipux"),
]

# design/fonts/ is empty in this handoff (see design/__init__.py's own
# docstring) -- bundling an empty directory is a no-op PyInstaller doesn't
# need telling about, so this only adds the pair once there is something in
# it for font_families() to find.
_fonts_dir = _SNIPUX_DIR / "design" / "fonts"
if _fonts_dir.is_dir() and any(_fonts_dir.iterdir()):
    _datas.append((str(_fonts_dir), "snipux/design/fonts"))

# build.ps1 writes this from setup_desktop.render_ico() before invoking
# PyInstaller, the same vendored PNGs install_icons()/_write_icon() already
# use on Linux/Windows for every other icon -- so the .exe Explorer shows
# isn't the PyInstaller default. Entirely optional: a checkout that runs
# `pyinstaller` on this spec without that step still produces a working
# executable, just with a generic icon.
_icon_path = _REPO_ROOT / "build" / "snipux.ico"

block_cipher = None

a = Analysis(
    [str(_SNIPUX_DIR / "__main__.py")],
    pathex=[str(_REPO_ROOT)],
    binaries=[],
    datas=_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="snipux",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windowed, not console (SNX-100): a tray app popping a black terminal
    # window behind it on every launch -- double-click, Start Menu,
    # Startup, or the installer's own first-run -- looked broken rather
    # than merely quiet, and was the first thing a new user saw. This used
    # to be console=True specifically so --setup/--remove/--snip/
    # --list-backends could still print (SNX-96's own acceptance
    # criterion), but that traded away silence in the one case that
    # actually mattered (the resident tray app, launched with no
    # arguments) to keep it in the ones that don't need a window at all --
    # a terminal running one of those flags is still there to attach to.
    # `snipux.platform.windows.reattach_console()`, called first thing by
    # `app.py`'s `cli()`, is what makes both halves true at once:
    # `AttachConsole(ATTACH_PARENT_PROCESS)` reattaches to a real caller's
    # console when there is one (a terminal), and points stdout/stderr at
    # `os.devnull` instead when there isn't (Explorer, a shortcut, the
    # installer) -- so print() never crashes, and no window ever appears
    # on its own.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_icon_path) if _icon_path.is_file() else None,
)
