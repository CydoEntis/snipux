# Next: use it, on both platforms

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-107** is merged. `main` runs 1,184 tests, 14 skipped.
**Linux and Windows are both implemented.** macOS is stubbed.

## Installing

    pipx install git+https://github.com/CydoEntis/snipux.git
    snipux

First launch sets itself up — desktop entry, autostart, and the shortcut.
`snipux --remove` undoes all of it before `pipx uninstall snipux`.

**Windows without Python:** build `dist\snipux.exe` with
`packaging\windows\build.ps1` and hand over that one file. It installs itself
on first run.

## Deviations and decisions — deliberate, do not revert

**No Windows installer.** Smart App Control blocks unsigned installers
outright on default Windows 11 — a refusal, not a warning the user can click
through. The portable exe is not blocked. Signing is ~$200-400/yr plus a
hardware token, and was declined. Do not rebuild the Inno Setup script
without knowing this.

**Capture never uses `QScreen.grabWindow(0)` on Linux** — black on Wayland.

**Minimum selection is 16x16**, not the handoff's 200x140, so a taskbar icon
stays snippable.

**Eleven tools, not the handoff's eight.** Ellipse, Line and Crop live in a
popover off the rect button.

**The hint HUD is off by default.**

**Windows uses Ctrl+Alt+S.** Win+Shift+S belongs to the Windows Snipping Tool
and `RegisterHotKey` refuses it outright.

**Snipux is capitalised in display text and lowercase in anything typed** —
the command, package, imports and repo stay `snipux`.

## The trap this project keeps falling into

A green test suite here is weak evidence. All 1,184 tests run headless, with
no compositor, no window manager and no keyboard. That suite was green while
the app shipped: a package that could not import, an overlay with no way to
make a selection, invisible blur, a toolbar clipped to single letters, a
terminal window on every launch, and an icon that was an unreadable smudge.
Every one was found by running it.

Two specific habits that catch these:

- **Do not seed state the app never sets.** A check that calls
  `set_selection()` before testing drawing proves nothing about the path that
  creates a selection. Start from what `app.py` actually constructs.
- **Grep for "later ticket" after any big change.** Several real bugs were
  agents deferring work in a comment where no such ticket existed.

## What has never been tested

**Fractional display scaling.** Multi-monitor is now covered properly — three
displays including one at negative coordinates, verified on real Windows
hardware — but no display in this project has ever had a scale factor other
than 1.0. That is the classic place coordinate bugs hide.

**macOS**, entirely. The seam is there so it slots in without rework, but it
needs a real Mac.

**The Linux side since the Windows port.** The platform seam moved Linux code
behind an interface; the suite covers it, but nobody has run Snipux on Linux
since.
