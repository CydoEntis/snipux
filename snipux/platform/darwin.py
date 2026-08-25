"""The macOS `Platform`: not implemented yet.

Windows gets full parity first (SNX-85); macOS is later. Every operation
here raises `UnimplementedPlatformError` naming itself and "macOS", rather
than silently doing nothing or pretending Linux's `.desktop`/gsettings
mechanism means anything here. Filling this in for real means giving each
method a macOS-native implementation (a `.app` bundle/Login Item in place of
a `.desktop`/autostart file, a system-level hotkey registration in place of
a GNOME custom keybinding, `~/Pictures` -- macOS's own, not Linux's XDG
one -- in place of `~/Pictures` via `XDG_DATA_HOME`) -- against exactly the
interface `snipux/platform/__init__.py` defines, with no other module
needing to change.
"""

from __future__ import annotations

from pathlib import Path

from . import Platform, UnimplementedPlatformError

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
