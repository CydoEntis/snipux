# Next: use it, and find what the tests still cannot see

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-72** is merged (PRs #6–#19). `main` runs 648 tests.
The overlay redesign is in and the separate editor window is gone.

Spec: `docs/design/overlay-redesign.md` (open
`docs/design/Snipux Overlay.dc.html` in Chrome — it is interactive and is the
behavioural authority). Tokens: `snipux/design/tokens.py`.

## What to do next

    cd ~/snipux && git pull
    bash packaging/install.sh

The installer starts snipux itself and writes an autostart entry, so the
shortcut works immediately and after a reboot.

**Then use it for ten minutes.** Everything below is unproven until you do.

## The lesson this project has already taught, twice

**A green test suite here is weak evidence.** All 648 tests run under
`QT_QPA_PLATFORM=offscreen`, which has no compositor, no window manager and no
keyboard. That suite was green while the app shipped a package that could not
import, an overlay with no way to make a selection, invisible blur, and a
toolbar clipped to single letters. Every one was found by running it.

Two specific traps, both of which bit:

- **Do not seed state the app never sets.** A check that calls
  `set_selection()` before testing drawing proves drawing works *given* a
  selection, while nothing on the real path creates one. Start from what
  `app.py` actually constructs. `AppController` is the entry point worth
  driving.
- **Grep for "later ticket" after any big change.** Three real bugs came from
  agents deferring work in a comment where no later ticket existed.

## Deviations from the handoff — deliberate, do not revert

**Capture never uses `QScreen.grabWindow(0)`.** It returns black on Wayland,
which is why `capture.py` has a portal backend at all.

**Minimum selection is 16x16, not 200x140**, so a taskbar icon or one line of
text stays snippable.

**Text labels commit in two stages**, not on the click, so a stray click leaves
no empty chip.

**Eleven tools, not the handoff's eight.** Ellipse, Line and Crop live in a
popover off the rect button — the bar still shows eight.

**The hint HUD is off by default.** It read as a stray banner over every
capture. Shortcuts stay discoverable through tooltips.

## Open questions for the next session

**Does the shortcut work now?** The last failure was the portal refusing a
screenshot to a freshly spawned process; SNX-67 asks for a non-modal dialog,
which GNOME should grant without a parent window. Unverified on hardware. If it
still fails, the tray now reports why — read the message, it names the backend
and the response code.

**Only one capture backend exists on GNOME.** `grim` is wlroots-only, so the
portal is it. One refusal and there is no fallback. Worth deciding whether to
add one.

## Known, unticketed

**`snipux/overlay.py` is over 5,000 lines** — the window plus every chrome
widget. Splitting the chrome into `snipux/chrome.py` is the obvious next cut,
and it gets harder with every feature.

IBM Plex is not vendored, so the layout renders in a fallback family it was not
tuned for. Drop the OFL `.ttf` files into `snipux/design/fonts/` — the loader
already looks there and the packaging already ships them.

## What has never been tested

Anything on a machine that is not the VirtualBox VM: **multiple monitors,
fractional scaling, and X11**.

**Freeform, Window, Full screen and the capture delay** have never run against
a live compositor — only synthetic frames. Region is the trusted path.
