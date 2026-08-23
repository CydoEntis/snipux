"""Controller, tray, and CLI entry point.

This ticket only establishes the CLI skeleton and the `--list-backends`
diagnostic — no real capture backends exist yet (those land in the X11 and
Wayland tickets), and no overlay/editor exists yet either, so there is
nothing meaningful for a bare invocation to do beyond print usage.

`copy_image_to_clipboard`/`save_image` also live here rather than in
`editor.py`: this is the only one of the two modules with no existing reason
to avoid `subprocess`/`shutil`/filesystem code (`capture.py` already owns
that pattern for backends; `editor.py` is scoped to widget/painting code).
`app.py` has no reason to import `editor.py`, so `editor.py` importing these
two functions from here stays one-directional.
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QGuiApplication, QImage

from snipux.capture import BackendRegistry


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
    image.save(str(path), "PNG")
    return path


def build_default_registry() -> BackendRegistry:
    """Construct the `BackendRegistry` the real app uses.

    Empty for now — no real `CaptureBackend` implementations exist yet.
    Later tickets extend this by appending real backend instances; its
    shape (a `BackendRegistry` with no arguments) does not change.
    """
    return BackendRegistry()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snipux",
        description="A Windows Snipping Tool workalike for Linux.",
    )
    parser.add_argument(
        "--list-backends",
        action="store_true",
        help="print each registered capture backend's name, availability, "
        "and reason if unavailable",
    )
    return parser


def _print_backends(registry: BackendRegistry) -> None:
    if len(registry) == 0:
        # An empty registry is expected today (no real backends exist yet),
        # but the output must never be silently empty in a way that looks
        # like a bug.
        print("no backends registered")
        return
    for backend in registry:
        if backend.is_available():
            print(f"{backend.name()}: available")
        else:
            reason = backend.unavailable_reason()
            if reason:
                print(f"{backend.name()}: unavailable ({reason})")
            else:
                print(f"{backend.name()}: unavailable")


def main(argv: list[str] | None = None, registry: BackendRegistry | None = None) -> int:
    """CLI entry point. Accepts an optional `registry` to stay testable
    without needing real backend availability on the machine running the
    tests.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()

    if not argv:
        # No arguments: nothing to capture with yet (no overlay, no real
        # backends), so print usage and exit cleanly rather than doing
        # nothing silently or trying to launch a display.
        parser.print_help()
        return 0

    args = parser.parse_args(argv)

    if registry is None:
        registry = build_default_registry()

    if args.list_backends:
        _print_backends(registry)
        return 0

    parser.print_help()
    return 0
