# Releasing Snipux

Snipux is MIT-licensed and published from its own GitHub repository. There
are three channels:

1. **PyPI.** `pip install snipux` / `pipx install snipux`, and what both
   installers (`packaging/install.sh`, `packaging/install.ps1`) and
   `snipux --update` fetch. Published automatically when a `v*` tag is
   pushed -- see [What the tag does](#what-the-tag-does).
2. **The GitHub Release** for the same tag, carrying the same wheel and
   sdist. Created by the same workflow.
3. **The Linux `.deb` and AppImage**, built and attached by the same
   workflow. Both carry their own Python and Qt, so they are what someone
   installs who does not have (or want) either -- see
   [Building the Linux artifacts](#building-the-linux-artifacts).
4. **The Windows `snipux.exe` and `snipux-setup-<version>.exe`**, built and
   attached by the same workflow on a Windows runner. Both ship: the installer for everyone, the portable exe
   for the machines Smart App Control refuses to run an unsigned installer
   on (see [Why the installer is shipped
   unsigned](#why-the-installer-is-shipped-unsigned-alongside-the-portable-exe)).

Work lands on `dev`; `main` moves only when a release is cut, so `main` is
always the last released version.

## Cutting a release

### 1. Merge `dev` into `main`

Open a pull request from `dev` to `main` and merge it once CI is green on
both runners.

### 2. Bump the version, in both places, and date the changelog

```sh
# pyproject.toml     -> version = "0.9.0"
# snipux/__init__.py -> __version__ = "0.9.0"
# CHANGELOG.md       -> "## Unreleased" becomes "## 0.9.0 — YYYY-MM-DD",
#                       with a fresh empty "## Unreleased" above it
```

Commit it as `chore(release): 0.9.0`.

`__version__` is **not** read from `pyproject.toml`. Changing one and not the
other produces a build whose metadata and whose own reported version disagree,
which is the kind of thing nobody notices until a bug report quotes the wrong
number. The release workflow refuses a tag that disagrees with
`pyproject.toml`, but it does not check `__init__.py`.

### 3. Confirm the suite is green, at both scalings

```sh
QT_QPA_PLATFORM=offscreen python -m pytest -q
QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 python -m pytest -q
```

Both, not one. 1.5 catches a whole class of coordinate-space bug that 1.0
cannot -- see TODO.md, "Fractional display scaling".

A green suite is necessary and **not sufficient** here. Read TODO.md's "The
trap, restated" before treating it as a release gate: 1,444 tests once passed
while the primary Linux recording path could not have worked even once. Drive
whatever changed, on a real screen, before tagging.

### 4. Tag it

```sh
git tag -a v0.9.0 -m "v0.9.0"
git push origin v0.9.0
```

### 5. Watch the workflow finish

Nothing else is needed by hand. The tag builds and attaches every artifact:
the wheel and sdist, the `.deb` and AppImage on an Ubuntu runner, and the
Windows exe and installer on a Windows one.

Both packaging jobs are `continue-on-error`, so a failure leaves a release
that is complete apart from that platform's files. Build the missing ones
with the scripts below and `gh release upload` them, or re-run the job.

## What the tag does

`.github/workflows/release.yml` runs on every pushed `v*` tag:

1. checks the tag matches `version` in `pyproject.toml`, and stops if not;
2. builds the wheel and sdist with `python -m build`;
3. publishes them to PyPI with **trusted publishing** -- no API token is
   stored anywhere; PyPI checks the workflow's own identity, which is why
   the job needs `id-token: write` and why the PyPI project names that
   workflow file exactly (renaming it breaks publishing);
4. creates the GitHub Release for the tag (or adds the files to it) with
   generated notes;
5. builds the Linux `.deb` and AppImage on an `ubuntu-22.04` runner,
   smoke-tests the bundle by running `--list-backends` out of it, and
   attaches both to that release;
6. builds the Windows exe and installer on a `windows-latest` runner,
   smoke-tests the exe the same way, and attaches both.

Steps 5 and 6 are `continue-on-error` and run *after* the release exists, on
purpose: a packaging step that fails must not hold back a release whose
wheel, sdist and notes are already good. When one does fail, the release is
complete apart from that platform's files -- build them with the scripts
below and `gh release upload` them, or re-run the job.

PyPI refuses a second upload of a version number that has already been
published, even if that upload was later deleted. A mistake after the tag
means a new patch version, not a re-tag.

### Publishing by hand (only if the workflow cannot)

```sh
rm -rf dist build *.egg-info
python -m build
twine check dist/*
twine upload dist/*
```

The `rm -rf` matters: `python -m build` does not prune stale files from
`dist/`. `twine` wants `__token__` as the username and a PyPI API token as
the password -- set `TWINE_USERNAME`/`TWINE_PASSWORD` in the shell, and never
write the token into a file in this repository.

## Building the Linux artifacts

The release workflow does this, and nothing below is needed to cut a
release. It is here for changing the packaging itself, where the loop is
build, install, run, rather than tag and hope.

```sh
./packaging/linux/build_appimage.sh   # dist/Snipux-<version>-x86_64.AppImage
./packaging/linux/build_deb.sh        # dist/snipux_<version>_amd64.deb
```

Both run on Linux only — PyInstaller bundles the interpreter and libraries
of the machine it runs on, so there is no building either of these from
Windows. Both `source` `build_bundle.sh`, which is what actually produces
`dist/snipux/` (PyInstaller, `packaging/linux/snipux.spec`, in a build venv
under `build/venv-linux`); each then packages that same directory. Building
both in a row builds the bundle twice, which is slower but keeps either
script runnable on its own.

**The bundle is onedir, not onefile, deliberately.** A onefile build unpacks
all of Qt into `/tmp` before `main()` runs, on every launch — dead time
between the keypress and the frozen screen, every single snip. The AppImage
gets the one-file property back anyway by mounting rather than extracting.

**Build on the oldest Ubuntu we support.** glibc is forward- but not
backward-compatible, so an artifact built on a newer release fails on 22.04
with `GLIBC_2.38 not found`. That is why the workflow pins `ubuntu-22.04`
rather than `ubuntu-latest`, and why a hand-built artifact from a newer VM
should not be uploaded to a release.

**What each one installs.** The `.deb` puts the bundle in `/opt/snipux`,
symlinks `/usr/bin/snipux`, and ships a system `.desktop` entry and the
hicolor icons so Snipux is in the application list before it has ever run.
The AppImage installs nothing. In both cases the *first launch* is still
what writes the autostart entry and binds the GNOME shortcut (`app.py`'s
`run_first_launch_setup`), because neither is something a package can write
on a user's behalf. The user-level `.desktop` that first launch also writes
shadows the system one by XDG precedence rather than appearing beside it,
so the application list shows one launcher either way.

**Why the AppImage needed a code change.** `find_console_script()` returns
`sys.executable` for a frozen build, which inside an AppImage is the
squashfs mount for that one run (`/tmp/.mount_snipuxXXXXXX/...`) — a path
that is gone by the time anything reads the `.desktop` entry or the GNOME
shortcut written from it. It reads `$APPIMAGE` first now, which AppRun sets
to the `.AppImage` file itself. Moving that file therefore breaks the
shortcut until Snipux is run once from the new location, which is why the
README says to keep it somewhere it will stay.

## Building the Windows exe

SNX-96 produces a standalone `snipux.exe` that runs with no Python
installed — the sole Windows release artifact (SNX-104: the Inno Setup
installer that used to wrap it, SNX-97, is gone; see "Why there's no
installer" below). This needs a Windows machine (or a VM) with Python
3.10+ and pip on PATH.

```powershell
powershell -File packaging\windows\build.ps1
```

This builds `dist\snipux.exe` with PyInstaller
(`packaging\windows\snipux.spec`) and nothing else — there is no
Add/Remove Programs entry to stamp a version into the way the installer
used to, so the build itself carries no version. The version bump belongs to
[Cutting a release](#cutting-a-release) above, which is also what produces the
tag this exe gets attached to.

What running `snipux.exe` actually does on first launch: relocates itself
to a stable location under the user's own `%LocalAppData%\snipux` (so a
later cleanup of the Downloads folder it was likely run from doesn't break
it), and sets up a Start Menu shortcut, a Startup entry, and the Ctrl+Alt+S
hotkey binding (SNX-95/103) — the same three things `snipux --setup` writes
for a pip/pipx install. `snipux --remove` undoes all of it.

### Building the installer

```powershell
powershell -File packaging\windows\build_installer.ps1
```

Produces `dist\snipux-setup-<version>.exe`, unsigned. It builds the exe
first; pass `-SkipExe` to reuse one you have just built. Inno Setup is the
only extra tool (`winget install --id JRSoftware.InnoSetup`), and the
script names that command if it is missing.

Three things about the installer are load-bearing and explained at length
in `packaging/windows/snipux.iss` itself:

- it installs into `%LOCALAPPDATA%\snipux`, which is
  `platform.windows._portable_exe_path()` — anywhere else and the exe
  duplicates itself there on first launch;
- it is per-user, so there is no UAC prompt;
- it creates no shortcuts, because snipux writes its own on first launch
  and a second set would be a second thing to keep in step.

It also silently removes SNX-97's old install if it finds one — a
different AppId and directory that nothing has replaced since SNX-104, so
without this it stays on the machine forever as an orphaned copy and a
stale Add/Remove Programs row.

### Why the installer is shipped unsigned, alongside the portable exe

SNX-97 built an installer; SNX-104 removed it because Smart App Control, on
by default on a meaningful share of clean Windows 11 installs, blocks an
unsigned installer outright — no "Run anyway" the way SmartScreen offers,
just a message that reads as if the file is corrupt. That is still true and
signing is still what fixes it.

What changed is the reasoning, not the fact. SNX-104 removed the installer
because it was the *only* thing some users could not run; the answer to that
is to ship both, which is what the Releases page does now. The portable exe
is not blocked by Smart App Control, so anyone the installer refuses still
has the route they have always had — and everyone else gets an Add/Remove
Programs entry, an uninstaller, and `winget install snipux`.

A certificate remains a few hundred a year plus a hardware token (see below)
for a tool with a handful of users, which is the trade that has not changed.

### Publishing to winget

The installer is the winget target, not the portable exe: winget's promise
is `winget install` and `winget uninstall` behaving like any other package,
which needs an Add/Remove Programs entry to remove and a silent switch to
install with. Inno provides both, and winget knows Inno's switches from
`InstallerType: inno`.

After the release exists and the installer is attached to it:

```sh
python packaging/winget/render_manifests.py --version 1.0.1     --installer dist/snipux-setup-1.0.1.exe
```

That writes the three manifests into `dist/winget/<version>/`, with the
installer's SHA256 and the URL it will be downloaded from. Submit them
either with `wingetcreate submit --token <pat> dist/winget/<version>` or as
a pull request to `microsoft/winget-pkgs` adding them under
`manifests/c/CydoEntis/Snipux/<version>/`. Microsoft's validation runs the
installer in a sandbox, so a manifest whose hash or URL is wrong fails there
rather than on a user's machine.

The manifests are generated rather than kept in this repository: each names
the version and one carries the installer's digest, so a checked-in copy is
not a stale document but a manifest pointing at the wrong binary.

### Why the exe isn't signed

`snipux.exe` is shipped unsigned, on purpose, and that decision is
recorded here so it isn't rediscovered — and re-debated — the next time
someone notices SmartScreen complaining about a release.

A certificate that Windows actually trusts (an EV or OV code-signing
certificate from a CA in Microsoft's trusted list) costs a few hundred
dollars a year, recurring, for as long as releases keep going out. Since
2023 the CA/Browser Forum has also required the private key to live on a
hardware token (a physical USB device, or an equivalent cloud HSM) rather
than as an importable file — so it isn't a one-time purchase that then
sits in CI; it's an ongoing subscription plus a physical dongle someone has
to hold and plug in to sign each release, or a paid cloud-HSM signing
service standing in for it. That is a real, continuing cost for a free,
unpaid tool at Snipux's scale, and it buys exactly one thing: SmartScreen
and Smart App Control treat the binary as recognized instead of warning
about or blocking it. Users still get the app either way, just with an
extra click (SmartScreen's More info → Run anyway) or, rarely, a need to
install via pipx instead (Smart App Control) — see the README's Windows
section for what that looks like from the user's side. If Snipux's user
base or distribution model changes enough that the warning itself becomes
the blocker, that's the trigger to revisit this, not a fixed schedule.

## Regenerating the app icon

`snipux/design/logo/` vendors the same `snipux-<size>.png` files onto both
platforms: `setup_desktop.install_icons()` copies them into the Linux
hicolor theme, and `setup_desktop.render_ico()` (via
`packaging/windows/build_icon.py`) packs them into the Windows `.ico`
`build.ps1` above embeds. Fixing a size here fixes both surfaces at once —
there is nothing else to update.

The set is produced two different ways, by size (SNX-102):

- **48px and up** (`snipux-48.png` through `snipux-512.png`) are a smooth
  downscale of `snipux.png`, the 1284px master. At these sizes the master's
  full scene — the outer rounded container, the title-bar dots, the dashed
  selection marquee, the inner window, the cursor — still has enough pixels
  per element to survive being scaled down. These are hand-exported from
  the master today; there is no script for this half, and none is needed
  until the master artwork itself changes.
- **16, 24 and 32px** are drawn directly, by
  `snipux/design/logo/generate_small_icons.py`, from simplified artwork
  instead of a downscale of the master. Below 48px the master's detail
  gets maybe two pixels per element and a smooth downscale just averages
  it into an indistinct blur — the actual bug SNX-102 fixed. The fix is
  the standard one for detailed marks at icon sizes: drop the container
  and the chrome, and enlarge the one element that still identifies the
  app at a glance, which for Snipux is the green selection marquee (here,
  its four corner brackets — a continuous dashed outline dissolves into a
  ring at this pen width) with its cursor.

Regenerate the small sizes after changing that design (colours,
proportions, the cursor shape) — or just to confirm nothing has drifted —
with:

```sh
QT_QPA_PLATFORM=offscreen python snipux/design/logo/generate_small_icons.py
```

This overwrites `snipux-16.png`, `snipux-24.png` and `snipux-32.png` in
place and nothing else; `build_icon.py` and `install_icons()` both already
pick up whatever sizes are sitting in `snipux/design/logo/`, so no other
step or code change is needed to ship a regenerated icon on either
platform.
