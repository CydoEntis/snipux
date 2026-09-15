# Installs Snipux on Windows for the current user, from one line:
#
#   irm https://raw.githubusercontent.com/CydoEntis/snipux/main/packaging/install.ps1 | iex
#
# What the README asks people to do by hand -- install Python with "Add to
# PATH" ticked, run a pip command, run Snipux once -- with the two steps that
# actually go wrong handled: finding a Python that is new enough, and being
# told plainly when there is none.
#
# Nothing here needs administrator rights, nothing is written outside the
# user's own profile, and re-running it upgrades an existing install rather
# than fighting it.

$ErrorActionPreference = 'Stop'

function Say($text)  { Write-Host $text }
function Step($text) { Write-Host "==> $text" -ForegroundColor Green }
# `throw`, not `exit`: this script is meant to be run as
# `irm ... | iex`, and `exit` there ends the *session* -- closing the window
# the user is reading the error in.
function Fail($text) { Write-Host "error: $text" -ForegroundColor Red; throw 'Snipux was not installed.' }

if ($env:OS -ne 'Windows_NT') {
    Fail 'this installer is for Windows. On Linux use packaging/install.sh.'
}

# --- a Python new enough to run Snipux --------------------------------------
# `py` (the launcher installed alongside python.org builds) before `python`:
# on a machine with the Microsoft Store stub, a bare `python` opens the Store
# instead of running anything, and `py` is what actually resolves to a real
# interpreter.
function Find-Python {
    foreach ($candidate in @(
        @{ Command = 'py';     Arguments = @('-3') },
        @{ Command = 'python'; Arguments = @() }
    )) {
        $exe = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        # No `2>$null` here, and ErrorActionPreference lowered around the
        # call: PowerShell 5.1 turns a native command's stderr into an
        # ErrorRecord, which under `Stop` is a terminating error -- so a
        # perfectly good interpreter that writes a deprecation warning to
        # stderr would read as "no Python installed". That bug is how an
        # earlier version of this script installed a second Python on a
        # machine that already had one.
        $version = $null
        try {
            $previous = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            # No quotes inside the snippet: PowerShell strips them on the
            # way to a native command, so `print("%d.%d" % ...)` arrives as
            # `print(%d.%d % ...)` -- a syntax error that reads, again, as
            # "no Python installed".
            $version = & $exe.Source @($candidate.Arguments + @('-c', 'import sys;print(sys.version_info[0], sys.version_info[1])'))
        } catch {
            continue
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
        $parts = ($version | Select-Object -First 1).Trim().Split(' ')
        if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10)) {
            return @{
                Source = $exe.Source
                Arguments = $candidate.Arguments
                Version = "$($parts[0]).$($parts[1])"
            }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Step 'Python 3.10+ not found. Installing it with winget...'
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail @'
no Python 3.10+ and no winget to install one with.

Install Python from https://www.python.org/downloads/ -- tick
"Add python.exe to PATH" in the installer -- then run this again.
'@
    }
    winget install --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements --silent
    # winget puts the new interpreter on PATH for *new* processes; this one
    # already has its environment, so read the machine's PATH back rather
    # than telling the user to open another window.
    $env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('PATH', 'User')
    $python = Find-Python
    if (-not $python) {
        Fail 'Python was installed but is still not on PATH. Close this window, open a new one, and run this again.'
    }
}
Say "Using Python $($python.Version) at $($python.Source)"

function Python-Run {
    & $python.Source @($python.Arguments + $args)
    if ($LASTEXITCODE -ne 0) { Fail "command failed: python $($args -join ' ')" }
}

# --- Snipux itself -----------------------------------------------------------
# --upgrade, so this doubles as the update route and so an install made from
# the old GitHub archive URL is replaced in place: same package name, so pip
# removes the old copy itself.
Step 'Installing Snipux from PyPI...'
Python-Run -m pip install --upgrade --quiet snipux

$version = & $python.Source @($python.Arguments + @('-c', 'import snipux; print(snipux.__version__)'))
Say "Installed Snipux $version"

# --- the pieces pip cannot write --------------------------------------------
# Start Menu entry, autostart entry, and the Ctrl+Alt+S shortcut. Snipux does
# this on its first launch too, but doing it here means the shortcut works
# without anyone having to know that.
Step 'Setting up the shortcut and Start Menu entry...'
Python-Run -m snipux --setup

# --- start it ----------------------------------------------------------------
# Matched by launcher name as well as by command line: a copy started from
# the Start Menu or Startup entry is snipuxw.exe with pythonw.exe under it,
# one started by an older version of this script is `pythonw -m snipux`,
# and the portable build is a snipux.exe of its own.
$running = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in 'snipux.exe', 'snipuxw.exe') -or
    (($_.Name -in 'python.exe', 'pythonw.exe') -and $_.CommandLine -like '*snipux*')
}
if ($running) {
    Say ''
    Say 'Snipux is already running. Quit it from the tray and press Ctrl+Alt+S'
    Say 'to start the version just installed.'
} else {
    Step 'Starting Snipux...'
    # snipuxw, the launcher the Start Menu entry points at: it has no console,
    # so there is no window left behind whose closing would end Snipux.
    # Asked of Snipux rather than looked up on PATH, because pip may have put
    # it in a Scripts folder PATH does not include. ErrorActionPreference is
    # lowered around the call for the stderr reason Find-Python gives.
    $launcher = $null
    try {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $launcher = & $python.Source @($python.Arguments + @('-c', 'import snipux.platform.windows as w;print(w.windowless_launcher())')) |
            Select-Object -Last 1
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($launcher -and (Test-Path -LiteralPath $launcher)) {
        Start-Process -FilePath $launcher
    } else {
        Say 'Could not find the Snipux launcher to start. Start Snipux from the Start Menu.'
    }
}

Say ''
Say 'Done. Press Ctrl+Alt+S to take a snip.'
Say 'Snipux sits in the system tray -- click it for Settings.'
Say 'To update later: snipux --update'
