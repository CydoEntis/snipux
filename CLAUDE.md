# snipux — conventions

A Windows Snipping Tool workalike for Linux. Snip an area, window, freehand
shape or the whole screen; annotate it; copy or save it.

## The one architectural rule

**Capture the entire virtual desktop in a single shot, then run selection
against that frozen frame in our own overlay.** The compositor is involved for
exactly one instant. This is what lets the same code path behave identically on
X11 and Wayland, and it is not negotiable — a design that asks the compositor
for pixels while the user is dragging will work on X11 and fail on Wayland.

Everything downstream of that grab is ordinary drawing on an image we already
hold in memory.

## Target platform

Ubuntu 22.04+, GNOME, **Wayland is the primary target** and X11 must also work.
The session type is detected at runtime, never assumed. Other desktops are
expected to work but are not what we test against.

Development happens on Windows and in an Ubuntu VM. Qt behaves the same on all
three for everything except capture, so keep platform-specific code confined to
the capture backends.

## Layout

```
snipux/
  capture.py    backends + virtual-desktop frame; the only platform-specific code
  overlay.py    the frozen-frame overlay: selection, chrome, annotation in place
                (its pre-snip chooser diverges from the handoff -- see
                 docs/design/pre-snip-chooser.md)
  shapes.py     annotation data model and the flattening renderer
  settings.py   the Settings window (Qt in front of setup_desktop.py)
  review.py     the optional post-capture review window; Annotate reuses the bar
  marks.py      the ink layer + undo/redo, shared by the overlay and review
  winchrome.py  frameless title bar/footer/controls for the two ordinary windows
  setup_desktop.py  desktop/autostart entries, icons, the GNOME shortcut, config
  app.py        controller, tray, CLI
tests/          pytest, mirroring the module names
```

## Commands

```sh
python -m pip install -r requirements.txt
QT_QPA_PLATFORM=offscreen python -m pytest -q     # the verify step runs this
python -m snipux                                  # run it
```

Tests must pass with `QT_QPA_PLATFORM=offscreen` — a build machine has no
display. Widgets can still be exercised there: `QWidget.grab()` runs a full
`paintEvent` into an offscreen pixmap without showing anything, and that is the
preferred way to test painting code.

## Conventions

- **Python 3.10+, PyQt6.** Qt6 enums are fully scoped: `Qt.PenStyle.DashLine`,
  never `Qt.DashLine`.
- **Dependencies are PyQt6, jeepney and pytest.** Adding a fourth is a decision
  worth raising in the ticket, not a detail. Notably: no numpy, no Pillow, no
  OpenCV — Qt already does image work, and a screenshot tool that drags in a
  numerical stack has made a bad trade.
- **Comments say why, not what.** A comment restating the line above it is
  noise; a comment explaining a compositor quirk or a non-obvious ordering
  constraint is the reason the file is maintainable.
- **Coordinates are the sharp edge here.** Be explicit about which space a value
  is in — logical vs physical pixels, screen-local vs virtual-desktop — and say
  so in the name or in a comment. Most bugs in a tool like this are a value used
  in the wrong space, and fractional display scaling makes them invisible on a
  developer's machine.
- **A capture backend that fails must not stop the next one.** Backends are
  tried in order and each failure is collected and reported together.
- **Never leave a QPainter open across a read of the pixmap it is painting.**
  Reading a pixmap mid-paint is not guaranteed to see pending strokes; the
  obscuring tools depend on this and it has already caused one bug.

## Out of scope for now

Scrolling / full-page capture is a later milestone. Do not build toward it
speculatively, but do not make it impossible either — the capture layer should
not assume one frame per session.
