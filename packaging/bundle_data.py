"""The data files every PyInstaller bundle carries, in one place.

Both specs (`packaging/windows/snipux.spec`, `packaging/linux/snipux.spec`)
read their `datas` from here rather than each listing the same assets.
The list is not arbitrary and not stable: it is every directory that
`snipux/design/__init__.py`'s `PACKAGE_DIR` resolves against at runtime, and
`PACKAGE_DIR` is what a frozen build redirects to `sys._MEIPASS`. Miss an
entry and the failure is not an import error at build time but a missing
icon, or a `.desktop` template that isn't there, on someone else's machine.
Two copies of that list is two chances to update only one of them.

A spec is `exec`'d rather than imported, so it has no `__file__` to import
relative to -- each one puts this file's directory on `sys.path` using
`SPECPATH` (PyInstaller's own name for the spec's directory) before
importing it.
"""

from __future__ import annotations

from pathlib import Path


def bundle_datas(repo_root: Path) -> list[tuple[str, str]]:
    """PyInstaller `(source, destination)` pairs for every asset directory
    read through `PACKAGE_DIR` at runtime.

    Destinations are the `snipux/...`-relative paths `PACKAGE_DIR` resolves
    to under `sys._MEIPASS`, which is what makes the same asset-reading code
    work unchanged from a checkout, a wheel and a bundle.
    """
    snipux_dir = repo_root / "snipux"

    datas = [
        # design/__init__.py's icon() and the tray/window logo app.py loads.
        (str(snipux_dir / "design" / "icons"), "snipux/design/icons"),
        (str(snipux_dir / "design" / "logo"), "snipux/design/logo"),
        # setup_desktop.py's .desktop template (SNX-73). Bundled on every
        # platform even though only Linux renders it: teaching this list
        # which OS it is building for costs more than the handful of bytes.
        (str(snipux_dir / "snipux.desktop"), "snipux"),
    ]

    # design/fonts/ is empty in this handoff (see design/__init__.py's own
    # docstring). Bundling an empty directory is a no-op PyInstaller does not
    # need telling about, so this pair only appears once there is something
    # in it for font_families() to find.
    fonts_dir = snipux_dir / "design" / "fonts"
    if fonts_dir.is_dir() and any(fonts_dir.iterdir()):
        datas.append((str(fonts_dir), "snipux/design/fonts"))

    return datas
