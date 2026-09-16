# Architecture

How snipux is put together, and which way the dependencies are allowed to
point. `CLAUDE.md` has the short version; this is the long one.

## Goals

- One app, the same behaviour on Linux (Wayland and X11), Windows, and later
  macOS, from one PyQt6 codebase.
- Everything platform-specific in one package, `snipux/platform/`, so a new
  OS is a port and not a rewrite.
- Local only. No network, no accounts, no telemetry.
- Three runtime dependencies: PyQt6, jeepney, and the standard library.

## The one rule

**Grab the whole virtual desktop once, then do everything else on that frozen
image in our own window.** The OS is asked for pixels at exactly one moment.
Selection, the bars, annotation and export are ordinary Qt drawing on an image
already in memory, so they behave the same everywhere.

Recording follows it too: the frozen frame is how you choose the region, and
the recorder starts only once you have chosen.

The one sanctioned exception is full-page browser capture, which has to scroll
and grab more than once (`docs/design/browser-capture.md`).

## Layers

```text
entry        handoff.py  ->  app.py (CLI, tray, controller)
               |
views        chooser.py  overlay.py  flowbars.py  review.py  player.py
             pin.py  settings.py  winchrome.py  glass.py
               |
model        shapes.py  marks.py  sensitive.py  textsnap.py
               |
backends     capture.py  recording.py
               |
seam         platform/  (linux.py, windows.py, windows_ocr.py, darwin.py)
               |
OS           D-Bus portals, GNOME Shell, X11 tools, Win32, WinRT, QtMultimedia
```

Dependencies point **down**. A view may use the model and the backends; the
model may not know about views; nothing below `app.py` should import it.

| Layer     | Owns                                                               | Must not                                            |
| --------- | ------------------------------------------------------------------ | --------------------------------------------------- |
| entry     | parsing the command line, the tray, the single-instance socket, deciding what a click *means* | draw anything beyond the tray                       |
| views     | painting and input; reporting what the user did                    | decide destinations, touch the filesystem directly, read `sys.platform` |
| model     | annotation data, undo/redo, text finding, flattening to an image   | import a view or `app.py`                           |
| backends  | turning "give me a frame / start recording" into pixels or a file  | assume one frame per session; stop at the first failing backend |
| seam      | every question whose answer depends on the OS                      | be bypassed                                         |

### Views report, the controller decides

`flowbars.py` and `chooser.py` emit what was clicked; `app.py` decides whether
that is Copy, Save, Open, Pin or Record, and what happens afterwards. Keep it
that way: a destination decided inside a view is a destination that cannot be
changed from Settings.

### Backends and registries

`capture.py` defines `Frame`, `CaptureBackend` and `BackendRegistry`.
`recording.py` mirrors it with `RecordingBackend` and `RecorderRegistry`. A
registry tries backends **in order**, collects every failure, and reports them
together if none works. One broken backend never hides the next.

Which backends a platform may even try is decided by
`platform.current.build_capture_registry()` / `build_recording_registry()`, not by the
backends themselves.

### The platform seam

`snipux/platform/__init__.py` defines the `Platform` ABC and picks the
implementation. `linux.py` adapts `setup_desktop.py`; `windows.py` is a full
implementation (COM `IShellLinkW` shortcuts, `RegisterHotKey`, capture
exclusion, QtMultimedia recording); `darwin.py` raises
`UnimplementedPlatformError` naming the platform and the operation.

A new OS should need changes only here. If it needs one anywhere else, that
is worth raising in the issue: it usually means something downstream of the
frozen frame has picked up a platform assumption.

## Where code lives

```text
snipux/
  app.py            controller, tray, CLI, clipboard/save helpers, socket
  handoff.py        forwards --snip/--settings to a running snipux without Qt
  capture.py        Frame, capture backends and their registry
  recording.py      recording backends and their registry
  overlay.py        the frozen-frame overlay: selection and in-place annotation
  chooser.py        the pre-snip row
  flowbars.py       the post-selection bars (views only)
  shapes.py         annotation data model and flattening renderer
  marks.py          ink layer and undo/redo, shared by overlay and review
  sensitive.py      finding things to hide (passwords, keys) in recognised text
  textsnap.py       snapping the highlighter to lines of text
  review.py         the optional post-capture window
  player.py         recording player, trim and export
  pin.py            a snip pinned on top of the screen
  settings.py       the Settings window
  setup_desktop.py  config.json, desktop entries, icons, the GNOME shortcut
  glass.py          cached blur behind bars and menus
  winchrome.py      frameless title bar and controls
  design/           icons, logo, fonts, design tokens
  platform/         the seam (see above)
tests/              pytest, one file per module
docs/               standards, design handoffs and their divergences
packaging/          installers and the Windows exe build
```

## Known drift from these rules

Recorded here so nobody copies it, and so fixing it is a known, bounded job:

- `capture.py` and `recording.py` read `sys.platform` directly in a few places
  (backend `is_available()` checks and default selection). The rule says only
  `platform/` should.
- `overlay.py` and `pin.py` import `copy_*_to_clipboard` and `save_image` from
  `app.py`, inside functions to dodge the import cycle. Those helpers belong
  in a small module below the views.
- `shapes.py` imports `capture.py` for the `Frame` type.

New code should not add to this list.

## Security and privacy

- **No network.** The only socket is a local `QLocalServer` used so a second
  `snipux --snip` hands its request to the running one. `snipux --update`
  runs `pip`, and only when the user asks.
- Pixels leave memory only when the user chooses Copy, Save, Pin or Record.
- OCR (Copy text, Hide sensitive) runs on the device. On Windows the image
  goes to a temp file that PowerShell reads through `Windows.Media.Ocr`, and
  is deleted afterwards.
- Screenshots and recordings are user content: never log them, never put them
  in test fixtures or bug reports without the user's say-so.
- See `docs/PLATFORM-CAPABILITIES.md` for every OS facility snipux touches.
