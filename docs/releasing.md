# Releasing Snipux

Snipux is MIT-licensed and distributed from its own GitHub repository. There
are two channels today, and neither is PyPI:

1. **The git install.** `pipx install git+https://github.com/CydoEntis/snipux.git`
   builds from whatever `main` is at that moment. Nothing has to be published
   for this to work -- `main` *is* the release, which is why it must stay
   green.
2. **The Windows `snipux.exe`**, attached to a
   [GitHub Release](https://github.com/CydoEntis/snipux/releases). This is the
   only artifact that gets uploaded anywhere, and the only one that needs the
   procedure below.

PyPI is set up for but deliberately not used -- see
"[Publishing to PyPI](#publishing-to-pypi-not-done-today)" at the bottom for
what it would take and why it is not done.

## Cutting a release

### 1. Bump the version, in both places

```sh
# pyproject.toml     -> version = "0.2.0"
# snipux/__init__.py -> __version__ = "0.2.0"
```

`__version__` is **not** read from `pyproject.toml`. Changing one and not the
other produces a build whose metadata and whose own reported version disagree,
which is the kind of thing nobody notices until a bug report quotes the wrong
number.

### 2. Confirm the suite is green, at both scalings

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

### 3. Tag it

```sh
git tag -a v0.2.0 -m "v0.2.0"
git push origin v0.2.0
```

The tag is what a user pins to:
`pipx install git+https://github.com/CydoEntis/snipux.git@v0.2.0`.

### 4. Build the Windows exe

On a Windows machine or VM -- see
[Building the Windows exe](#building-the-windows-exe) below. Then create the
GitHub Release against the tag and attach `dist\snipux.exe`:

```sh
gh release create v0.2.0 dist/snipux.exe \
  --title "v0.2.0" --notes "..."
```

Linux needs no artifact: the git install above covers it.

## Publishing to PyPI (not done today)

`snipux` is unclaimed on PyPI and the packaging metadata is already valid for
it, so this is available whenever it is wanted. It is not done because the git
install already gives a one-command install on both platforms, and publishing
adds a namespace to own and a release cadence to keep up with in exchange for
`pip install snipux` and discoverability.

If that trade changes, this is the whole procedure. It needs a PyPI account
with ownership of the `snipux` project name and an API token for it -- the
token must never be committed anywhere in this repository.

### Build clean artifacts

```sh
rm -rf dist build *.egg-info
python -m build
```

`build` reads `[project]` in `pyproject.toml` and produces both a wheel and
an sdist under `dist/`. The `rm -rf` first matters: `python -m build` does
not prune stale files from a previous version out of `dist/`, so skipping it
risks uploading an old artifact alongside the new one.

Before uploading, sanity-check what got built:

```sh
twine check dist/*
```

`twine check` catches the two things PyPI itself rejects at upload time -- a
`long_description` that fails to render, and metadata missing fields PyPI
requires. Fix and rebuild rather than uploading and finding out from a failed
upload.

### Upload

```sh
twine upload dist/*
```

`twine` prompts for credentials; use `__token__` as the username and the
PyPI API token as the password. To skip the prompt, export
`TWINE_USERNAME=__token__` and `TWINE_PASSWORD=<token>` in the shell before
running the command -- never write the token itself into a file in this
repository.

PyPI refuses to accept a second upload of a version number that has already
been published, even if the previous upload was later deleted. So the version
bump in step 1 above is not optional here, it is load-bearing.

**If this is ever done, update the README's Install section** -- it currently
documents the git install as the only route, on purpose, so that nothing tells
a user to run a command that 404s.

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

### Why there's no installer

SNX-97 originally wrapped `snipux.exe` in an Inno Setup installer,
`snipux-setup.exe`, so a user wouldn't need to know where to put the file.
SNX-104 removed it: Smart App Control, a Windows 11 feature that is on by
default on a meaningful share of clean installs, blocked that installer
outright — no "Run anyway" the way SmartScreen offers, just a message that
read like the file was corrupt. For those users the installer did not
merely inconvenience, it did not work at all. The portable exe is not
blocked by Smart App Control, and since it already sets itself up on first
run (see above), it delivers what the installer was for without the thing
that broke it. A build artifact nobody can run is worse than none — someone
will try it — so the installer was deleted rather than left in the
repository. Don't re-add one without re-reading this.

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
