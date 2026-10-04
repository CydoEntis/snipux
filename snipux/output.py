"""Where a finished capture goes: the clipboard, or a file.

These lived in `app.py`, which left `overlay.py` and `pin.py` importing the
application controller from inside their own methods to reach them -- an
import cycle kept alive by deferring it. They need nothing from the
controller, so they sit here, below every view, and `app.py` re-exports
them for its own callers.

Every copy also lands in the copy history below, for the same reason: it is
the one place all copies pass through.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QBuffer, QIODevice, QMimeData, QUrl
from PyQt6.QtGui import QGuiApplication, QImage


def _settle_clipboard() -> None:
    """Make sure the compositor has taken what was just put on the clipboard
    before anything else happens -- one round trip, `QGuiApplication.sync()`.

    On Wayland a selection is only accepted from the client with keyboard
    focus, and Qt hands it over asynchronously. Every copy here is followed
    at once by the overlay closing, so without the round trip the window
    could be gone -- and its focus with it -- before the compositor saw the
    request. Measured on GNOME 46: Enter copied and toasted "Copied to
    clipboard" while the clipboard kept whatever it held before; a button
    click happened to win the race. On X11 this is an XSync, and costs
    nothing.
    """
    QGuiApplication.sync()


# Copy history: the last few things snipux put on the clipboard, so a snip
# that was only ever copied -- never saved -- can be copied again after
# something else has replaced it. Held in memory and nowhere else: a
# screenshot can carry a password or a private message, and writing every
# copy to disk without being asked would keep it long after the user
# thought it was gone. Quitting snipux forgets all of it.
COPY_HISTORY_MAX = 10


@dataclass(frozen=True, eq=False)
class CopiedItem:
    """One copy, as it went to the clipboard. `kind` says which of the
    three copy functions below made it, and so what `value` is: a QImage
    for "image", a str for "text", a Path for "file"."""

    kind: str
    value: QImage | str | Path
    copied_at: datetime.datetime


_copy_history: list[CopiedItem] = []
_copy_history_listener: Callable[[], None] | None = None


def copy_history() -> list[CopiedItem]:
    """Newest first."""
    return list(_copy_history)


def clear_copy_history() -> None:
    _copy_history.clear()
    _notify_copy_history()


def set_copy_history_listener(listener: Callable[[], None] | None) -> None:
    """Who to tell when the history changes -- the tray, so its menu is
    current even where GNOME never asks for it before opening it. One
    slot, not a list: there is one tray, and a new controller replacing an
    old one should not leave the old one still being called."""
    global _copy_history_listener
    _copy_history_listener = listener


def recopy(item: CopiedItem) -> None:
    """Put a history entry back on the clipboard, which also moves it to
    the top of the history -- it is now the newest thing copied."""
    if item.kind == "image":
        copy_image_to_clipboard(item.value)
    elif item.kind == "text":
        copy_text_to_clipboard(item.value)
    else:
        copy_file_to_clipboard(item.value)


def _remember(kind: str, value: QImage | str | Path) -> None:
    # The same thing copied twice is one entry, moved to the top, not two:
    # otherwise copying a snip again from the history would push a second
    # copy of it in and the oldest real entry out.
    for existing in _copy_history:
        if existing.kind == kind and existing.value == value:
            _copy_history.remove(existing)
            break
    _copy_history.insert(0, CopiedItem(kind, value, datetime.datetime.now()))
    del _copy_history[COPY_HISTORY_MAX:]
    _notify_copy_history()


def _notify_copy_history() -> None:
    if _copy_history_listener is not None:
        _copy_history_listener()


def copy_image_to_clipboard(image: QImage) -> None:
    """Place `image` on the clipboard: the in-process Qt clipboard always,
    and (best-effort) `wl-copy` as well when it's on PATH.

    Wayland's Qt clipboard is owned by the process that set it and dies the
    instant it exits, per CLAUDE.md — piping the same image to `wl-copy`
    (which persists independently) is what lets a copied snip survive the
    app closing. `shutil.which` is checked first so a missing binary is a
    silent skip rather than a raised `FileNotFoundError`, mirroring
    `_x11_shell_backend_available`'s check-first pattern in capture.py; the
    subprocess call itself is also guarded in case the binary vanishes
    between the check and the call, or runs but exits non-zero — either way
    this must not raise.
    """
    QGuiApplication.clipboard().setImage(image)
    _settle_clipboard()
    # A deep copy: callers hand over images backed by buffers they own and
    # are about to free (the overlay's `rendered_image()`, for one).
    _remember("image", image.copy())

    if shutil.which("wl-copy") is None:
        return

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    png_bytes = buffer.data().data()

    try:
        subprocess.run(
            ["wl-copy", "--type", "image/png"], input=png_bytes, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        pass  # Qt clipboard already holds the image; this sink is best-effort


def copy_text_to_clipboard(text: str) -> None:
    """Place `text` on the clipboard: the in-process Qt clipboard always,
    and (best-effort) `wl-copy` as well when it's on PATH -- the same
    Wayland persistence `copy_image_to_clipboard` exists for, and the same
    reasoning applies here unchanged.
    """
    QGuiApplication.clipboard().setText(text)
    _settle_clipboard()
    _remember("text", text)

    if shutil.which("wl-copy") is None:
        return

    try:
        subprocess.run(
            ["wl-copy", "--type", "text/plain"], input=text.encode("utf-8"), check=True
        )
    except (OSError, subprocess.CalledProcessError):
        pass  # Qt clipboard already holds the text; this sink is best-effort


def copy_file_to_clipboard(path: Path) -> None:
    """Place a *reference* to the file at `path` on the clipboard -- the way
    Windows Snipping Tool does it, and the only way a recording can be
    copied since there is no bitmap to put there instead.

    A sibling to `copy_image_to_clipboard`, not a branch inside it: the
    `QMimeData` shape is different (a URL list, not image bytes) and so are
    the flavours involved. `QMimeData.setUrls` with a `QUrl.fromLocalFile`
    is the whole of what makes Qt's clipboard backend map this to `CF_HDROP`
    on Windows and `text/uri-list` on Linux -- there is nothing to branch on
    `sys.platform` for here.

    This function does not touch the filesystem beyond reading `path` to
    build the URL -- it does not create, move, or check that the file
    exists. Copy is save-then-copy: it's the caller's job to have written a
    real file first.
    """
    url = QUrl.fromLocalFile(str(path))

    mime = QMimeData()
    mime.setUrls([url])
    # Nautilus (and other GNOME file managers) ignore text/uri-list for
    # paste and want this flavour instead: an operation word ("copy") then
    # one URI per line, on the *same* QMimeData.
    #
    # `toEncoded()`, not `toString()`. `toString()` returns QUrl's *pretty*
    # form, which leaves spaces and non-ASCII characters exactly as they
    # are -- so this flavour used to carry `file:///.../Screen recording
    # .webm`, which is not a URI, while `setUrls` above put the properly
    # escaped one in text/uri-list. Two flavours on one QMimeData naming
    # the same file two different ways, and this is not a corner case: the
    # default filename pattern ("Screenshot from %Y-%m-%d %H-%M-%S")
    # always contains spaces, so every recording copied hit it.
    mime.setData("x-special/gnome-copied-files", b"copy\n" + bytes(url.toEncoded()))
    QGuiApplication.clipboard().setMimeData(mime)
    _settle_clipboard()
    _remember("file", path)

    if shutil.which("wl-copy") is None:
        return

    try:
        subprocess.run(
            ["wl-copy", "--type", "text/uri-list"],
            # Percent-encoded, for the same reason the GNOME flavour
            # above is: text/uri-list carries URIs, and a raw space is
            # not one.
            input=bytes(url.toEncoded()) + b"\n",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pass  # Qt clipboard already holds the file reference; best-effort sink


def display_path(path: Path | None) -> str:
    r"""A path as a person should read it: `~`-relative where it sits under
    their home directory, whole where it doesn't.

    Here rather than in each window, because all three of them show a
    written-to path and had grown their own copy. The player's copy built
    `"~/" + ...`, which on Windows produced
    `~/Pictures\snipux\Screenshot from ....png` -- one path spelled two ways
    in a single string, in the label whose whole job is saying where the
    file went. `os.sep` is what `relative_to()` renders with, so it is what
    the prefix has to use.
    """
    if path is None:
        return "Not saved to disk"
    try:
        return f"~{os.sep}{Path(path).relative_to(Path.home())}"
    except ValueError:
        return str(path)


def save_image(image: QImage, directory: Path | str | None = None) -> Path:
    """Write `image` as a PNG into `directory` (or `~/Pictures` by default)
    under a filename derived from the current date and time, and return the
    path written.

    The directory is created if it doesn't exist — "save without naming it"
    implies this must not fail just because `~/Pictures` isn't there yet on
    a fresh machine.
    """
    if directory is None:
        directory = Path.home() / "Pictures"
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    filename = datetime.datetime.now().strftime("Screenshot from %Y-%m-%d %H-%M-%S.png")
    path = directory / filename
    # QImage.save() reports failure by returning False, not by raising, and a
    # disk that is full or a folder that has been made read-only both land
    # here. Ignoring it returned a path to a file that was never written, and
    # every caller believed it: the overlay toasted "Saved to ~/Pictures",
    # and app.py added the missing file to the Recent list. Raising is what
    # gives the caller something to tell the user -- `review.py` already
    # checks the same boolean for its own writes.
    if not image.save(str(path), "PNG"):
        raise OSError(f"could not write {path}")
    return path
