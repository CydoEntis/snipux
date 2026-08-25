"""The Windows `Platform`: desktop integration not implemented yet, capture
is (SNX-88).

snipux is going cross-platform with Windows as the first target for full
parity (SNX-85), but the desktop-integration side of that hasn't landed --
every one of those operations here still raises `UnimplementedPlatformError`
naming itself and "Windows", rather than silently doing nothing or
pretending Linux's `.desktop`/gsettings mechanism means anything here.
Filling that in for real means giving each method a Windows-native
implementation (a Start Menu shortcut and Run-key entry in place of a
`.desktop`/autostart file, `RegisterHotKey` in place of a GNOME custom
keybinding, `%USERPROFILE%\\Pictures` in place of `~/Pictures`) -- against
exactly the interface `snipux/platform/__init__.py` defines, with no other
module needing to change.

`build_capture_registry()` (SNX-86/88) is the exception: it forwards to
`capture.build_windows_registry()`, the same way `LinuxPlatform` forwards to
`capture.build_linux_registry()` -- CLAUDE.md's one architectural rule (grab
the whole virtual desktop in one shot, then let the existing overlay run
selection against that frozen frame) is what makes capture itself no
different here than on Linux; only *how* the grab happens changes per
platform, and that logic lives in `capture.py` alongside the backends it
chooses between, not duplicated in this module. See `capture.py` for the
qt-native/Win32-GDI backends themselves.
"""

from __future__ import annotations

from pathlib import Path

from snipux import capture
from snipux.capture import BackendRegistry

from . import Platform, UnimplementedPlatformError

_PLATFORM_NAME = "Windows"


class WindowsPlatform(Platform):
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
        return capture.build_windows_registry()
