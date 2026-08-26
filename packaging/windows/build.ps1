# Builds a standalone snipux.exe (SNX-96) that runs on a machine with no
# Python installed. Run from a checkout with Python 3.10+ and pip on PATH:
#
#   powershell -File packaging\windows\build.ps1
#
# Produces dist\snipux.exe -- the sole Windows release artifact (SNX-104:
# the Inno Setup installer this script used to also build is gone, because
# Smart App Control blocks it outright on a meaningful share of clean
# Windows 11 installs with no way to click through; see the README's Smart
# App Control section and docs/releasing.md). The exe installs itself --
# Start Menu/Startup shortcut, hotkey, a stable install location -- the
# first time it runs (SNX-95/103), which is what let the installer go
# without losing what it was for. Every path below is resolved relative to
# this script, not the caller's working directory, so it can be run from
# anywhere.

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Push-Location $RepoRoot
try {
    # The runtime dependencies (PyQt6, jeepney) first, exactly what running
    # or testing snipux needs, then PyInstaller itself via pyproject.toml's
    # `build-windows` extra -- deliberately not in requirements.txt, so
    # `pip install -r requirements.txt` (what the test suite installs from)
    # never pulls in a build-only tool it has no use for.
    python -m pip install --quiet -r requirements.txt
    python -m pip install --quiet ".[build-windows]"

    # The Start Menu/Startup-shortcut icon (setup_desktop.render_ico(),
    # SNX-92) built once here from the same vendored PNGs `install_icons()`
    # uses on Linux, so the .exe Explorer shows one instead of a generic
    # icon -- snipux.spec picks this up if present and falls back to none
    # if this step is skipped, fails, or finds nothing to build from.
    python packaging/windows/build_icon.py

    pyinstaller packaging/windows/snipux.spec --noconfirm

    Write-Host "Built $(Join-Path $RepoRoot 'dist\snipux.exe')"
}
finally {
    Pop-Location
}
