# Next: two asks from real use, and a branch waiting on a PR

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-107** is merged. **Linux and Windows are both
implemented.** macOS is stubbed.

## Pick up here

    gh pr create --head punch/SNX-108

**SNX-108 is fixed and pushed, not merged.** Four commits on
`punch/SNX-108`, 1,211 tests passing:

- the chooser's own widgets swallow their mouse presses (the ticket)
- the same fix for the overlay's eight chrome widgets — the floating bar
  had it too, and there it threw away the selection you had just dragged
- a refused Window mode now reaches the chooser, not just the chip
- `verify_bug.py` deleted

Four tests fail on Linux and failed before this branch: three in
`TestCreateShortcut` need `ctypes.WINFUNCTYPE` and can only run on Windows,
one asserts a font metric this box does not reproduce. Neither is a
regression; both are worth a marker eventually.

Then **rebuild `dist\snipux.exe`** — the prebuilt one predates SNX-108, and
PyInstaller cannot cross-compile, so it has to happen on Windows:

    powershell -File packaging\windows\build.ps1

## Both of those are built now

On the same branch, after the write-up below turned out to be short work.

**The top bar.** `Platform.reserved_top(screen)` is the new seam: a
portable default off `QScreen`, overridden on Linux to read `_NET_WORKAREA`
under X11, where Qt reports no strut at all. The chooser and the close
button both clear it. Chrome placement only — the capture is untouched.

**The menu.** Instant / Edit / Review, in place of Review / Copy / Save.
`instant` is the new path and hangs off `_commit_selection`, a funnel for
the four routes a selection arrives by. Stored `clip`/`file` read back as
`edit`; the default is still `edit`.

Two things found on the way, both dead settings promising behaviour that
never existed:

- `clip` vs `file` made no difference to anything. Nothing but `app.py:902`
  ever read the value, and it only asked whether it said `review`. Folded
  into `edit` by this branch.
- **"Always copy to clipboard too" still does nothing.** `load_always_copy`
  is written by Settings and read by nobody. Left alone here — wiring it is
  its own decision, not a rename.

Still open on the menu: **instant always copies.** Instant-save has no way
to be asked for, because there is no copy-vs-save preference to read — the
switch above is the obvious place to put one, once it does anything.

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

A green suite here is weak evidence. All 1,211 tests run headless — no
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

**The Linux side has now been run** — once, on X11, after the platform seam
moved Linux code behind an interface. It took one launch to surface the top
bar hiding the chooser, which the whole headless suite had nothing to say
about. Wayland has not been run since the port at all, and that is the
primary session type.
