# Builds dist\snipux-setup.exe: the Windows installer, unsigned.
#
#   powershell -File packaging\windows\build_installer.ps1
#
# Runs build.ps1 first (so the exe inside the installer is always the one
# this checkout produces, never a stale dist\snipux.exe), then compiles
# packaging\windows\snipux.iss with Inno Setup. Pass -SkipExe to reuse an
# exe you have just built by hand.
#
# **The installer does not replace the portable exe.** Both ship, for the
# reason snipux.iss's own header sets out: Smart App Control blocks an
# unsigned installer outright, with no way to click through, while the
# portable exe it blocks nothing. Anyone that installer refuses to run for
# still has the route they have always had -- which is what makes shipping
# an unsigned one reasonable rather than a repeat of SNX-104.

param([switch]$SkipExe)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE"
    }
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# Inno installs per-user by default (winget's package does), so Program
# Files is checked second rather than first, and ISCC on PATH last. Named
# rather than searched for: a recursive scan of C:\ to find a build tool is
# slower than telling someone the one command that installs it.
$IsccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    $Iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
}
if (-not $Iscc) {
    throw @"
Inno Setup is not installed, so there is nothing to compile the installer with.
Install it and run this again:

    winget install --id JRSoftware.InnoSetup

Building dist\snipux.exe on its own needs none of this -- see build.ps1.
"@
}

Push-Location $RepoRoot
try {
    if (-not $SkipExe) {
        Invoke-Checked "build.ps1" {
            powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build.ps1")
        }
    }

    $Exe = Join-Path $RepoRoot "dist\snipux.exe"
    if (-not (Test-Path $Exe)) {
        throw "dist\snipux.exe is missing -- run without -SkipExe, or build it first."
    }

    # The same version the app reports, read from __init__.py rather than
    # pyproject.toml: that is the one the running application shows in
    # Settings, and an installer whose Add/Remove Programs entry disagrees
    # with it is the kind of thing nobody notices until a bug report quotes
    # the wrong number.
    $Version = (Select-String -Path (Join-Path $RepoRoot "snipux\__init__.py") `
        -Pattern '^__version__ = "([^"]+)"').Matches[0].Groups[1].Value
    if (-not $Version) {
        throw "could not read __version__ from snipux\__init__.py"
    }

    Write-Host "Building the installer for snipux $Version..."
    Invoke-Checked "ISCC" {
        & $Iscc "/DAppVersion=$Version" (Join-Path $PSScriptRoot "snipux.iss")
    }

    # Named with its version only at the end: Inno writes
    # OutputBaseFilename, and a version in the .iss would have to be kept in
    # step with __init__.py by hand.
    $Built = Join-Path $RepoRoot "dist\snipux-setup.exe"
    $Final = Join-Path $RepoRoot "dist\snipux-setup-$Version.exe"
    if (Test-Path $Final) { Remove-Item $Final }
    Move-Item $Built $Final

    Write-Host "Built $Final"
    Write-Host "Unsigned, deliberately -- see snipux.iss for what that costs and why."
}
finally {
    Pop-Location
}
