# snipux — conventions

A Windows Snipping Tool workalike, cross-platform. Snip an area, window,
freehand shape or the whole screen; annotate it; copy or save it. Or record
the same selection to video and trim it in the player.

## The one architectural rule

**Capture the entire virtual desktop in a single shot, then run selection
against that frozen frame in our own overlay.** The compositor/OS is involved
for exactly one instant. This is not negotiable, and it gets stronger with
every platform added, not weaker: a design that asks the OS for pixels while
the user is dragging is inherently platform-specific at the one moment that
matters, because every OS's live-interaction APIs differ. Everything
*downstream* of the grab — selection, chrome, annotation — is ordinary
drawing on an image already held in memory, and ordinary Qt runs unchanged
wherever PyQt6 does. That split is what let the same overlay/marks/shapes
code behave identically on X11 and Wayland, and it is exactly what makes
Windows and macOS tractable as ports rather than rewrites: it is the reason
the Windows port (SNX-85/86) touches roughly 210 lines rather than the whole
codebase. A capture backend that grabs pixels mid-drag instead of up front
would spread that platform dependency into every module downstream of it,
on every OS, forever.

## Target platform

Linux, Windows and macOS, aimed at full feature parity across all three.

- **Linux** (Ubuntu 22.04+, GNOME; other desktops expected to work but not
  what we test against) is implemented today. Wayland is the primary session
  type and X11 must also work; the session type is detected at runtime,
  never assumed.
- **Windows** (10 2004+ / 11) is implemented and has been driven for real
  (SNX-85/86 and after). `snipux/platform/windows.py` is ~985 lines of
  real implementation: desktop integration through COM `IShellLinkW`
  shortcuts, `RegisterHotKey` for the global hotkey, and the
  `QScreenCapture` capture/recording registries. `default_save_folder` is
  the one operation still raising `UnimplementedPlatformError`. What has
  *not* been watched there is listed in TODO.md, "Windows: four jobs" —
  treat that list as current, not the code's apparent completeness.
- **macOS** is next. The seam (`snipux/platform/darwin.py`) exists and
  nothing behind it is implemented; it needs a real Mac for the Screen
  Recording and Accessibility permissions.

Development happens on Windows and in an Ubuntu VM. Qt behaves the same on
all three — everything except the platform seam below is ordinary, portable
PyQt6, so keep platform-specific code confined to the `platform/` package
(see Layout).

## Layout

```
snipux/
  platform/     the platform seam: an ABC (`Platform`) plus one implementation
                per OS -- linux.py and windows.py are real; darwin.py
                raises UnimplementedPlatformError, naming both platform and
                operation, for everything not built yet. Desktop
                integration, global-shortcut (re)binding, the default save
                folder, whether the recorder can carry audio, and which
                capture and recording backends this OS can even try all
                get decided here. This is the one place `sys.platform` is
                read and the one seam to fill in for a new OS -- see its
                module docstring.
  capture.py    the Frame type, the CaptureBackend/BackendRegistry
                abstraction, and the concrete backends a platform's
                build_capture_registry() chooses between (Linux's
                X11/Wayland ones, and Windows')
  recording.py  the recording twin of capture.py: RecordingBackend /
                RecorderRegistry, plus GnomeScreencastBackend (D-Bus, the
                only Linux route -- WebM, no audio) and
                WindowsRecorderBackend (QScreenCapture -> QMediaRecorder)
  overlay.py    the frozen-frame overlay: selection, chrome, annotation in place
                (its pre-snip chooser diverges from the handoff -- see
                 docs/design/pre-snip-chooser.md)
  chooser.py    the docked pre-snip chooser row: what to capture, and what
                happens to it afterwards
  flowbars.py   the post-selection bars from the locked capture-flow handoff
                (docs/design/flow/, and its divergences.md). Views only --
                they report clicks and app.py decides what they mean.
  shapes.py     annotation data model and the flattening renderer
  settings.py   the Settings window (Qt in front of setup_desktop.py)
  review.py     the optional post-capture review window; Annotate reuses the bar
  player.py     the recording player/trim editor -- the `Open` destination for
                a recording (docs/design/player/, and its divergences.md for
                what this Qt build cannot encode)
  marks.py      the ink layer + undo/redo, shared by the overlay and review
  winchrome.py  frameless title bar/footer/controls for the two ordinary windows
  setup_desktop.py  Linux desktop/autostart entries, icons, the GNOME
                shortcut, config -- what platform/linux.py adapts
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

This holds on every platform snipux supports, not just Linux: as Windows and
macOS gain real implementations behind the `platform/` seam, their tests must
pass headless too, the same way `tests/test_platform.py` already runs against
`windows.py`/`darwin.py`'s stubs without a display today.

## Conventions

- **Python 3.10+, PyQt6.** Qt6 enums are fully scoped: `Qt.PenStyle.DashLine`,
  never `Qt.DashLine`.
- **Dependencies are PyQt6, jeepney and pytest.** Adding a fourth is a decision
  worth raising in the ticket, not a detail. `ffmpeg` is the one *optional*
  exception and is not a dependency: `player.system_ffmpeg()` uses one if the
  system already has it, because Qt's bundled FFmpeg cannot encode H.264 in
  software, and every export works without it one codec down. Never import it,
  never require it, never install it. Notably: no numpy, no Pillow, no
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
