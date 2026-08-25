"""Controller, tray, and CLI entry point.

`build_default_registry()` wires the real capture backends in, selecting
`capture.py`'s X11 or Wayland registry (or both, on an unrecognised session
type) by `detect_session_type()`.

`copy_image_to_clipboard`/`save_image` also live here rather than in
`overlay.py`: this is the module with no existing reason to avoid
`subprocess`/`shutil`/filesystem code (`capture.py` already owns that
pattern for backends; `overlay.py` is scoped to widget/painting code).
`app.py` has no reason to import `overlay.py`'s `OverlayWindow.copy`/`save`,
so `overlay.py` importing these two functions from here (deferred, to avoid
a circular import) stays one-directional.
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
    X11WindowGeometryProvider,
    build_wayland_registry,
    build_x11_registry,
    detect_session_type,
)
from snipux.overlay import (
    GeometryProvider,
    OverlayWindow,
    UnsupportedGeometryProvider,
    open_overlay,
)
from snipux import setup_desktop


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


def build_default_geometry_provider() -> GeometryProvider:
    """Construct the `GeometryProvider` the real app uses for window mode.

    `X11WindowGeometryProvider` when its own `is_available()` says the
    session is X11 with `wmctrl` on PATH; `UnsupportedGeometryProvider`
    otherwise, so Wayland and a machine without `wmctrl` degrade to plain
    rectangle dragging exactly as they do without a provider at all. The
    choice is made here rather than in overlay.py so overlay.py never needs
    to import anything wmctrl-specific — per CLAUDE.md, platform-specific
    code stays confined to capture.py, and app.py is already the place that
    picks between platform-specific implementations for `registry`.
    """
    provider = X11WindowGeometryProvider()
    if provider.is_available():
        return provider
    return UnsupportedGeometryProvider()


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
        help="ask an already-running snipux instance to start a capture, "
        "starting one first if none is running yet (for binding to a "
        "key such as Print Screen)",
    )
    group.add_argument(
        "--setup",
        action="store_true",
        help="install the desktop entry, autostart entry, and GNOME "
        "Super+Shift+S shortcut for this installed copy of snipux -- no "
        "repository checkout needed",
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

    if args.setup:
        # No registry/transport involved -- unlike --snip and the default
        # resident path, this never touches capture backends or a display,
        # so it's handled before either is ever built.
        return setup_desktop.run_setup()

    if args.snip:
        # Forward to an already-resident instance when there is one. When
        # there isn't, this call's own try_claim() just became the primary
        # -- and, unlike before, it stays that way: it becomes the resident
        # instance itself and shows the overlay immediately, the same as if
        # it had forwarded to one that was already running. Abandoning the
        # claim here (the previous behaviour) is what made Super+Shift+S
        # depend on invisible state the user has no way to see (SNX-53):
        # nothing starts a resident instance on its own, so the very first
        # press after login -- or after any crash -- did nothing at all,
        # silently, because whoever pressed it had no way to know one
        # wasn't already running.
        if transport is None:
            transport = QLocalSocketTransport()
        if transport.try_claim():
            if registry is None:
                registry = build_default_registry()
            return _become_resident(registry, transport, start_capture_immediately=True)
        transport.send_snip_request()
        return 0

    if registry is None:
        registry = build_default_registry()

    if args.list_backends:
        _print_backends(registry)
        return 0

    parser.print_help()
    return 0


# -- resident app: single instance, tray icon, capture -> overlay --------
#
# Kept separate from main() above rather than repurposing bare `main([])`
# for this: main() is pinned by the tests above to an immediate,
# display-free return for every path except `--snip` becoming primary
# (which needs this same resident machinery -- see `_become_resident()`
# below), and repurposing all of main() for it would break the other,
# still-immediate paths' tests. `__main__.py` dispatches to
# `run_resident_app()` only when invoked with no arguments.


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
        if not self._server.listen(self._server_name):
            # Lost a race with another process's try_claim() landing
            # between our probe above and this listen() call — two presses
            # of the same keybinding in quick succession with nothing
            # running hit exactly this race. Whichever of us binds first
            # wins; the loser must report False, not True, or both would go
            # on to become primary and a second tray icon/overlay would
            # start.
            self._server = None
            return False
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
    """Owns the tray icon and the capture -> overlay wiring.

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
        geometry_provider: GeometryProvider | None = None,
    ):
        # Must happen before any overlay window is ever shown. Without it,
        # Qt's default behavior quits the whole application the moment the
        # last visible window closes — exactly what happens the first time
        # an overlay is dismissed without ink, which would kill the
        # resident process on the very first dismissal instead of
        # returning it to idle.
        QApplication.instance().setQuitOnLastWindowClosed(False)

        self._registry = registry
        self._transport = transport
        # None means "use real screen geometry"; tests inject synthetic
        # rects instead of depending on real screens under an offscreen
        # platform, mirroring the None -> "build the real thing" pattern
        # main() already uses for `registry`.
        self._monitor_geometries = monitor_geometries
        # Same None -> "build the real thing" pattern as `monitor_geometries`
        # above and `registry`/`transport` in main()/run_resident_app():
        # tests inject a fake provider directly instead of depending on a
        # real X11 session with wmctrl under an offscreen platform.
        self._geometry_provider = (
            geometry_provider
            if geometry_provider is not None
            else build_default_geometry_provider()
        )

        self._overlay: OverlayWindow | None = None

        # Stock Ubuntu GNOME shows no legacy tray icon at all without the
        # AppIndicator extension: calling show() unconditionally left the
        # app resident -- holding the single-instance claim -- with no
        # icon, no menu, and no way to quit it except pkill (SNX-54). The
        # tray icon and menu are still built either way (showMessage() on
        # the icon is still a usable, if invisible, notification sink, and
        # the menu costs nothing unshown), but show() itself is gated on
        # this check.
        self._tray_available = QSystemTrayIcon.isSystemTrayAvailable()

        self._tray_icon = QSystemTrayIcon(self._build_icon())
        menu = QMenu()
        # A single Snip item, not one per SelectionMode: OverlayWindow's own
        # capture-mode popover (CaptureModePopover, opened from its floating
        # bar's chip) is what picks Region/Window/Full screen/Freeform now,
        # so the tray no longer needs a separate entry point for each -- the
        # old per-mode menu existed only because the previous Overlay/Editor
        # pair couldn't change mode once a selection was already open.
        self.snip_action = menu.addAction("Snip")
        self.snip_action.triggered.connect(self.start_capture)
        self.quit_action = menu.addAction("Quit")
        self.quit_action.triggered.connect(self._quit)
        self._tray_icon.setContextMenu(menu)

        if self._tray_available:
            self._tray_icon.show()
        else:
            # Told once, on stdout, rather than left to be discovered by
            # `ps`/pkill: the keybinding (--snip) is still the way in with
            # no tray, but quitting needs a real answer now that there's no
            # Quit menu item visible to click.
            print(
                "No system tray detected -- snipux will run without a tray "
                "icon or menu. It is still listening for snip requests "
                "(e.g. via `snipux --snip`, typically bound to a key); to "
                "quit it, kill this process."
            )

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
        # No mode parameter: unlike the old per-monitor Overlay, a single
        # OverlayWindow starts in Region and lets its own capture-mode
        # popover switch to Window/Full screen/Freeform after the fact, so
        # every caller here -- the tray's own Snip action, the --snip
        # transport listener wired below, and a forwarded request from a
        # second launch -- needs no mode of its own to pass in.
        #
        # SNX-62: this used to read `self._overlay.isVisible()` instead of
        # just `is not None` -- but Copy and Save (before this ticket)
        # flattened the image and toasted without ever dismissing the
        # overlay, so `isVisible()` stayed True forever after either one and
        # every later snip request was silently refused for the rest of the
        # session. `_on_overlay_dismissed` below is now the single place
        # `self._overlay` is cleared, driven by `OverlayWindow`'s own
        # `on_dismissed` hook -- which fires exactly once, from
        # `closeEvent`, for every way a session actually ends (Copy, Save,
        # Enter, Esc) -- so this guard no longer depends on a widget
        # property that a future dismissal path could leave stale again.
        if self._overlay is not None:
            # A Snip request arrived while an overlay is genuinely still
            # open and in use (tray double-click, or a forwarded request
            # from a second launch while already selecting) — no-op rather
            # than opening a second overlay on top of the first.
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
            # the resident process otherwise never shows one -- except a
            # balloon message has nowhere to appear when there's no tray
            # icon shown to hang it off of (SNX-54), so that case falls
            # back to stdout instead of calling showMessage() into a void.
            if self._tray_available:
                self._tray_icon.showMessage(
                    "Snip failed",
                    str(exc),
                    QSystemTrayIcon.MessageIcon.Warning,
                )
            else:
                print(f"Snip failed: {exc}")
            return

        geometries = (
            self._monitor_geometries
            if self._monitor_geometries is not None
            else self._real_monitor_geometries()
        )
        # SNX-58: the session type is detected here, never assumed, and
        # handed to open_overlay() rather than each call site re-deriving
        # it -- the same pattern build_default_registry() already uses for
        # picking capture backends. A client can't position its own window
        # on Wayland, which is what open_overlay()'s own docstring (and
        # OverlayWindow.show_on_screen's) covers; X11 keeps behaving
        # exactly as it did before this ticket.
        overlay = open_overlay(
            frame,
            geometries,
            wayland=detect_session_type() == "wayland",
            geometry_provider=self._geometry_provider,
            # So a delayed capture (SNX-50's re-grab) and Window/Full
            # screen mode inside the overlay itself have the same registry
            # the initial capture above used, rather than an inert default
            # that could never actually re-capture anything.
            registry=self._registry,
            # SNX-62: the one place `self._overlay` is cleared -- see
            # `_on_overlay_dismissed` and the guard above.
            on_dismissed=self._on_overlay_dismissed,
        )
        # Stored on self, not left as a local: a parentless widget is fair
        # game for Python's GC to collect out from under the still-open
        # window otherwise, a known PyQt foot-gun. OverlayWindow manages
        # its own dismissal (Esc, Enter-to-copy, the bar's Copy/Save) and
        # closes itself; `_on_overlay_dismissed` is what tells this
        # controller that happened. open_overlay() already showed it (and
        # any Wayland multi-monitor veil companions), so there's no
        # separate .show() to call here.
        self._overlay = overlay

    def _on_overlay_dismissed(self) -> None:
        """Called once, by `OverlayWindow`'s own `on_dismissed` hook, the
        moment the current overlay's session actually ends -- Copy, Save,
        Enter, or Esc, every one of which routes through `closeEvent`
        (SNX-62). Clearing `self._overlay` here, rather than leaving
        `start_capture`'s guard to re-derive "is it still open" from
        `isVisible()`, is what makes a stale overlay unable to wedge this
        guard shut for the rest of the session: whatever ends the overlay,
        this is the one path that lets the next Snip request through.
        """
        self._overlay = None


def _become_resident(
    registry: BackendRegistry,
    transport: Transport,
    start_capture_immediately: bool = False,
) -> int:
    """Build the `QApplication`/`AppController` and run the event loop.

    Assumes `transport.try_claim()` has already returned True for
    `transport` -- calling try_claim() a second time on the same transport
    would have it probe its own not-yet-listening state and misreport
    itself as not primary, so becoming primary is always a precondition
    here rather than something this function repeats.

    Shared by `run_resident_app()` (a bare `snipux` launch) and `main()`'s
    `--snip` path when nothing was resident yet: refusing to start one
    there made whether Super+Shift+S did anything at all depend on
    invisible state the user had no way to see (SNX-53). Whether the
    overlay opens right away is what tells the two callers apart -- a bare
    launch opens idle to the tray, a `--snip` launch that just became
    primary shows the overlay immediately, the same as if it had forwarded
    to an instance that was already up.
    """
    # Reuse an already-running instance rather than unconditionally
    # constructing one: PyQt raises if a second QApplication is built in a
    # process that already has one, which is exactly the situation a test
    # suite's shared, module-scoped QApplication fixture creates. Mirrors
    # the same None-instance check the tests' own `qapp` fixture uses.
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    controller = AppController(registry, transport)
    if start_capture_immediately:
        controller.start_capture()
    return app.exec()


def run_resident_app(
    registry: BackendRegistry | None = None, transport: Transport | None = None
) -> int:
    """The real, resident entry point: builds the `QApplication`, enforces
    single-instance, and runs the event loop until Quit.

    If another instance is already running, forwards a capture request to
    it instead of starting a second tray icon, and returns immediately
    without starting an event loop of its own.
    """
    if registry is None:
        registry = build_default_registry()
    if transport is None:
        transport = QLocalSocketTransport()

    if not transport.try_claim():
        transport.send_snip_request()
        return 0

    return _become_resident(registry, transport)


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
