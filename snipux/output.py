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

from . import log


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
# And no more pixels than this, whatever the count. Each picture is kept at
# full resolution -- 33 MB for a 4K desktop, 59 MB for a 5K one -- so ten of
# them was most of a gigabyte held until quit. This is room for a few
# full-desktop copies or a great many ordinary region snips.
COPY_HISTORY_MAX_BYTES = 128 * 1024 * 1024


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
    # The newest is kept whatever its size: it is what was just copied, and
    # a history that cannot give back the last copy is not one.
    while _copy_history_bytes() > COPY_HISTORY_MAX_BYTES:
        older_pictures = [item for item in _copy_history[1:] if item.kind == "image"]
        if not older_pictures:
            break
        _copy_history.remove(older_pictures[-1])
    _notify_copy_history()


def _copy_history_bytes() -> int:
    """What the kept pictures weigh. Text and file references are not
    counted: next to one picture they are nothing."""
    return sum(
        item.value.sizeInBytes() for item in _copy_history if item.kind == "image"
    )


def _notify_copy_history() -> None:
    if _copy_history_listener is not None:
        _copy_history_listener()


# `wl-copy` hands the data to a child of its own and returns in a few
# milliseconds. It runs on the UI thread, straight after a copy, so one that
# never returns -- a compositor that will not take the selection -- froze
# Snipux for good. Long enough for a loaded machine, short enough to sit
# through once.
WL_COPY_TIMEOUT_SECONDS = 3.0


def _pipe_to_wl_copy(mime_type: str, data: bytes) -> None:
    """Hand `data` to `wl-copy` so the copy outlives this process. Best
    effort: the Qt clipboard already holds it, so a failure here is noted
    and never raised."""
    try:
        subprocess.run(
            ["wl-copy", "--type", mime_type],
            input=data,
            check=True,
            timeout=WL_COPY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.note(
            f"Note: wl-copy did not finish within {WL_COPY_TIMEOUT_SECONDS:g} "
            "seconds and was stopped. The copy is on the clipboard now, but "
            "may not still be there after Snipux quits."
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        log.note(
            f"Note: wl-copy could not take the copy ({exc}). It is on the "
            "clipboard now, but may not still be there after Snipux quits."
        )


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
    _pipe_to_wl_copy("image/png", buffer.data().data())


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

    _pipe_to_wl_copy("text/plain", text.encode("utf-8"))


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

    # Percent-encoded, for the same reason the GNOME flavour above is:
    # text/uri-list carries URIs, and a raw space is not one.
    _pipe_to_wl_copy("text/uri-list", bytes(url.toEncoded()) + b"\n")


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


def save_image(
    image: QImage,
    directory: Path | str | None = None,
    *,
    pattern: str | None = None,
) -> Path:
    """Write `image` as a PNG into `directory` (or `~/Pictures` by default)
    under the filename pattern from Settings, and return the path written.

    The directory is created if it doesn't exist — "save without naming it"
    implies this must not fail just because `~/Pictures` isn't there yet on
    a fresh machine.

    `pattern` is read from Settings when not given. It used not to be read
    at all: Settings stored it and previewed the name it would make, and
    every snip was still called "Screenshot from ...".

    Never over an existing file. The default pattern counts in seconds and
    a user's own may not count at all, so two saves could land on one name
    and the second silently replaced the first; a later one gets " (2)".
    """
    if directory is None:
        directory = Path.home() / "Pictures"
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    from . import setup_desktop

    if pattern is None:
        pattern = setup_desktop.load_filename_pattern()
    try:
        path = Path(setup_desktop.preview_filename(directory, pattern))
    except ValueError:
        # A pattern strftime refuses is not worth losing the snip over.
        path = Path(
            setup_desktop.preview_filename(directory, setup_desktop.tokens.FILENAME_DEFAULT)
        )
    stem, number = path.stem, 2
    while path.exists():
        path = path.with_name(f"{stem} ({number}){path.suffix}")
        number += 1
    # A pattern may name a folder of its own ("%Y-%m/snip %H%M%S").
    path.parent.mkdir(parents=True, exist_ok=True)
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
