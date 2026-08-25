# Builds a standalone snipux.exe (SNX-96) and wraps it in a real Windows
# installer (SNX-97) that runs on a machine with no Python installed. Run
# from a checkout with Python 3.10+ and pip on PATH, and Inno Setup
# installed (https://jrsoftware.org/isdl.php, or `winget install
# JRSoftware.InnoSetup`) for the second half:
#
#   powershell -File packaging\windows\build.ps1
#
# Produces dist\snipux.exe and dist\snipux-setup.exe. Every path below is
# resolved relative to this script, not the caller's working directory,
# so it can be run from anywhere.

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
    # snipux.iss (below) reuses the same file for the installer's own icon
    # and the Add/Remove Programs entry.
    python packaging/windows/build_icon.py

    pyinstaller packaging/windows/snipux.spec --noconfirm

    Write-Host "Built $(Join-Path $RepoRoot 'dist\snipux.exe')"

    # pyproject.toml's `[project] version`, read with a plain regex rather
    # than `tomllib` (stdlib only since 3.11; this project's own floor is
    # 3.10 -- see pyproject.toml's `requires-python`) or a dependency this
    # is the only caller of. Passed to Inno Setup below so the installer
    # and the Add/Remove Programs entry it registers report the real
    # release version instead of snipux.iss's own placeholder default.
    $versionMatch = Select-String -Path (Join-Path $RepoRoot "pyproject.toml") `
        -Pattern '^version\s*=\s*"([^"]+)"'
    if (-not $versionMatch) {
        throw "Could not find version = `"...`" in pyproject.toml"
    }
    $version = $versionMatch.Matches[0].Groups[1].Value

    # Not on PATH by default -- the Inno Setup installer adds a Start Menu
    # shortcut for the IDE, not ISCC.exe itself to PATH -- so both the
    # common install locations are checked before giving up with a clear
    # instruction, the same "fail fast, name the fix" style `find_console_script`
    # / `install_desktop_integration` already use for their own missing
    # prerequisites.
    $iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($iscc) {
        $isccPath = $iscc.Source
    } else {
        $candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        )
        $isccPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $isccPath) {
        throw "ISCC.exe (Inno Setup) not found on PATH or in its default install " +
            "location. Install it from https://jrsoftware.org/isdl.php (or " +
            "winget install JRSoftware.InnoSetup), then re-run this script."
    }

    & $isccPath "/DMyAppVersion=$version" "packaging\windows\snipux.iss"

    Write-Host "Built $(Join-Path $RepoRoot 'dist\snipux-setup.exe')"
}
finally {
    Pop-Location
}
