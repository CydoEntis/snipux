# snipux — specification, milestone 1

## What this is

A Windows Snipping Tool workalike for Linux, written in Python with PyQt6.

The user runs Ubuntu at work and previously had the Windows Snipping Tool. The
three things they lost, in their words, are: snip an area, snip the full screen,
and paint over the snip. No Linux tool they tried replaced that workflow well
enough, and the gap roughly doubles the effort of their daily work.

Milestone 1 is a **daily driver**: press a key, drag a box, draw on it, copy or
save. If it does not replace the Windows workflow for someone who used it every
day, milestone 1 is not done.

## Target platform

- **Ubuntu 22.04+ with GNOME.** Wayland is the primary target; X11 must also
  work. The session type is detected at runtime and never assumed.
- Other desktops (KDE, Sway, Hyprland, XFCE) should work where the capture
  backends allow, but are not what correctness is judged against.
- Development happens on Windows and in an Ubuntu VM. Qt behaves identically on
  all three for everything except capture.

## The architectural constraint that shapes everything

**Capture the entire virtual desktop in a single shot, then run selection
against that frozen frame in the application's own overlay.**

Wayland deliberately forbids clients from reading the screen at will. A design
that asks the compositor for pixels while the user drags a selection box will
work on X11 and fail on Wayland. Grabbing once, up front, involves the
compositor for a single instant and makes the rest of the application ordinary
drawing on an image already held in memory.

This is not an optimisation. It is the reason the tool can work at all on the
primary target, and no ticket may design around it.

## Scope of milestone 1

Twelve capabilities, agreed with the user. Items 1–6 are fully verifiable on a
machine with no Linux present; items 7–12 need a Linux session and are
verifiable in an Ubuntu VM.

1. **Project skeleton, capture-backend interface, runtime session detection.**
   A `Frame` type holding the captured virtual desktop plus the logical
   geometry it covers, able to crop a logical rectangle out of itself correctly
   under display scaling. A backend registry that reports which backends this
   session can actually use, and a `--list-backends` diagnostic.
2. **Annotation canvas.** Displays a captured image letterboxed and never
   upscaled past 100%, and maps widget coordinates to image pixels and back.
3. **Drawing tools:** pen, highlighter, arrow, line, rectangle, ellipse. Colour
   and stroke width are selectable.
4. **Redaction and markup:** text, numbered step markers, blur, pixelate, crop.
5. **Undo/redo history** covering every annotation and the crop.
6. **Save and clipboard.** Auto-copy on capture, matching the Windows tool. Save
   with a sensible default name and directory.
7. **Selection overlay.** One frameless fullscreen window per monitor painting
   its slice of the frozen frame, a dimmed veil outside the selection, a live
   size readout, and a zoomed magnifier with a pixel crosshair.
8. **Selection modes:** rectangle, freeform, window, full screen.
9. **Capture backend — X11.**
10. **Capture backend — Wayland.**
11. **Tray-resident background process**, so a hotkey reaches an already-warm
    application rather than paying process startup on every snip.
12. **Packaging:** desktop entry, install script, and instructions for binding
    the Print Screen key on GNOME.

## Requirements by area

### Capture

- Backends are tried in a session-appropriate order until one succeeds. A
  failing backend must never prevent the next from being tried; failures are
  collected and reported together so a user can see why everything failed.
- Wayland order: `grim` (wlroots), then xdg-desktop-portal, then
  desktop-environment helpers. X11 order: Qt's native grab first because it
  costs no process spawn, then `maim`, `import`, `scrot`.
- The portal backend talks D-Bus through `jeepney` and must subscribe to the
  response signal *before* issuing the request, or a fast portal replies into
  the void.
- Multi-monitor is a first-class case, including monitors positioned above or
  left of the primary, which produce a virtual desktop with a negative origin.
- Display scaling must be handled. Derive the ratio from the captured pixel size
  against the reported logical size rather than trusting the reported ratio;
  fractional scaling setups misreport it.

### Selection overlay

- Escape cancels, right-click cancels, Enter confirms, release confirms.
- A click with no meaningful drag is a misfire, not a selection of nothing.
- Selection is tracked in logical virtual-desktop coordinates so a drag spanning
  two monitors is arithmetic rather than a special case.
- The size readout must state what the crop will actually contain.
- Window mode needs per-window geometry, which X11 exposes and Wayland does not.
  Where it is unavailable the mode degrades to a rectangle drag rather than
  presenting a dead option.

### Annotation

- Annotations are kept as data and re-rendered onto a copy of the base image.
  This buys exact undo, non-destructive editing, and correct blur.
- **Blur and pixelate must obscure whatever is beneath them, including earlier
  annotations** — not merely the original screenshot pixels. A redaction that
  leaves an earlier label readable is a data leak, and this is the single most
  important correctness property in the annotation layer.
- Blur and pixelate must not read a pixmap while a painter is open on it;
  pending strokes are not guaranteed to be visible.
- Cropping flattens the annotations, because shape coordinates would otherwise
  point outside the new image. It must remain undoable.

### Clipboard

- The snip is copied automatically when the selection is confirmed.
- On Wayland the Qt clipboard is owned by the process and its contents die when
  the process exits. Where `wl-copy` is available it should be used so a copied
  snip survives closing the window. This is a real behaviour difference users
  hit immediately, not an edge case.

### Resident process and hotkey

- Process startup is roughly a third of a second, which is perceptible on every
  single snip. The application therefore runs resident with a tray icon and the
  hotkey reaches the warm process.
- Binding Print Screen is the desktop's job, not the application's. Ship the
  instructions; do not attempt to grab keys globally.

## Non-goals for milestone 1

- **Scrolling / full-page capture.** A later milestone. Do not build toward it
  speculatively, but do not make it impossible: the capture layer must not
  assume one frame per session.
- Video recording, OCR, and colour picking. The Windows tool has some of these;
  they are not what the user is missing.
- Cloud upload or sharing of any kind.

## Testing

- `pytest`, run as `QT_QPA_PLATFORM=offscreen python -m pytest -q`. A build
  machine has no display.
- Widget painting is testable offscreen: `QWidget.grab()` runs a full
  `paintEvent` into a pixmap without showing a window. Painting code should be
  tested this way rather than left uncovered.
- Coordinate mapping deserves round-trip tests. Most bugs in a tool like this
  are a value used in the wrong coordinate space, and fractional scaling makes
  them invisible on a single developer's machine.
- Backends that shell out to a binary should be tested with the binary absent,
  because "the tool is not installed" is the common real-world case.

## Dependencies

PyQt6, jeepney, pytest. Adding a fourth is a decision to raise in the ticket.
Specifically no numpy, no Pillow, no OpenCV — Qt already does image work, and a
screenshot tool that pulls in a numerical stack has made a bad trade.

## Definition of done for milestone 1

On an Ubuntu VM, from a cold boot: the tray icon is present, Print Screen
freezes the screen, a dragged rectangle opens in the editor with the image
already on the clipboard, an arrow and a blur can be drawn on it, and Ctrl+S
writes a PNG. On both a Wayland session and an X11 session.
