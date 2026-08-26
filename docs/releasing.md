# Releasing snipux to PyPI

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

# Releasing the Windows installer

SNX-96 already produces a standalone `snipux.exe` that runs with no Python
installed. SNX-97 wraps that one file in a real installer, so a user does not
also need to know where to put it. This needs a Windows machine (or a VM) with
Python 3.10+ and pip on PATH, plus [Inno Setup 6](https://jrsoftware.org/isdl.php)
(`winget install JRSoftware.InnoSetup`) installed anywhere — the packaging
equivalent of the PyPI account this page's other half assumes. `build.ps1`
finds `ISCC.exe` itself: on PATH if it's there, otherwise in the machine-wide
Program Files locations or the per-user `LocalAppData\Programs` one that a
plain `winget install` (no admin prompt) actually uses, so nothing needs to
be added to PATH by hand (SNX-99).

```powershell
powershell -File packaging\windows\build.ps1
```

This one command does both halves: builds `dist\snipux.exe` with PyInstaller
(`packaging\windows\snipux.spec`), then feeds it to Inno Setup
(`packaging\windows\snipux.iss`) to produce `dist\snipux-setup.exe`. The
version reported in the installer, and later in Add/Remove Programs, is read
straight out of `pyproject.toml` — the same `version` bumped above, not a
second place to remember.

What running `snipux-setup.exe` actually does:

- Installs to the user's own `%LocalAppData%\Programs\snipux` by default, no
  administrator prompt — Inno's `PrivilegesRequired=lowest` combined with
  `{autopf}` in `snipux.iss`. Choosing a machine-wide Program Files install
  from the installer's UI (or `/ALLUSERS` on its command line) is still
  possible, and is the only case that prompts for elevation.
- Registers snipux in Add/Remove Programs with the app icon and the version
  above. A fixed `AppId` (a GUID baked into `snipux.iss`, never regenerated
  release to release) is what makes installing a newer version overwrite the
  old one's files and reuse that one entry, rather than leaving two snipux
  entries side by side.
- Places the exe and nothing else. It deliberately does not create a Start
  Menu shortcut, a Startup entry, or a hotkey binding of its own — snipux
  already does all three itself, the first time it actually runs (SNX-95),
  and the installer would only be duplicating that. The installer does launch
  snipux once after a finished install so that first run actually happens
  without the user having to go find the exe themselves.
- On uninstall, runs `snipux.exe --remove` (undoing everything the first
  launch above set up) before deleting the exe itself, after first killing
  any snipux process still running — both so the exe isn't locked and so the
  `RegisterHotKey` registration a running instance holds is released. Nothing
  snipux ever wrote is left behind.

### Why the installer isn't signed

`snipux-setup.exe` and `snipux.exe` are shipped unsigned, on purpose, and
that decision is recorded here so it isn't rediscovered — and re-debated —
the next time someone notices SmartScreen complaining about a release.

A certificate that Windows actually trusts (an EV or OV code-signing
certificate from a CA in Microsoft's trusted list) costs a few hundred
dollars a year, recurring, for as long as releases keep going out. Since
2023 the CA/Browser Forum has also required the private key to live on a
hardware token (a physical USB device, or an equivalent cloud HSM) rather
than as an importable file — so it isn't a one-time purchase that then
sits in CI; it's an ongoing subscription plus a physical dongle someone has
to hold and plug in to sign each release, or a paid cloud-HSM signing
service standing in for it. That is a real, continuing cost for a free,
unpaid tool at snipux's scale, and it buys exactly one thing: SmartScreen
and Smart App Control treat the binary as recognized instead of warning
about or blocking it. Users still get the app either way, just with an
extra click (SmartScreen's More info → Run anyway) or, rarely, a need to
install via pipx instead (Smart App Control) — see the README's Windows
section for what that looks like from the user's side. If snipux's user
base or distribution model changes enough that the warning itself becomes
the blocker, that's the trigger to revisit this, not a fixed schedule.
