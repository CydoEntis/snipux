"""Replacing the AppImage snipux is running from, and starting the new one.

An AppImage is one file the user keeps somewhere, and every launcher --
the app menu entry, autostart, the Hyprland or GNOME shortcut -- names
that file by its path (`setup_desktop.find_console_script`). So an update
is the new file *at the same path*: nothing that points at it is rewritten,
because nothing that points at it has changed.

The running process is not disturbed by this. AppRun mounts the file it
was started from and holds it open, so the old contents stay readable for
as long as this process lives -- provided the file is *replaced* rather
than written into. That is the one rule here: the new AppImage is written
under a temporary name in the same folder and moved over the old one with
`os.replace`, which is atomic. Opening the old file for writing instead
would change the pages a running program is executing from, and a power
cut halfway would leave a file that is neither version.

Kept apart from linux.py for the reason windows_portable.py is apart from
windows.py: it is file handling with no Qt in it, and it is tested with a
folder and a fake launcher rather than with a platform.
"""

from __future__ import annotations

import errno
import os
import platform as host_platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from snipux import hostenv, log

from . import UpdateError

# What AppRun and PyInstaller export for the process they started. A new
# AppImage sets its own; inheriting these would describe the old mount,
# which is gone a moment after this process exits.
_BUNDLE_VARIABLES = ("APPIMAGE", "APPDIR", "ARGV0", "OWD")

_BACKUP_SUFFIX = ".bak"


def running_appimage() -> Path | None:
    """The `.AppImage` file this process was started from, or None when it
    was not started from one.

    `$APPIMAGE` is AppRun's own marker, the same one
    `setup_desktop.find_console_script` reads. Resolved, so that a symlink
    to the file (`/usr/bin/snipux -> /opt/snipux/Snipux.AppImage`) has the
    file replaced and stays a symlink, rather than being replaced itself.
    """
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        return None
    path = Path(appimage)
    try:
        return path.resolve() if path.is_file() else None
    except OSError:
        return None


def can_replace(appimage: Path) -> bool:
    """Whether this user can put a different file at `appimage`'s path.

    The folder is what matters, not the file: replacing is a rename into
    the folder. One installed for everyone (`/opt/snipux`, the AUR
    package) fails this, and is updated by whatever put it there.
    """
    return os.access(appimage.parent, os.W_OK | os.X_OK)


def asset_name(version: str) -> str:
    """The AppImage the release attaches for this machine, as
    packaging/linux/build_appimage.sh names it."""
    return f"Snipux-{version}-{_machine()}.AppImage"


def _machine() -> str:
    # The stdlib's `platform`, not this package: the release names its
    # files after `uname -m`.
    return host_platform.machine()


def backup_path(appimage: Path, version: str) -> Path:
    return appimage.with_name(f"{appimage.name}.{version}{_BACKUP_SUFFIX}")


def install(downloaded: Path, appimage: Path, current_version: str) -> Path:
    """Put `downloaded` at `appimage`'s path, keeping what was there as
    `<name>.<current_version>.bak`. Returns the backup's path.

    `downloaded` has already been checked against the release's SHA-256
    (`updates.download` writes nothing that fails it), which is why nothing
    here looks at its contents.

    Raises `UpdateError`, with a sentence for the user, when it cannot --
    and in that case the AppImage at `appimage` is the one that was there
    before: every step that can fail comes before the rename.
    """
    # Same folder, so the rename below never crosses a filesystem -- a
    # rename that does is a copy, and a copy is not atomic. Hidden, and
    # named for the file it becomes, so one left by a crash is recognisably
    # ours and is simply overwritten by the next update.
    staged = appimage.with_name(f".{appimage.name}.new")
    backup = backup_path(appimage, current_version)
    try:
        with open(downloaded, "rb") as source, open(staged, "wb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            # On disk before it takes the old one's place: a rename is
            # durable long before the data it names is, and a power cut in
            # between would leave an empty file where snipux used to be.
            os.fsync(target.fileno())
        os.chmod(staged, 0o755)
        _keep_backup(appimage, backup)
        os.replace(staged, appimage)
    except OSError as exc:
        _discard(staged)
        log.note(f"Note: could not replace {appimage}: {exc}")
        raise UpdateError(_failure_sentence(exc, appimage)) from exc

    _remove_older_backups(appimage, keep=backup)
    _discard(downloaded)
    return backup


def _keep_backup(appimage: Path, backup: Path) -> None:
    """Leave the current AppImage reachable as `backup` once its own name
    is given to the new one.

    A hard link where the filesystem has them: it is instant, costs no
    disk, and after the rename it is simply the old file under another
    name. A copy otherwise, written beside its final name first so a full
    disk cannot leave half a backup that looks like a whole one.
    """
    _discard(backup)   # an earlier update from this same version
    try:
        os.link(appimage, backup)
        return
    except OSError:
        pass
    partial = backup.with_name(backup.name + ".part")
    try:
        shutil.copy2(appimage, partial)
        os.replace(partial, backup)
    except OSError:
        _discard(partial)
        raise


def _remove_older_backups(appimage: Path, *, keep: Path) -> None:
    """One backup is a way back; one per update is 80 MB each, for good."""
    try:
        older = [
            path
            for path in appimage.parent.glob(f"{appimage.name}.*{_BACKUP_SUFFIX}")
            if path != keep
        ]
    except OSError:
        return
    for path in older:
        _discard(path)


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _failure_sentence(exc: OSError, appimage: Path) -> str:
    folder = appimage.parent
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return (
            f"There is not enough free space in {folder} for the update, so "
            "nothing was changed. Free some space and pick Update again."
        )
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return (
            f"Snipux cannot write to {folder}, so it could not replace "
            "itself and nothing was changed. Download the new AppImage from "
            f"the Releases page and save it as {appimage}."
        )
    return (
        f"Could not install the update ({exc.strerror or exc}), so nothing "
        "was changed. Download the new AppImage from the Releases page and "
        f"save it as {appimage}."
    )


def relaunch(appimage: Path, launcher: Callable | None = None) -> bool:
    """Start the AppImage now at `appimage` as a process of its own, and
    say whether it started.

    The caller has stopped listening for requests before calling this:
    a snipux started while the old one still answers would hand it a
    request and exit, and the update would end with nothing running.

    A new session, so it is not in this process's group and does not go
    down with it; nothing inherited to read or write, so it does not hold
    a terminal or a pipe that is about to close.

    `launcher` is `subprocess.Popen`, injected so a test starts nothing.
    """
    env = hostenv.host_env()
    for name in _BUNDLE_VARIABLES:
        env.pop(name, None)
    # PyInstaller's bootloader reads its parent's variables to decide it is
    # a child of the same application, and would then look for the old
    # mount. This is its documented switch for "a separate instance".
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    launch = launcher if launcher is not None else subprocess.Popen
    try:
        launch(
            [str(appimage)],
            env=env,
            # Not this process's: that may be inside the mount AppRun
            # removes when it exits.
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        log.note(f"Note: could not start {appimage}: {exc}")
        return False
    return True


def manual_update_hint(version: str) -> str:
    """How someone updates this install by hand, in one sentence -- for an
    install snipux cannot update itself, and the right command differs for
    every way there is of installing it.
    """
    appimage = running_appimage()
    if appimage is not None:
        if not can_replace(appimage):
            return (
                f"This AppImage is in {appimage.parent}, which Snipux cannot "
                "write to: update it the way it was installed, or download "
                f"{asset_name(version)} from the Releases page."
            )
        return (
            f"Download {asset_name(version)} from the Releases page and save "
            f"it as {appimage}."
        )
    if getattr(sys, "frozen", False):
        # Frozen and not an AppImage is the .deb: the one route with a
        # package manager to hand the file to, and one that needs root.
        deb = f"snipux_{version}_{_deb_arch()}.deb"
        return (
            f"Download {deb} from the Releases page and install it with "
            f"`sudo apt install ./{deb}`."
        )
    if "pipx" in Path(sys.prefix).parts:
        # pipx keeps each application in a venv of its own and refuses a
        # plain `pip install` into it from outside.
        return "Run `pipx upgrade snipux` to install it."
    return "Run `snipux --update` to install it."


def _deb_arch() -> str:
    # packaging/linux/build_bundle.sh's own mapping.
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(
        _machine(), _machine()
    )
