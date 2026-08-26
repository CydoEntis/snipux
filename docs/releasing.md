# Releasing Snipux to PyPI

Publishing needs a PyPI account with ownership of the `snipux` project name
and an API token for it — only the account owner has that token, and it must
never be committed anywhere in this repository. This page assumes both
already exist; it records the two commands that turn a checkout into a
published release, nothing else.

## 1. Build clean artifacts

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

`twine check` catches the two things PyPI itself rejects at upload time — a
`long_description` that fails to render, and metadata missing fields PyPI
requires. Fix and rebuild rather than uploading and finding out from a failed
upload.

## 2. Upload

```sh
twine upload dist/*
```

`twine` prompts for credentials; use `__token__` as the username and the
PyPI API token as the password. To skip the prompt, export
`TWINE_USERNAME=__token__` and `TWINE_PASSWORD=<token>` in the shell before
running the command — never write the token itself into a file in this
repository.

## Before either command

Bump `version` in `pyproject.toml` (and `__version__` in
`snipux/__init__.py`, which is not read from it). PyPI refuses to accept a
second upload of a version number that has already been published, even if
the previous upload was later deleted.

# Releasing the Windows exe

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
used to, so unlike the PyPI half above there is no version to bump first.

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

# Regenerating the app icon

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
