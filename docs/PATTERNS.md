# Patterns

snipux has no server and no database, so it has no API layer. What it does
have is a handful of patterns that every feature goes through. Follow them
rather than inventing new ones.

## A user action, end to end

```text
key press / click
  -> view (chooser.py, overlay.py, flowbars.py, ...)   emits a signal
    -> controller (app.py)                             decides what it means
      -> model (shapes.py, marks.py)                   flattens the annotation
      -> backend (capture.py, recording.py)            gets pixels / a file
      -> platform seam (platform/)                     anything OS-specific
      -> destination                                   clipboard, file, pin, player
```

A view never writes a file, touches the clipboard, or checks the OS. It says
"Copy was clicked", and `app.py` does the copying.

## Adding a feature

1. **Where does it live?** A new tool is a shape in `shapes.py` plus its
   button in `flowbars.py`. A new destination is a handler in `app.py`. A new
   OS behaviour is a method on `Platform`.
2. **Does it need the OS?** Add it to the `Platform` ABC, implement it in
   `linux.py` and `windows.py`, and have `darwin.py` raise
   `UnimplementedPlatformError`. If one OS can't do it, the view greys the
   control *and says why*, instead of hiding it.
3. **Does it need a setting?** Add a `load_<key>`/`save_<key>` pair in
   `setup_desktop.py` (see `docs/STORAGE.md`) and a row in `settings.py`.
4. **Test it headless**, faking the platform where it matters.
5. **Changelog line**, written for someone who uses the tool.

## Backends and registries

```python
class CaptureBackend(ABC):
    def name(self) -> str: ...
    def is_available(self) -> bool: ...
    def unavailable_reason(self) -> str | None: ...
    def capture(self) -> Frame: ...
```

A `BackendRegistry` holds backends in preference order. `capture()` tries
each available backend, collects each failure, and raises one `CaptureError`
listing all of them only when none worked. `RecordingBackend`/`RecorderRegistry` in
`recording.py` follow the same shape.

To add a backend: subclass, implement `is_available()` cheaply (no pixels, no
dialogs), and add it to the right place in the platform's
`build_capture_registry()`/`build_recording_registry()`.

## Platform operations

```python
class Platform(ABC):
    def bind_shortcut(self, shortcut: str | None = None) -> str: ...
    def build_capture_registry(self) -> BackendRegistry: ...
    def can_pin(self) -> bool: ...
    def pin_unavailable_reason(self) -> str: ...
    ...

current: Platform = _select()   # chosen once, at import
```

Callers use `platform.current.<operation>()` and never branch on the OS
themselves. Capability questions come in pairs -- `can_pin()` with
`pin_unavailable_reason()`, `recognizes_text()` with
`text_recognition_unavailable_reason()` -- so a greyed control can say why. An
operation an OS cannot do yet raises `UnimplementedPlatformError(platform_name,
operation)` so the message says exactly what is missing.

## Settings

Settings are plain functions, not an object:

```python
def load_recording_frame_rate(config_dir: Path | None = None) -> int: ...
def save_recording_frame_rate(frame_rate: int, config_dir: Path | None = None) -> bool: ...
```

`load_*` validates and falls back to a default on anything unexpected.
`save_*` returns `False` rather than raising. `config_dir` exists so tests
point at a temp directory and never at the real one.

## Single instance

`handoff.py` is the console entry point. For a lone `--snip` or `--settings`
it connects to the running snipux over a `QLocalSocket` and forwards the
request **without importing the app**, which is what makes the shortcut
fast. Anything else goes on to `app.py`'s CLI. If no instance answers, the
new process becomes the server.

## Graceful absence

Optional things are found, not required:

- `ffmpeg` on `PATH` gives H.264 export; without it, export drops one codec.
- PowerShell + an OCR language gives Copy text; without it, the button is
  greyed with the reason.
- `grim`, `scrot`, `import`, `xprop`, `wl-copy` are each one backend or
  helper among several.

A missing optional tool is never an exception and never a crash.

## Async and long work

Recording, export and OCR take time. Keep them off the paint path, show that
something is happening, and when it fails, tell the user what to try next --
the same wording standard as `CHANGELOG.md`.

## Testing

- Views: `QWidget.grab()` and synthetic input, headless.
- Controller: drive `app.py`'s real objects with the platform and backends
  faked.
- Settings: a `tmp_path` as `config_dir`, including a corrupt file.
- Backends: fake the subprocess/D-Bus/Win32 call, and remember that a mocked
  D-Bus once hid four real faults -- verify the real path on a real session
  before calling it done.
