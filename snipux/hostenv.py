"""Programs snipux starts must not inherit the bundle's own libraries.

A PyInstaller/AppImage build starts with ``LD_LIBRARY_PATH`` pointing at the
libraries it ships, so the bundled Python and Qt find them. Every program
snipux then launches inherits that: ``hyprctl`` loads the bundle's older
``libstdc++`` and dies with ``version 'GLIBCXX_3.4.32' not found``. Measured
on Omarchy, from inside the 1.1.4 AppImage, every ``hyprctl`` call failed.
Snipux uses ``hyprctl`` to turn off Hyprland's window-open animation for the
snip, so with it dead the animation played: the whole desktop shrank and
sprang back open each time a snip started. It is also why ``snipux --setup``
could not reload Hyprland from the AppImage.

The bundle's own process keeps the variable (it is still loading plugins
from there); only children are given the clean environment. On a normal
install there is nothing to remove, and `install` does nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys

_VARIABLE = "LD_LIBRARY_PATH"
_ORIGINAL = "LD_LIBRARY_PATH_ORIG"

_installed = False


def _bundle_roots() -> list[str]:
    roots = [getattr(sys, "_MEIPASS", None), os.environ.get("APPDIR")]
    return [os.path.realpath(root) for root in roots if root]


def host_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """`environ` (default: this process's) as a child should see it: with the
    bundle's library directories taken out of ``LD_LIBRARY_PATH``, or the
    value PyInstaller saved before it overwrote it, when it saved one."""
    env = dict(os.environ if environ is None else environ)
    original = env.pop(_ORIGINAL, None)
    if original is not None:
        # PyInstaller's own record of what the variable held before it ran.
        if original:
            env[_VARIABLE] = original
        else:
            env.pop(_VARIABLE, None)
        return env

    current = env.get(_VARIABLE)
    if not current:
        return env
    roots = _bundle_roots()
    kept = [
        entry
        for entry in current.split(os.pathsep)
        if entry and not any(_is_within(entry, root) for root in roots)
    ]
    if kept:
        env[_VARIABLE] = os.pathsep.join(kept)
    else:
        env.pop(_VARIABLE, None)
    return env


def _is_within(path: str, root: str) -> bool:
    path = os.path.realpath(path)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def needs_cleaning() -> bool:
    """True when this process's environment differs from what a child
    should get -- i.e. only inside a bundle."""
    return host_env() != dict(os.environ)


def install() -> bool:
    """Make every ``subprocess`` child start with `host_env()` unless the
    caller passed an ``env`` of their own. True if anything was changed.

    Done once, here, rather than at the forty-odd places snipux starts a
    program: the next one added would otherwise bring the bug back.
    """
    global _installed
    if _installed or not needs_cleaning():
        return False
    original_init = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):
        if kwargs.get("env") is None:
            kwargs["env"] = host_env()
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__
    subprocess.Popen.__init__.__wrapped__ = original_init  # for tests to undo
    _installed = True
    return True


def uninstall() -> None:
    """Undo `install`. For tests; nothing in the app calls it."""
    global _installed
    wrapped = getattr(subprocess.Popen.__init__, "__wrapped__", None)
    if wrapped is not None:
        subprocess.Popen.__init__ = wrapped
    _installed = False


def apply_to_qprocess(process) -> None:
    """The same, for a `QProcess` (the player's ffmpeg), which does not go
    through ``subprocess``."""
    if not needs_cleaning():
        return
    from PyQt6.QtCore import QProcessEnvironment

    environment = QProcessEnvironment()
    for key, value in host_env().items():
        environment.insert(key, value)
    process.setProcessEnvironment(environment)
