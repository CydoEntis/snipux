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

# $ErrorActionPreference governs PowerShell's own errors; it does nothing
# about a *native* executable that exits non-zero. python and pyinstaller
# are both native here, so without this every step below could fail and
# the script would still run on to print "Built ...". It did exactly that:
# a PyInstaller run that died with "PermissionError: Access is denied" on
# dist\snipux.exe (the previous build was still running and holding the
# file) reported success and left the stale exe in place, which is a
# uniquely bad way to fail -- the next thing anyone does is run the binary
# they were just told was rebuilt.
function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE"
    }
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Push-Location $RepoRoot
try {
    # The runtime dependencies (PyQt6, jeepney) first, exactly what running
    # or testing snipux needs, then PyInstaller itself via pyproject.toml's
    # `build-windows` extra -- deliberately not in requirements.txt, so
    # `pip install -r requirements.txt` (what the test suite installs from)
    # never pulls in a build-only tool it has no use for.
    Invoke-Checked "pip install -r requirements.txt" {
        python -m pip install --quiet -r requirements.txt
    }
    Invoke-Checked "pip install .[build-windows]" {
        python -m pip install --quiet ".[build-windows]"
    }

    # The Start Menu/Startup-shortcut icon (setup_desktop.render_ico(),
    # SNX-92) built once here from the same vendored PNGs `install_icons()`
    # uses on Linux, so the .exe Explorer shows one instead of a generic
    # icon -- snipux.spec picks this up if present and falls back to none
    # if this step is skipped, fails, or finds nothing to build from.
    Invoke-Checked "build_icon.py" {
        python packaging/windows/build_icon.py
    }

    # The exe cannot be overwritten while a previous build of it is
    # running -- a resident tray app holds its own image open -- and that
    # is the single most likely way this step fails, so say so rather than
    # leaving the caller with a raw PermissionError.
    $ExePath = Join-Path $RepoRoot 'dist\snipux.exe'
    $Running = Get-Process -Name snipux -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $ExePath }
    if ($Running) {
        throw ("$ExePath is running (pid $($Running.Id -join ', ')) and cannot " +
               "be overwritten. Close Snipux from its tray icon, or stop it " +
               "with: Stop-Process -Name snipux")
    }

    Invoke-Checked "pyinstaller" {
        pyinstaller packaging/windows/snipux.spec --noconfirm
    }

    Write-Host "Built $ExePath"
}
finally {
    Pop-Location
}
