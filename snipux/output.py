"""Where a finished capture goes: the clipboard, or a file.

These lived in `app.py`, which left `overlay.py` and `pin.py` importing the
application controller from inside their own methods to reach them -- an
import cycle kept alive by deferring it. They need nothing from the
controller, so they sit here, below every view, and `app.py` re-exports
them for its own callers.
"""

from __future__ import annotations

import base64
import datetime
import os
import shutil
import subprocess
from pathlib import Path

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


# How many characters of base64 go on one line of the rebuild command.
# Not cosmetic: a terminal in canonical mode stops accepting a single line
# somewhere around 4 KB, and the rest is dropped *silently* -- the paste
# looks fine and the file on the far end is truncated. 76 is the width
# `base64` itself wraps at, so what is pasted looks like what that command
# would have written.
_TERMINAL_LINE_WIDTH = 76

# Above this, the paste is the problem rather than the solution: tens of
# thousands of lines take long enough that a terminal looks hung, and some
# refuse a paste that size outright. A snip of a window is about 90 KB
# encoded, so this leaves a lot of room before it bites.
_TERMINAL_MAX_BYTES = 1_000_000


def terminal_paste_command(image: QImage, filename: str | None = None) -> str | None:
    """`image` as a shell command that writes it to a file, or None when it
    would be too big to paste.

    For pasting into a terminal on another machine -- an SSH session, a
    container, a serial console. A clipboard cannot cross that boundary:
    what a terminal receives is keystrokes, so the only thing that travels
    is text. This is the image *as* text, in a form the far end can turn
    back into a file with nothing installed on it -- `base64` is in both
    coreutils and busybox.

    A quoted heredoc (`<<'SNIPUX'`), so nothing in the data is expanded by
    the shell it lands in, and a quoted filename so a space cannot split it.
    Bracketed paste -- on by default in current bash and zsh -- means the
    whole thing arrives as one input and waits for Return rather than
    running a line at a time.
    """
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    encoded = base64.b64encode(bytes(buffer.data())).decode("ascii")
    if len(encoded) > _TERMINAL_MAX_BYTES:
        return None

    if filename is None:
        filename = datetime.datetime.now().strftime("snip-%Y-%m-%d-%H%M%S.png")
    lines = [
        encoded[at:at + _TERMINAL_LINE_WIDTH]
        for at in range(0, len(encoded), _TERMINAL_LINE_WIDTH)
    ]
    body = "\n".join(lines)
    return f"base64 -d > '{filename}' <<'SNIPUX'\n{body}\nSNIPUX\n"


def copy_image_to_clipboard(image: QImage, *, also_as_text: str = "") -> None:
    """Place `image` on the clipboard: the in-process Qt clipboard always,
    and (best-effort) `wl-copy` as well when it's on PATH.

    `also_as_text` puts a second form on the same clipboard entry, which is
    how one Copy serves both an image editor and a terminal. A clipboard
    carries several formats at once and the *receiving* application picks
    the one it understands: anything that takes pictures asks for the image
    and never sees the text, and a terminal can only take text, so it gets
    that. Without it a paste into a terminal does nothing at all, which is
    what it did -- there was simply nothing there it could accept.

    Empty by default. The text is not free: a plain text box is not a
    terminal, and pasting a rebuild command into one is a wall of base64,
    so this is on only when the row's flag says so.

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
    if also_as_text:
        # One QMimeData carrying both, rather than two calls: the second
        # would replace the first, since setting the clipboard replaces
        # what is on it rather than adding to it.
        mime = QMimeData()
        mime.setImageData(image)
        mime.setText(also_as_text)
        QGuiApplication.clipboard().setMimeData(mime)
    else:
        QGuiApplication.clipboard().setImage(image)
    _settle_clipboard()

    if shutil.which("wl-copy") is None:
        return

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    png_bytes = buffer.data().data()

    try:
        # The image, even when text was offered too: one `wl-copy` serves one
        # type, and this call exists to make a copy survive snipux exiting.
        # What survives should be the picture -- the text form can always be
        # produced again by copying again, and a terminal paste is something
        # done in the moment rather than an hour later.
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
