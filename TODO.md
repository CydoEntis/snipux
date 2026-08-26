# Next: one known bug, then real use

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-107** is merged. `main` runs 1,184 tests, 14 skipped.
**Linux and Windows are both implemented.** macOS is stubbed.

## Pick up here

    punch work SNX-108

**SNX-108 is written, diagnosed, and not built.** A run was started and
stopped part-way; the ticket is back in the ready column and nothing is
claimed.

### The bug, and what is already known about it

Clicking the pre-snip chooser's `Region` dropdown does not open its menu — it
starts a region capture and drops you into the overlay. Recorded on video;
frames 1–3 show the chooser, frame 4 is already the overlay with the Frozen
pill.

The mechanism is Qt event propagation, and it is three lines:

    _MenuRow   press=False  release=True   <-- press leaks to parent
    _Trigger   press=False  release=True   <-- press leaks to parent
    _Tab       press=False  release=True   <-- press leaks to parent

All three in `chooser.py` implement `mouseReleaseEvent` and no
`mousePressEvent`. A Qt widget that does not accept a press lets it propagate
to its parent — the overlay — whose handler treats a press with no selection
as the start of a region drag. That overlay rule is correct and should not
change; its own docstring states it. **The fix belongs in `chooser.py`.**

## Installing

    pipx install git+https://github.com/CydoEntis/snipux.git
    snipux

First launch sets itself up. `snipux --remove` undoes it before
`pipx uninstall snipux`.

**Windows without Python:** build `dist\snipux.exe` with
`packaging\windows\build.ps1` and hand over that one file. It installs itself
on first run. **The prebuilt exe in `dist/` predates SNX-108** — rebuild after
fixing it.

## Decisions — deliberate, do not revert

**No Windows installer.** Smart App Control blocks unsigned installers
outright on default Windows 11 — a refusal, not a warning that can be clicked
through. The portable exe is not blocked. Signing is ~$200-400/yr plus a
hardware token, and was declined. Do not rebuild the Inno Setup script without
knowing this.

**Capture never uses `QScreen.grabWindow(0)` on Linux** — black on Wayland.

**Minimum selection is 16x16**, not the handoff's 200x140.

**Eleven tools, not the handoff's eight.** Ellipse, Line and Crop live in a
popover off the rect button.

**The hint HUD is off by default.**

**Windows uses Ctrl+Alt+S.** Win+Shift+S belongs to the Windows Snipping Tool
and `RegisterHotKey` refuses it outright.

**Snipux is capitalised in display text, lowercase in anything typed** — the
command, package, imports and repo stay `snipux`.

## The trap this project keeps falling into

A green suite here is weak evidence. All 1,184 tests run headless — no
compositor, no window manager, no keyboard. That suite was green while the app
shipped: a package that could not import, an overlay with no way to select,
invisible blur, a toolbar clipped to single letters, a terminal window on
every launch, an unreadable icon, and now a chooser whose buttons start a
capture.

Three habits that catch these:

- **Do not seed state the app never sets.** A check that calls
  `set_selection()` before testing drawing proves nothing about the path that
  creates a selection. Start from what `app.py` constructs.
- **Grep for "later ticket" after any big change.** Several real bugs were
  agents deferring work in a comment where no such ticket existed.
- **For anything interaction-shaped, get a screen recording.** SNX-108 took
  four wrong guesses from reading code and two minutes from a video.

## What has never been tested

**Fractional display scaling.** Multi-monitor is covered — three displays
including one at negative coordinates, on real Windows hardware — but no
display here has ever had a scale factor other than 1.0.

**macOS**, entirely. The seam exists so it slots in without rework; it needs a
real Mac for the Screen Recording and Accessibility permissions.

**The Linux side since the Windows port.** The platform seam moved Linux code
behind an interface. Tests cover it; nobody has run Snipux on Linux since.
