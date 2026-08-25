"""Writes build/snipux.ico from the vendored logo PNGs, for build.ps1 to run
before PyInstaller so packaging/windows/snipux.spec has an icon to embed.

A standalone script, not an inline `python -c` in build.ps1, because
PowerShell's own re-quoting of a multi-line string passed as a native
command's argument is not reliable -- the embedded double quotes around
"build/snipux.ico" have been silently stripped that way before.

Never fails the build: a missing or unusable vendored PNG (render_ico()
returning None, see its own docstring) just means snipux.spec falls back to
no custom icon, the same "one step's failure must not stop the rest" rule
CLAUDE.md states for capture backends, applied here to packaging.
"""

from __future__ import annotations

from pathlib import Path

from snipux.setup_desktop import render_ico

_ICO_PATH = Path(__file__).resolve().parent.parent.parent / "build" / "snipux.ico"


def main() -> None:
    data = render_ico()
    if data is None:
        print("Note: no vendored icon found -- the .exe will use a generic icon.")
        return
    _ICO_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ICO_PATH.write_bytes(data)
    print(f"Icon written to {_ICO_PATH}")


if __name__ == "__main__":
    main()
