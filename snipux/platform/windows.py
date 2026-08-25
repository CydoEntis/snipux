"""The Windows `Platform`: not implemented yet.

snipux is going cross-platform with Windows as the first target for full
parity (SNX-85), but that work hasn't landed -- every operation here raises
`UnimplementedPlatformError` naming itself and "Windows", rather than
silently doing nothing or pretending Linux's `.desktop`/gsettings mechanism
means anything here. Filling this in for real means giving each method a
Windows-native implementation (a Start Menu shortcut and Run-key entry in
place of a `.desktop`/autostart file, `RegisterHotKey` in place of a GNOME
custom keybinding, `%USERPROFILE%\\Pictures` in place of `~/Pictures`) --
against exactly the interface `snipux/platform/__init__.py` defines, with no
other module needing to change.
"""

from __future__ import annotations

from pathlib import Path

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
