"""The macOS `Platform`: not implemented yet.

Windows gets full parity first (SNX-85); macOS is later. Every
desktop-integration operation here raises `UnimplementedPlatformError`
naming itself and "macOS", rather than silently doing nothing or pretending
Linux's `.desktop`/gsettings mechanism means anything here. Filling this in
for real means giving each method a macOS-native implementation (a `.app`
bundle/Login Item in place of a `.desktop`/autostart file, a system-level
hotkey registration in place of a GNOME custom keybinding, `~/Pictures` --
macOS's own, not Linux's XDG one -- in place of `~/Pictures` via
`XDG_DATA_HOME`) -- against exactly the interface
`snipux/platform/__init__.py` defines, with no other module needing to
change.

`build_capture_registry()` (SNX-86) does not raise, unlike the rest of this
class -- see `windows.py`'s docstring for why: `--list-backends` and
`app.build_default_registry()` both need a real, always-answering registry,
not an exception. It answers with a registry containing one
`capture.UnsupportedPlatformBackend`, naming this platform. No real backend
is constructed here -- that is later work, not this ticket's.

`build_recording_registry()` (SNX-119) is not an exception the way
`build_capture_registry()` is: it raises `UnimplementedPlatformError`
naming itself and the operation, same as every other method above.
Recording has no `--list-backends`-style caller yet that needs a real,
always-answering registry, and `recording.py` has no
`UnsupportedPlatformBackend` of its own to hand back one that says so --
see `Platform.build_recording_registry()`'s own docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from snipux.capture import BackendRegistry, UnsupportedPlatformBackend

from . import Platform, UnimplementedPlatformError

if TYPE_CHECKING:
    from snipux.recording import RecorderRegistry

_PLATFORM_NAME = "macOS"


class DarwinPlatform(Platform):
    def install_desktop_integration(self, *, shortcut: str | None = None) -> int:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "install_desktop_integration")

    def remove_desktop_integration(self) -> int:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "remove_desktop_integration")

    def bind_shortcut(self, shortcut: str | None = None) -> str:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "bind_shortcut")

    def unbind_shortcut(self) -> str:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "unbind_shortcut")

    def default_save_folder(self) -> Path:
        raise UnimplementedPlatformError(_PLATFORM_NAME, "default_save_folder")

    def build_capture_registry(self) -> BackendRegistry:
        registry = BackendRegistry()
        registry.add(UnsupportedPlatformBackend(_PLATFORM_NAME))
        return registry

    def build_recording_registry(self) -> "RecorderRegistry":
        raise UnimplementedPlatformError(_PLATFORM_NAME, "build_recording_registry")
