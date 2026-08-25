# Next: try the redesign on real hardware

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

The overlay redesign is **built and merged** — SNX-30 through SNX-52, four
pull requests (#11–#14). It has never been run on a real screen.

Spec: `docs/design/overlay-redesign.md` (open
`docs/design/Snipux Overlay.dc.html` in Chrome — it is interactive and is the
behavioural authority). Tokens: `snipux/design/tokens.py`.

## What to do next

Install on the VM and use it for ten minutes:

    bash packaging/install.sh
    snipux            # or Super+Shift+S

Everything below the line is a guess until that happens. All 540 tests run
with `QT_QPA_PLATFORM=offscreen`, which paints without a compositor — so
nothing in this repo has yet proven the overlay looks right, sits at the
right size, or lands on top of other windows on a live Wayland session.

## Four deviations from the handoff — deliberate, do not revert

**Capture never uses `QScreen.grabWindow(0)`.** The spec says to; it returns
black on Wayland, which is the entire reason `capture.py` has a portal backend
and a gnome-shell fallback. The frame comes from `BackendRegistry.capture()`.

**Minimum selection is 16x16, not 200x140.** The spec's minimum exists to give
its chrome room, but it would make a taskbar icon or a single line of text
unsnippable, and those are core uses. The bar and chips are positioned relative
to the selection, so a small selection is only a layout problem, not a broken
one.

**Text labels commit in two stages, not on the click.** The spec says a click
drops a label. A click opens a focused editor; the mark lands when the text is
finished, and an empty label is discarded. Committing on the click alone would
leave an empty chip behind every stray click.

**The bar reaches eleven tools, not eight (SNX-64).** The spec drops Ellipse,
Line and Crop and says not to add controls to the bar for them. The owner
asked, before the redesign started, to keep all eleven — he'd tried them and
they worked — which outranks the handoff here. The bar still shows eight
buttons; rect's own button opens a small popover (`ShapeToolPopover`) that
reaches the other three, per the handoff's own "new tools belong in a
submenu" guidance for exactly this situation.

## Known, unticketed

**`overlay.py` is 4,475 lines.** It is one coherent widget tree — the window,
the bar, both trays, the popover, the HUD, the toast and every custom button —
but it is now by far the biggest file here, and this codebase is meant to stay
readable. Splitting the chrome widgets into `snipux/chrome.py` is the obvious
first cut, and worth doing before the next feature lands on top of it.

**Esc is two-stage** — first press discards ink, second closes the overlay with
no capture. That was a decision the spec left open. It reads well in a test and
may feel wrong in the hand; it is a one-line change either way.

IBM Plex is not vendored, so the app falls back to a system sans and mono. Drop
the OFL `.ttf` files into `snipux/design/fonts/` to get the intended type — the
loader already looks for them.

`BackendRegistry.capture()` reports an empty failure list as
`no capture backend is available`, which was written before anyone had seen it
on a bare machine.

## What has never been tested

Anything on a machine that is not the VirtualBox VM: **multiple monitors,
fractional scaling, and X11**. The work box is the first real test of all three.

Freeform, Window, Full screen and the capture delay have never been run against
a live compositor either — only against a synthetic frame.
