# Next: merge the branch, then a new feature

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-107** is merged. **Linux and Windows are both
implemented.** macOS is stubbed.

## Pick up here

    gh pr create --head punch/SNX-108

**`punch/SNX-108` is done and pushed, not merged.** Six code commits, 1,242
tests passing, and run by hand on a real GNOME/X11 desktop:

- **SNX-108** — the chooser's widgets swallow their own mouse presses. A
  press a widget does not accept goes to its parent, and the parent is the
  overlay, which reads one as the start of a region drag.
- **The same bug in the overlay's own chrome.** Every painted chrome widget
  had it; on the floating bar it threw away the selection just dragged out.
- **The GNOME top bar was hiding the chooser.** `Platform.reserved_top()`
  is the new seam. `availableGeometry()` is not the fix — Qt reports no
  strut at all on X11, so Linux reads `_NET_WORKAREA` instead.
- **The "then" menu now asks where you edit** — Instant / Edit / Review, in
  place of Review / Copy / Save. `instant` is new; the other two existed
  under other names.
- **A refused Window mode reaches the chooser**, not just the chip.
- `verify_bug.py` deleted.

Four tests fail on Linux and failed before this branch: three in
`TestCreateShortcut` need `ctypes.WINFUNCTYPE` and can only run on Windows,
one asserts a font metric this box does not reproduce. Both sets are worth
a skip marker rather than a red suite.

Then **rebuild `dist\snipux.exe`** on Windows — PyInstaller cannot
cross-compile:

    powershell -File packaging\windows\build.ps1

## Left open by that branch

**The top bar fix is unverified on Wayland.** It reserves nothing there on
purpose: `show_on_screen` fullscreens the overlay onto one output and GNOME
hides its bar for a fullscreen window. That reasoning has not been watched
happen. One launch on a Wayland session settles it.

**Instant always copies.** There is no copy-vs-save preference to read, so
instant-save cannot be asked for. The obvious home for one already exists
and does nothing:

**"Always copy to clipboard too" is a dead setting.** `load_always_copy` is
written by Settings and read by no one. Wiring it is its own decision --
either it starts meaning something, or it comes out.

## Installing

    pipx install git+https://github.com/CydoEntis/snipux.git
    snipux

First launch sets itself up. `snipux --remove` undoes it before
`pipx uninstall snipux`.

**Windows without Python:** build `dist\snipux.exe` with
`packaging\windows\build.ps1` and hand over that one file. It installs itself
on first run. **The prebuilt exe in `dist/` predates SNX-108** — rebuild it from
`punch/SNX-108`.

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

A green suite here is weak evidence. All 1,242 tests run headless — no
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

**Wayland, since the Windows port.** X11 has now been run from
`punch/SNX-108` and it took one launch to surface the top bar hiding the
chooser -- something the whole headless suite had nothing to say about.
Wayland is the primary session type and has not been run since the platform
seam went in.
