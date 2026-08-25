"""The Linux `Platform`: a thin adapter onto `snipux.setup_desktop`.

Every operation here already exists in `setup_desktop.py` -- `.desktop`
entries, the GNOME custom-keybinding dance, XDG paths -- and is covered by
its own, much larger test suite (`tests/test_setup_desktop.py`) that
predates this seam. This module does not reimplement or duplicate any of
that; it only gives it a name in the shape `snipux/platform/__init__.py`
defines, so callers reach it through `platform.current` instead of
importing `setup_desktop` (a Linux specific) directly.
"""

from __future__ import annotations

from pathlib import Path

from snipux import setup_desktop

from . import Platform


class LinuxPlatform(Platform):
    def install_desktop_integration(self, *, shortcut: str | None = None) -> int:
        return setup_desktop.run_setup(shortcut=shortcut)

    def remove_desktop_integration(self) -> int:
        return setup_desktop.run_remove()

    def bind_shortcut(self, shortcut: str | None = None) -> str:
        # find_console_script()'s failure is reported here, not inside
        # bind_gnome_shortcut() -- the same split setup_desktop.run_setup()
        # already makes between "no console script" (fatal) and every other
        # step (a note) -- see its own docstring.
        exec_path = setup_desktop.find_console_script()
        if exec_path is None:
            return (
                "Settings saved, but the snipux console script could not be "
                "found, so the shortcut was not re-bound."
            )
        return setup_desktop.bind_gnome_shortcut(exec_path, shortcut)

    def unbind_shortcut(self) -> str:
        return setup_desktop.unbind_gnome_shortcut()

    def default_save_folder(self) -> Path:
        return setup_desktop.default_save_folder()
