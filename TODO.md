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

## Next up — written down, not ticketed

Both came out of actually running the thing on Linux.

### The GNOME top bar hides the chooser

The panel hangs flush from the monitor's top edge and the shell's top bar is
drawn straight over it. More than half of the 54px panel is gone. The armed
tab is 26px, so it disappears completely.

Measured on the dev box (Ubuntu GNOME, **X11**, three monitors):

    _NET_WORKAREA                0, 32, 6400, 1337   <- 32px reserved, top
    QScreen.geometry()           1920,0 2560x1440
    QScreen.availableGeometry()  1920,0 2560x1440    <- identical: no strut

**`availableGeometry()` is not the fix.** Qt reports it equal to
`geometry()` on all three monitors here, so the obvious one-liner in
`_active_screen_rect()` changes nothing. `_NET_WORKAREA` holds the real
number, is X11-only, and is one rect for the entire virtual desktop — it
cannot say which monitor the bar is on.

**Check first whether this is X11-only.** On Wayland `show_on_screen`
fullscreens the overlay onto one output and GNOME hides its top bar for
fullscreen windows; on X11 the overlay is a plain always-on-top window sized
to the whole virtual desktop, which the shell is happy to paint over. If it
only bites on X11 the problem shrinks a lot.

Shapes worth weighing, none chosen:

- a top inset from the platform seam — `platform/linux.py` reads
  `_NET_WORKAREA` under X11 and returns 0 elsewhere — with the chooser
  hanging from `top + inset`
- stop hanging flush when something else owns the edge: float the panel
  just below it
- make the X11 overlay genuinely fullscreen so the shell hides its own bar

Whatever wins, only chrome placement moves. The capture still covers the
whole virtual desktop in one shot, per the one architectural rule.

`_Tab`'s docstring claims the 26px it occupies "on GNOME is the top bar's
territory anyway, so in practice it costs nothing" — that is exactly
backwards, and belongs in the same change.

### The "then" menu should ask about editing, not destination

Today it offers three destinations — Review / Copy / Save (`review`, `clip`,
`file` in `tokens.AFTER_CAPTURE`). Wanted instead: **instant capture**,
**capture + edit**, **capture and open the GUI to edit**.

That is a different axis. The menu currently answers *where the shot goes*;
the ask is *where you edit it*. Against what exists:

- **capture + edit** — today's default. The overlay always lets you annotate
  in place, and you press Copy or Save to finish.
- **capture and open the GUI** — today's `review`, which opens `review.py`'s
  window once the overlay closes.
- **instant capture** — **does not exist.** Every snip lands in the
  annotate-capable overlay and waits. `clip` and `file` only decide whether a
  review window opens afterwards; `app.py:902` is the only reader of
  `outcome` in the codebase.

So the real new work is the instant path: selection released, straight to the
destination, overlay gone.

The open question is that destination is orthogonal to flow, so where does
"copy vs save" go? Cheapest coherent answer: only the instant flow needs it
settled up front, because the other two end with the user pressing Copy or
Save themselves. Instant takes the Settings default, and the panel keeps
three triggers instead of growing a fourth.

Touches `tokens.AFTER_CAPTURE` and `CHOOSER_AFTER_NOTE` (shared ids, two
lengths of prose), the Settings radio cards, `setup_desktop`'s
`load_review_window`/`save_review_window`, and needs a migration for configs
already holding `review`/`clip`/`file`.

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
