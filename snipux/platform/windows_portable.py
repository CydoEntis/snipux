"""The portable `snipux.exe` installs itself as a folder build, once.

The portable download is a PyInstaller single-file build: its bootloader
unpacks every library it carries into `%TEMP%\\_MEI*` before Python starts,
on every launch, and nothing PyInstaller offers keeps that folder between
runs. Copying the single file to `%LOCALAPPDATA%\\snipux` (what
`windows._ensure_stable_copy()` did on its own until now) made the path
stable and left the cost where it was: every login and every launch from
the Start Menu unpacked the lot again.

So the single file also carries the *folder* build's exe -- the same one
the installer ships, a few megabytes, as plain data at `FOLDER_EXE` -- and
the first run turns what the bootloader has just unpacked into a folder
build:

    <install dir>\\snipux.exe     the bundled folder-build exe
    <install dir>\\_internal\\    a copy of this run's unpacked libraries

That is the layout, and the folder, the installer uses. The shortcuts
already name `<install dir>\\snipux.exe`, so they never move, and the
installer run later over a portable install upgrades it like any other.
Every launch of the single file then ends by starting that exe with the
same arguments; launches of the folder build itself unpack nothing.

Three rules keep it from making anything worse than the single-file copy
it replaces:

- **Nothing half-written is ever startable.** The new build is assembled
  in a staging folder beside the old one, started once there to prove it
  runs, and only then moved in -- the exe last, so a folder holding
  `snipux.exe` always holds the libraries that go with it.
- **The installer's install is the installer's.** A folder with its
  uninstaller in it is left exactly as it is.
- **Every failure falls back.** `hand_off()` answers None, with the reason
  in snipux.log, and the launch carries on inside the single-file process
  the way it always has.

Standard library only, and nothing here reads the environment or decides
where the install lives: `windows.py` passes that in.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from snipux import log

# Where packaging/windows/snipux.spec puts the folder build's exe inside
# the single-file bundle, relative to `sys._MEIPASS`.
FOLDER_EXE = "_folder_build/snipux.exe"

_LIBRARIES = "_internal"
_UNINSTALLER = "unins000.exe"
_STAGING = ".staging"
_REPLACED = ".replaced"
_PROBE_TIMEOUT_S = 60

# CreateProcess flags (winbase.h); `subprocess` only defines them on Windows.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def bundled_folder_exe() -> Path | None:
    """The folder build's exe this process carries, or None for every build
    but the single-file one -- which is how the rest of this module tells
    that build from a checkout, a pip install and the folder build itself.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or bundle is None:
        return None
    folder_exe = Path(bundle) / FOLDER_EXE
    return folder_exe if folder_exe.is_file() else None


def owned_by_installer(target: Path) -> bool:
    """Whether snipux-setup put the build at `target` there. Only the
    installer writes an uninstaller, and the folder says nothing: both
    builds live in the same one.
    """
    return (target.parent / _UNINSTALLER).is_file()


def hand_off(
    target: Path,
    arguments: list[str],
    *,
    before_start: Callable[[], None] = lambda: None,
) -> int | None:
    """Run this launch from the folder build at `target`, installing it
    first if it is missing or another version. The exit code to finish
    with, or None when this process should carry on itself.

    `before_start` runs once the install is settled and before `target` is
    started for real -- see `WindowsPlatform.hand_off_to_stable_install`.
    """
    folder_exe = bundled_folder_exe()
    if folder_exe is None:
        return None
    # Removal deletes the folder build, which it cannot do from inside it.
    if "--remove" in arguments:
        return None
    if owned_by_installer(target) or _is_this_process(target):
        return None
    if not _is_current(target, folder_exe) and not _install(target, folder_exe):
        return None
    before_start()
    return _start(target, arguments)


def _is_this_process(target: Path) -> bool:
    """Whether this single-file exe is itself what sits at `target`: the
    fallback copy `windows._ensure_stable_copy()` leaves when a folder build
    could not be installed. It cannot replace the file it is running from.
    """
    try:
        return Path(sys.executable).samefile(target)
    except OSError:
        return False


def _is_current(target: Path, folder_exe: Path) -> bool:
    """Whether `target` is already this build. The folder exe holds every
    Python module of its build, so two that match byte for byte are the
    same version, and `_swap()` only ever puts one beside its own
    libraries.
    """
    try:
        return (target.parent / _LIBRARIES).is_dir() and filecmp.cmp(
            target, folder_exe, shallow=False
        )
    except OSError:
        return False


def _install(
    target: Path, folder_exe: Path, *, starts: Callable[[Path], bool] | None = None
) -> bool:
    """Put this build at `target` as a folder build. False, with a note and
    with whatever was at `target` still there, when it cannot be done.
    """
    root = target.parent
    staging = root / _STAGING
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Left behind only by a run that was killed part-way.
        _discard(staging)
        _discard(root / _REPLACED)
        if _in_use(target):
            log.note(
                f"Note: Snipux is running from {target}, so this version could not "
                "replace it. Quit Snipux from its tray icon and run this file again.",
                to_stderr=True,
            )
            return False
        _stage(folder_exe, staging)
        if not (starts or _starts)(staging / target.name):
            log.note(
                f"Note: the copy of Snipux prepared in {staging} did not start, so "
                f"it was not installed to {root}. Snipux will keep unpacking itself "
                "on each start instead.",
                to_stderr=True,
            )
            _discard(staging)
            return False
        _swap(staging, target)
    except OSError as exc:
        log.note(
            f"Note: could not install Snipux to {root}: {exc}. Snipux will keep "
            "unpacking itself on each start instead.",
            to_stderr=True,
        )
        _discard(staging)
        return False
    _discard(staging)
    _discard(root / _REPLACED)
    return True


def _in_use(exe: Path) -> bool:
    """Whether `exe` is running: Windows refuses write access to the file
    behind a running process. Asked before copying a hundred megabytes that
    could not then be moved into place.
    """
    try:
        with exe.open("r+b"):
            return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _stage(folder_exe: Path, staging: Path) -> None:
    """Assemble the folder build in `staging`: the unpacked bundle this
    process is running from becomes its libraries, minus the folder exe
    carried inside it, which becomes its exe.
    """
    carried = Path(FOLDER_EXE).parts[0]
    bundle = folder_exe.parent.parent

    def without_the_carried_exe(directory: str, names: list[str]) -> list[str]:
        return [carried] if Path(directory) == bundle and carried in names else []

    shutil.copytree(bundle, staging / _LIBRARIES, ignore=without_the_carried_exe)
    shutil.copy2(folder_exe, staging / folder_exe.name)


def _starts(exe: Path) -> bool:
    """Whether `exe` runs to the end of `--help`. A copy that an antivirus
    quarantined a library out of, or that an application-control policy
    refuses to start, fails here instead of at the next login.
    """
    try:
        finished = subprocess.run(
            [str(exe), "--help"],
            env=_child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return finished.returncode == 0


def _swap(staging: Path, target: Path) -> None:
    """Move the staged build into place, undoing itself if it cannot finish.

    The old exe goes first and the new exe arrives last, so between the
    two there is no `snipux.exe` in the folder at all: a shortcut used at
    that instant finds nothing, never an exe beside another version's
    libraries.
    """
    root = target.parent
    replaced = root / _REPLACED
    replaced.mkdir()
    moved: list[str] = []
    libraries_in_place = False
    try:
        for name in (target.name, _LIBRARIES):
            if (root / name).exists():
                os.rename(root / name, replaced / name)
                moved.append(name)
        os.rename(staging / _LIBRARIES, root / _LIBRARIES)
        libraries_in_place = True
        os.replace(staging / target.name, target)
    except OSError:
        if libraries_in_place:
            os.rename(root / _LIBRARIES, staging / _LIBRARIES)
        for name in reversed(moved):
            os.rename(replaced / name, root / name)
        # rmdir, not rmtree: were anything not put back, it stays findable.
        replaced.rmdir()
        raise


def _discard(folder: Path) -> None:
    # Best effort: what cannot be deleted now is deleted by the next install.
    shutil.rmtree(folder, ignore_errors=True)


def _child_environment() -> dict[str, str]:
    """This process's environment, for a different PyInstaller build to
    start in: its bootloader is told not to treat itself as a child of
    this one's bundle.
    """
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}


def _start(target: Path, arguments: list[str]) -> int | None:
    """Start the folder build with this launch's arguments. None when it
    could not be started at all.

    A launch with arguments is a command someone may be waiting on, so its
    exit code is this one's. A launch without is the tray app, which runs
    until Quit: it is started on its own and this process ends, so the
    downloaded file and the folder it unpacked into are not held open for
    as long as Snipux runs.
    """
    try:
        if arguments:
            return subprocess.run(
                [str(target), *arguments], env=_child_environment()
            ).returncode
        subprocess.Popen(
            [str(target)],
            cwd=str(target.parent),
            env=_child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        log.note(f"Note: could not start {target}: {exc}", to_stderr=True)
        return None
    return 0


def remove_libraries(target: Path) -> None:
    """Delete the libraries beside `target`, for `snipux --remove`, once
    the exe itself is gone. Raises OSError like the unlink before it.
    """
    libraries = target.parent / _LIBRARIES
    if libraries.is_dir():
        shutil.rmtree(libraries)
