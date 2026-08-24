# Next: the overlay redesign

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Spec: `docs/design/overlay-redesign.md` (open
`docs/design/Snipux Overlay.dc.html` in Chrome — it is interactive and is the
behavioural authority). Tokens: `snipux/design/tokens.py`.

## Resume with

    punch work SNX-30

Five tickets, SNX-30..SNX-34, one integration branch, worked in order.

## Two deviations from the handoff — deliberate, do not revert

**Capture never uses `QScreen.grabWindow(0)`.** The spec says to; it returns
black on Wayland, which is the entire reason `capture.py` has a portal backend
and a gnome-shell fallback. The frame comes from `BackendRegistry.capture()`.

**Minimum selection is 16x16, not 200x140.** The spec's minimum exists to give
its chrome room, but it would make a taskbar icon or a single line of text
unsnippable, and those are core uses. The bar and chips are positioned relative
to the selection, so a small selection is only a layout problem, not a broken
one.

## Still to write — 13 tickets, not yet in Linear

**PR 2 — tools & ink** (5)
Pen/highlighter/arrow/rect to the design's geometry (highlighter x3.5 at 34%
alpha; arrow head maths) · step badges and text labels · blur and pixelate with
strength, destructive on export · eraser hit-testing, topmost mark wins ·
undo/redo/clear semantics plus the copy/save pipeline to `~/Pictures/snipux`.

**PR 3 — chrome & modes** (8)
Floating bar · settings tray, visible only while a drawing tool is held · blur
tray variant · dimension chip and Frozen pill · capture-mode popover and delay ·
toasts · top hint HUD · keyboard shortcuts with suppression while text or a
slider has focus. Then Window, Full-screen and Freeform modes.

## Known, unticketed

`BackendRegistry.capture()` reports an empty failure list as
`no capture backend is available` — good — but the wording was written before
anyone had seen it on a bare machine. Worth re-reading after the redesign.

IBM Plex is not vendored; SNX-30 falls back to a system family. Drop the OFL
`.ttf` files into `snipux/design/fonts/` to get the intended type.

## What has never been tested

Anything on a machine that is not the VirtualBox VM. In particular: multiple
monitors, fractional scaling, and X11. The work box on Monday is the first
real test of all three.
