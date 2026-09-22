# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build spec for the Linux bundle that both `build_appimage.sh`
# and `build_deb.sh` package. It produces `dist/snipux/` -- a directory, not
# a single file -- and the two builders copy that directory into an AppDir
# or under /opt respectively.
#
# **onedir, not onefile, and that is the whole design.** A onefile build
# unpacks the entire bundle (Qt included, ~80 MB) into a fresh /tmp
# directory on *every* launch before main() runs. Snipux is a tray app woken
# by a keypress: the user presses Ctrl+Alt+S expecting the screen to freeze,
# and that unpack is dead time between the keypress and the overlay, on
# every single snip. The AppImage gets the single-file property back for
# free anyway -- squashfs is mounted, not extracted -- so onefile would buy
# nothing and cost the one interaction that matters.
#
# Everything downstream of the grab is ordinary portable Qt (CLAUDE.md), so
# there is nothing platform-specific in here beyond the console setting and
# the lack of a Windows .ico.

import sys
from pathlib import Path

# SPECPATH is PyInstaller's own name for this file's directory, always
# defined in the namespace a .spec is exec'd in -- used instead of
# `Path(__file__)` because a .spec is exec'd as a string, not imported as a
# module, and so has no `__file__` of its own to read.
_PACKAGING_DIR = Path(SPECPATH).resolve().parent
_REPO_ROOT = _PACKAGING_DIR.parent

# packaging/bundle_data.py holds the asset list this shares with the Windows
# spec; see its docstring for why it is not copied into both.
sys.path.insert(0, str(_PACKAGING_DIR))
from bundle_data import bundle_datas  # noqa: E402

block_cipher = None

a = Analysis(
    [str(_REPO_ROOT / "snipux" / "__main__.py")],
    pathex=[str(_REPO_ROOT)],
    binaries=[],
    datas=bundle_datas(_REPO_ROOT),
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
    [],
    exclude_binaries=True,
    name="snipux",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=True, unlike the Windows spec (SNX-100). There is no black
    # terminal window to avoid here: on Linux a process launched from a
    # .desktop entry has no controlling terminal and opens none, while one
    # launched from a shell keeps that shell's -- so `snipux --list-backends`
    # can still print, and the tray app started at login still shows nothing.
    console=True,
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
    name="snipux",
)
