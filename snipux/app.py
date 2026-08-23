"""Controller, tray, and CLI entry point.

`build_default_registry()` wires the real capture backends in, selecting
`capture.py`'s X11 or Wayland registry (or both, on an unrecognised session
type) by `detect_session_type()`.

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
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QBuffer, QIODevice, QRectF
from PyQt6.QtGui import QColor, QGuiApplication, QIcon, QImage, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from snipux.capture import (
    BackendRegistry,
    CaptureError,
    build_wayland_registry,
    build_x11_registry,
    detect_session_type,
)
from snipux.overlay import Overlay, create_overlays


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

    Selects by `detect_session_type()`: Wayland gets
    `build_wayland_registry()`, X11 gets `build_x11_registry()`. An
    unrecognised session type gets both, concatenated -- every backend
    already gates itself with its own `is_available()`, so offering both
    lets whatever is actually installed be found instead of failing
    outright because the session type couldn't be determined.
    """
    session_type = detect_session_type()
    if session_type == "wayland":
        return build_wayland_registry()
    if session_type == "x11":
        return build_x11_registry()

    registry = BackendRegistry()
    for backend in build_wayland_registry():
        registry.add(backend)
    for backend in build_x11_registry():
        registry.add(backend)
    return registry


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snipux",
        description="A Windows Snipping Tool workalike for Linux.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--list-backends",
        action="store_true",
        help="print each registered capture backend's name, availability, "
        "and reason if unavailable",
    )
    group.add_argument(
        "--snip",
        action="store_true",
        help="ask an already-running snipux instance to start a capture "
        "(for binding to a key such as Print Screen); fails if no "
        "instance is running",
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


def main(
    argv: list[str] | None = None,
    registry: BackendRegistry | None = None,
    transport: "Transport | None" = None,
) -> int:
    """CLI entry point. Accepts an optional `registry` to stay testable
    without needing real backend availability on the machine running the
    tests, and an optional `transport` (same DI shape) for `--snip`.
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

    if args.snip:
        # A trigger-and-exit path, same spirit as the rest of main(): it
        # never starts an event loop. Only forward to an already-resident
        # instance; if none is running, this call's own try_claim() just
        # became the (empty) primary, which we deliberately abandon rather
        # than growing into a full AppController here -- that would make
        # the flag behave differently depending on timing, exactly what a
        # keybinding must not do. The QLocalServer it created is not a
        # leak: this process exits without ever calling listen() on it, so
        # the next real launch's try_claim() finds nothing live and
        # reclaims the name normally.
        if transport is None:
            transport = QLocalSocketTransport()
        if transport.try_claim():
            print(
                "no snipux instance is running -- start it first",
                file=sys.stderr,
            )
            return 1
        transport.send_snip_request()
        return 0

    if registry is None:
        registry = build_default_registry()

    if args.list_backends:
        _print_backends(registry)
        return 0

    parser.print_help()
    return 0


# -- resident app: single instance, tray icon, capture -> overlay -> editor -
#
# Kept separate from main() above rather than repurposing bare `main([])`
# for this: main() is pinned by the tests above to an immediate,
# display-free return, and repurposing it would break them. `__main__.py`
# dispatches to `run_resident_app()` only when invoked with no arguments.


class Transport(ABC):
    """Single-instance coordination and minimal cross-process signaling.

    Same DI shape the codebase already uses for `BackendRegistry` (main's
    `registry` param) and `GeometryProvider` (overlay.py) — a real
    implementation for production, a fake one for tests, both satisfying
    this shape and injected rather than constructed internally.
    """

    @abstractmethod
    def try_claim(self) -> bool:
        """Attempt to become the primary (resident) instance.

        Returns True if this call is now the primary instance, False if
        another instance already holds that role.
        """

    @abstractmethod
    def send_snip_request(self) -> None:
        """Ask the primary instance to run a capture. Called by a
        non-primary launch, after its own `try_claim()` returned False.
        """

    @abstractmethod
    def listen(self, on_request: Callable[[], None]) -> None:
        """Primary-instance only: call `on_request` once for every snip
        request a later, non-primary launch forwards.
        """


class QLocalSocketTransport(Transport):
    """Real `Transport`, backed by `QtNetwork`'s `QLocalServer`/
    `QLocalSocket` bound to a fixed, well-known server name.

    Not a new dependency: `QtNetwork` ships inside the `PyQt6` wheel
    alongside `QtCore`/`QtGui`/`QtWidgets`, the same way capture.py already
    reaches `QtGui` without declaring it separately.

    The protocol is deliberately minimal: a successful connection to the
    server *is* the capture request — there is exactly one kind of request
    today, so no framed message needs parsing.
    """

    SERVER_NAME = "snipux-resident"
    _CONNECT_TIMEOUT_MS = 200

    def __init__(self, server_name: str = SERVER_NAME):
        self._server_name = server_name
        self._server: QLocalServer | None = None

    def try_claim(self) -> bool:
        probe = QLocalSocket()
        probe.connectToServer(self._server_name)
        if probe.waitForConnected(self._CONNECT_TIMEOUT_MS):
            # A live server answered: another instance already owns this
            # name.
            probe.disconnectFromServer()
            return False

        # No live server answered: become it. A stale socket file left
        # behind by a crashed previous instance persists on disk on Linux
        # after an unclean exit, and would otherwise make every future
        # try_claim() falsely report "already running" forever — so this
        # must run immediately before listen(), not deferred to shutdown,
        # which may never run on a crash.
        QLocalServer.removeServer(self._server_name)
        self._server = QLocalServer()
        self._server.listen(self._server_name)
        return True

    def send_snip_request(self) -> None:
        socket = QLocalSocket()
        socket.connectToServer(self._server_name)
        socket.waitForConnected(self._CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()

    def listen(self, on_request: Callable[[], None]) -> None:
        if self._server is None:
            raise RuntimeError("listen() called before a successful try_claim()")

        def _accept() -> None:
            connection = self._server.nextPendingConnection()
            if connection is not None:
                connection.disconnectFromServer()
            on_request()

        self._server.newConnection.connect(_accept)


class AppController:
    """Owns the tray icon and the capture -> overlay -> editor wiring.

    Built to be testable without ever calling `QApplication.exec()`:
    nothing in `__init__` or its methods blocks, so `run_resident_app()` is
    a thin wrapper that constructs one of these and calls `.exec()`, and
    every test in test_app.py talks to the controller directly instead.
    """

    def __init__(
        self,
        registry: BackendRegistry,
        transport: Transport,
        monitor_geometries: list[QRectF] | None = None,
    ):
        # Must happen before any overlay/editor window is ever shown.
        # Without it, Qt's default behavior quits the whole application the
        # moment the last visible window closes — exactly what happens the
        # first time an overlay is cancelled, which would kill the resident
        # process on the very first cancel instead of returning it to idle.
        QApplication.instance().setQuitOnLastWindowClosed(False)

        self._registry = registry
        self._transport = transport
        # None means "use real screen geometry"; tests inject synthetic
        # rects instead of depending on real screens under an offscreen
        # platform, mirroring the None -> "build the real thing" pattern
        # main() already uses for `registry`.
        self._monitor_geometries = monitor_geometries

        self._overlays: list[Overlay] = []
        self._frame = None
        self._editor = None

        self._tray_icon = QSystemTrayIcon(self._build_icon())
        menu = QMenu()
        self.snip_action = menu.addAction("Snip")
        self.snip_action.triggered.connect(self.start_capture)
        self.quit_action = menu.addAction("Quit")
        self.quit_action.triggered.connect(self._quit)
        self._tray_icon.setContextMenu(menu)
        self._tray_icon.show()

        self._transport.listen(self.start_capture)

    def _build_icon(self) -> QIcon:
        # No icon asset exists in this repo, and adding one would be
        # unnecessary churn against CLAUDE.md's small, deliberate
        # dependency list — built programmatically instead.
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(60, 110, 200))
        return QIcon(pixmap)

    def _quit(self) -> None:
        QApplication.instance().quit()

    def _real_monitor_geometries(self) -> list[QRectF]:
        return [QRectF(screen.geometry()) for screen in QGuiApplication.screens()]

    def start_capture(self) -> None:
        if self._overlays:
            # A Snip request arrived mid-selection (tray double-click, or a
            # forwarded request from a second launch while already
            # selecting) — no-op rather than stacking a second overlay set
            # on top of the first.
            return

        try:
            frame = self._registry.capture()
        except CaptureError as exc:
            # A failed capture must not take down the resident process,
            # same "a failure must not stop the rest" spirit CLAUDE.md
            # states for backends, applied one level up here -- but silently
            # returning to idle left a Print Screen press with no feedback
            # at all, indistinguishable from the key doing nothing. Reported
            # through the existing tray icon rather than a new window, since
            # the resident process otherwise never shows one.
            self._tray_icon.showMessage(
                "Snip failed",
                str(exc),
                QSystemTrayIcon.MessageIcon.Warning,
            )
            return

        geometries = (
            self._monitor_geometries
            if self._monitor_geometries is not None
            else self._real_monitor_geometries()
        )
        overlays = create_overlays(frame, geometries)
        for overlay in overlays:
            overlay.confirmed.connect(self._on_confirmed)
            overlay.cancelled.connect(self._on_cancelled)
            overlay.show()
        # Stored on self, not left as a local: a parentless widget is fair
        # game for Python's GC to collect out from under the still-open
        # window otherwise, a known PyQt foot-gun.
        self._overlays = overlays
        self._frame = frame

    def _close_overlays(self) -> None:
        # All of them, not just the one that emitted — create_overlays()
        # makes one Overlay per monitor, and only one can end up confirming
        # or cancelling a given drag.
        for overlay in self._overlays:
            overlay.close()
        self._overlays = []

    def _on_confirmed(self, rect: QRectF, path) -> None:
        # Imported here, not at module level: editor.py imports
        # copy_image_to_clipboard/save_image from this module, so importing
        # Editor at the top of this file would be a circular import at
        # load time. By the time a signal can fire, both modules are
        # already fully loaded.
        from snipux.editor import Editor

        frame = self._frame
        self._close_overlays()
        self._frame = None

        # The freeform `path` is accepted but unused: Editor/Canvas take a
        # Frame only, and nothing in this ticket needs freeform-aware
        # cropping.
        cropped = frame.crop(rect)
        self._editor = Editor(cropped)
        self._editor.show()

    def _on_cancelled(self) -> None:
        self._close_overlays()
        self._frame = None


def run_resident_app(
    registry: BackendRegistry | None = None, transport: Transport | None = None
) -> int:
    """The real, resident entry point: builds the `QApplication`, enforces
    single-instance, and runs the event loop until Quit.

    If another instance is already running, forwards a capture request to
    it instead of starting a second tray icon, and returns immediately
    without starting an event loop of its own.
    """
    # Reuse an already-running instance rather than unconditionally
    # constructing one: PyQt raises if a second QApplication is built in a
    # process that already has one, which is exactly the situation a test
    # suite's shared, module-scoped QApplication fixture creates. Mirrors
    # the same None-instance check the tests' own `qapp` fixture uses.
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    if registry is None:
        registry = build_default_registry()
    if transport is None:
        transport = QLocalSocketTransport()

    if not transport.try_claim():
        transport.send_snip_request()
        return 0

    controller = AppController(registry, transport)
    return app.exec()


def cli() -> int:
    """The `console_scripts` entry point (`pyproject.toml` points
    `snipux` at this).

    A `console_scripts` entry point calls `module:function()` directly, so
    it cannot execute an `if __name__ == "__main__":` block -- this
    reproduces `__main__.py`'s dispatch rule (arguments present -> the
    display-free CLI diagnostics in `main()`; none -> the resident,
    tray-icon app) as an importable function instead.
    """
    if sys.argv[1:]:
        return main()
    return run_resident_app()
