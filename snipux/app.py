"""Controller, tray, and CLI entry point.

`build_default_registry()` wires the real capture backends in by asking
`platform.current.build_capture_registry()` (SNX-86) -- it used to pick
`capture.py`'s X11 or Wayland registry (or both, on an unrecognised session
type) itself, by branching on `detect_session_type()` directly, but that has
no answer at all on a platform with no notion of an X11/Wayland session
type. See `snipux/platform/__init__.py` for why that choice now lives
behind the platform seam instead.

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
    XwininfoWindowGeometryProvider,
    BackendRegistry,
    CaptureError,
    WindowsWindowGeometryProvider,
    X11WindowGeometryProvider,
    detect_session_type,
)
from snipux.overlay import (
    GeometryProvider,
    OverlayWindow,
    UnsupportedGeometryProvider,
    open_overlay,
)
from snipux import design, platform, setup_desktop
from snipux.platform.windows import HotkeyEventFilter, reattach_console
from snipux.review import ReviewWindow
from snipux.settings import SettingsDialog


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


# design.PACKAGE_DIR (SNX-96) rather than a second Path(__file__)-based
# guess: it already knows the difference between a source checkout/pip
# install and a PyInstaller bundle, and this logo ships inside the bundle
# the same way the rest of design/ does.
_LOGO_DIR = design.PACKAGE_DIR / "design" / "logo"


def load_app_icon() -> QIcon:
    """Build a multi-resolution QIcon from the vendored
    `design/logo/snipux-<size>.png` files -- the same PNGs
    `setup_desktop.install_icons()` copies into the user's hicolor icon
    theme. Adding every size lets Qt pick the closest match itself for
    whatever the tray, window titlebar, or Alt-Tab switcher asks for,
    rather than scaling a single pixmap up or down.

    Falls back to the small drawn placeholder this used to always be when
    the artwork directory is missing or none of the files in it load as a
    real image -- SNX-81's acceptance criterion is that a broken or
    absent icon must not stop the tray (and the whole resident process)
    from starting, the same "a failure must not stop the rest" rule
    CLAUDE.md states for capture backends, applied here.
    """
    icon = QIcon()
    if _LOGO_DIR.is_dir():
        for path in sorted(_LOGO_DIR.glob("snipux-*.png")):
            icon.addFile(str(path))

    if icon.isNull():
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(60, 110, 200))
        return QIcon(pixmap)
    return icon


def build_default_registry() -> BackendRegistry:
    """Construct the `BackendRegistry` the real app uses.

    Delegates to the platform seam (SNX-86) rather than branching on
    `detect_session_type()` here itself -- `platform.current` already knows
    what it can capture with (Linux's own Wayland/X11/both selection lives
    in `platform.linux.LinuxPlatform.build_capture_registry`; a platform
    with nothing implemented yet, like Windows or macOS, answers with a
    registry that says so rather than an empty one or a raised error).
    """
    return platform.current.build_capture_registry()


def build_default_geometry_provider() -> GeometryProvider:
    """Construct the `GeometryProvider` the real app uses for window mode.

    Tried in order, the same way capture backends are: `wmctrl` first for
    its curated window list, then `xwininfo` (x11-utils, which a GNOME
    session already has) so a stock Ubuntu without `wmctrl` still gets
    Window mode instead of silently falling back to Region, then
    `WindowsWindowGeometryProvider` (SNX-90), which answers unconditionally
    on `win32` since Windows has no "no client may enumerate other
    windows" restriction to work around in the first place. Wayland, or a
    machine with none of the above, gets `UnsupportedGeometryProvider` and
    degrades to plain rectangle dragging exactly as it does without a
    provider at all. The choice is made here rather than in overlay.py so
    overlay.py never needs to import anything platform-specific — per
    CLAUDE.md, platform-specific code stays confined to capture.py, and
    app.py is already the place that picks between platform-specific
    implementations for `registry`.
    """
    for provider in (
        X11WindowGeometryProvider(),
        XwininfoWindowGeometryProvider(),
        WindowsWindowGeometryProvider(),
    ):
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
        help="ask an already-running Snipux instance to start a capture, "
        "starting one first if none is running yet (for binding to a "
        "key such as Print Screen)",
    )
    group.add_argument(
        "--settings",
        action="store_true",
        help="open the Settings window -- asking an already-running Snipux "
        "instance to raise its own if there is one, starting one first if "
        "not (same rule --snip follows). The way in on a machine with no "
        "tray icon, e.g. stock GNOME without the AppIndicator extension",
    )
    group.add_argument(
        "--setup",
        action="store_true",
        help="install the desktop entry, autostart entry, and GNOME "
        "Super+Shift+S shortcut for this installed copy of Snipux -- no "
        "repository checkout needed",
    )
    group.add_argument(
        "--remove",
        action="store_true",
        help="undo everything --setup did -- the desktop entry, autostart "
        "entry, installed icons, GNOME shortcut and remembered shortcut "
        "choice -- run this before `pipx uninstall snipux` so nothing is "
        "left behind",
    )
    # Outside the mutually exclusive group above: this modifies --setup
    # rather than being an action of its own.
    parser.add_argument(
        "--shortcut",
        metavar="ACCELERATOR",
        help="with --setup, bind this accelerator instead of the default "
        f"{setup_desktop.DEFAULT_SHORTCUT}, and remember it so later "
        "--setup runs (every install.sh does one) keep it. GNOME "
        "accelerator syntax, e.g. '<Super><Shift>x' or '<Alt>Print'",
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

    if args.shortcut is not None and not args.setup:
        parser.error("--shortcut only means anything alongside --setup")

    if args.setup:
        # No registry/transport involved -- unlike --snip and the default
        # resident path, this never touches capture backends or a display,
        # so it's handled before either is ever built.
        #
        # Routed through the platform seam (snipux/platform/) rather than
        # calling setup_desktop directly: that used to be the only real
        # implementation (Windows/macOS both raised), so going through the
        # seam would have turned every `--setup` run during Windows-hosted
        # development into a crash. Windows now has its own real
        # implementation (SNX-92), so this reaches it instead of writing
        # meaningless `.desktop`/gsettings state under fake XDG paths on a
        # non-Linux host. macOS still raises `UnimplementedPlatformError`
        # naming itself and the operation -- the documented, intended
        # behaviour for a platform seam with nothing behind it yet (see
        # `snipux/platform/__init__.py`), not a regression this introduces.
        return platform.current.install_desktop_integration(shortcut=args.shortcut)

    if args.remove:
        # Same reasoning as --setup above: --remove only deletes what
        # --setup wrote, so it has no more use for a registry or a display
        # than --setup did.
        return platform.current.remove_desktop_integration()

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
        # Before try_claim(), never after -- see `_ensure_qapplication`.
        # This is the path that becomes resident when nothing is running
        # yet, so it needs the same ordering `run_resident_app` does.
        _ensure_qapplication()
        if transport.try_claim():
            if registry is None:
                registry = build_default_registry()
            return _become_resident(registry, transport, start_capture_immediately=True)
        transport.send_snip_request()
        return 0

    if args.settings:
        # SNX-78: the same forward-or-become-resident shape as --snip just
        # above, and for the same reason -- a standalone Settings window in
        # its own short-lived process could not rebind a live Windows
        # `RegisterHotKey` registration (that belongs to the one process
        # that is actually resident), and would just be a second,
        # disconnected window next to a GNOME session's already-running
        # instance. Reaching the one process that actually owns the tray
        # (or becoming it, if none is running yet) is what lets Save take
        # effect without a restart on every platform, not just GNOME's.
        if transport is None:
            transport = QLocalSocketTransport()
        _ensure_qapplication()
        if transport.try_claim():
            if registry is None:
                registry = build_default_registry()
            return _become_resident(registry, transport, open_settings_immediately=True)
        transport.send_settings_request()
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
    def send_settings_request(self) -> None:
        """Ask the primary instance to raise its Settings window (SNX-78).
        Same shape as `send_snip_request`, and for the same reason: a
        `--settings` launch that found an instance already resident hands
        the request to it rather than opening a second, disconnected
        window that could never rebind that instance's own live hotkey
        registration.
        """

    @abstractmethod
    def listen(
        self,
        on_snip_request: Callable[[], None],
        on_settings_request: Callable[[], None],
    ) -> None:
        """Primary-instance only: call `on_snip_request` for every snip
        request, and `on_settings_request` for every Settings request, that
        a later, non-primary launch forwards.
        """


class QLocalSocketTransport(Transport):
    """Real `Transport`, backed by `QtNetwork`'s `QLocalServer`/
    `QLocalSocket` bound to a fixed, well-known server name.

    Not a new dependency: `QtNetwork` ships inside the `PyQt6` wheel
    alongside `QtCore`/`QtGui`/`QtWidgets`, the same way capture.py already
    reaches `QtGui` without declaring it separately.

    The protocol is one byte: `_REQUEST_BYTE` means "take a snip",
    `_SETTINGS_REQUEST_BYTE` (SNX-78) means "raise the Settings window",
    and a connection that sends nothing is only asking whether anyone is
    home.

    That distinction is load-bearing, not ceremony. `try_claim()` probes by
    connecting, so when a bare connection *was* the request, every liveness
    check fired a capture on the resident -- including `--snip`'s own probe
    moments before its real request, which is why one keypress used to
    deliver two. Nothing worse than a duplicate happened only because
    `start_capture` ignores a request while an overlay is already open.
    """

    SERVER_NAME = "snipux-resident"
    _CONNECT_TIMEOUT_MS = 200
    _REQUEST_BYTE = b"S"
    _SETTINGS_REQUEST_BYTE = b"T"
    # Long enough that a request is never lost to scheduling, short enough
    # that a probe (which sends nothing) doesn't hold the handler up.
    _READ_TIMEOUT_MS = 200

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
        self._send(self._REQUEST_BYTE)

    def send_settings_request(self) -> None:
        self._send(self._SETTINGS_REQUEST_BYTE)

    def _send(self, payload: bytes) -> None:
        socket = QLocalSocket()
        socket.connectToServer(self._server_name)
        if not socket.waitForConnected(self._CONNECT_TIMEOUT_MS):
            return
        socket.write(payload)
        # Flushed before disconnecting: this process exits the moment
        # `main()` returns, and an unflushed byte dies with it -- the
        # request would be sent, accepted, and silently empty.
        socket.waitForBytesWritten(self._CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()

    def listen(
        self,
        on_snip_request: Callable[[], None],
        on_settings_request: Callable[[], None],
    ) -> None:
        if self._server is None:
            raise RuntimeError("listen() called before a successful try_claim()")

        def _accept() -> None:
            connection = self._server.nextPendingConnection()
            if connection is None:
                return
            # A probe sends nothing and simply times out here, which is
            # exactly how it stays distinguishable from either request.
            received = (
                connection.readAll().data()
                if connection.waitForReadyRead(self._READ_TIMEOUT_MS)
                else b""
            )
            connection.disconnectFromServer()
            if received.startswith(self._REQUEST_BYTE):
                on_snip_request()
            elif received.startswith(self._SETTINGS_REQUEST_BYTE):
                on_settings_request()

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
        # Held for the same reason `_overlay` is: a parentless widget is
        # fair game for the GC while its window is still on screen.
        self._settings: SettingsDialog | None = None
        self._reviews: list[ReviewWindow] = []
        # Set by install_hotkey_listener(), not here -- see its own
        # docstring for why registering the real Windows hotkey is kept out
        # of __init__.
        self.hotkey_filter: HotkeyEventFilter | None = None

        # Stock Ubuntu GNOME shows no legacy tray icon at all without the
        # AppIndicator extension: calling show() unconditionally left the
        # app resident -- holding the single-instance claim -- with no
        # icon, no menu, and no way to quit it except pkill (SNX-54). The
        # tray icon and menu are still built either way (showMessage() on
        # the icon is still a usable, if invisible, notification sink, and
        # the menu costs nothing unshown), but show() itself is gated on
        # this check.
        self._tray_available = QSystemTrayIcon.isSystemTrayAvailable()

        # The window icon is set here, not left to whatever an individual
        # window (the overlay, later a dialog) might set for itself, so
        # every window this process ever shows -- and the window
        # switcher/taskbar entry for it -- carries the real icon rather
        # than Qt's default, from the moment the app has a QApplication at
        # all.
        icon = load_app_icon()
        QApplication.instance().setWindowIcon(icon)

        self._tray_icon = QSystemTrayIcon(icon)
        menu = QMenu()
        # A single Snip item, not one per SelectionMode: OverlayWindow's own
        # capture-mode popover (CaptureModePopover, opened from its floating
        # bar's chip) is what picks Region/Window/Full screen/Freeform now,
        # so the tray no longer needs a separate entry point for each -- the
        # old per-mode menu existed only because the previous Overlay/Editor
        # pair couldn't change mode once a selection was already open.
        self.snip_action = menu.addAction("Snip")
        self.snip_action.triggered.connect(self.start_capture)
        self.settings_action = menu.addAction("Settings...")
        self.settings_action.triggered.connect(self.open_settings)
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
                "No system tray detected -- Snipux will run without a tray "
                "icon or menu. It is still listening for snip requests "
                "(e.g. via `snipux --snip`, typically bound to a key); to "
                "quit it, kill this process."
            )

        self._transport.listen(self.start_capture, self.open_settings)

    def open_settings(self) -> None:
        """Show the Settings window, or raise the one already open.

        Held on `self` for the same reason `_overlay` is: a parentless
        widget is fair game for Python's GC to collect out from under a
        window still on screen. Non-modal, so a snip can still be taken
        while it is open.
        """
        if self._settings is not None and self._settings.isVisible():
            self._settings.raise_()
            self._settings.activateWindow()
            return
        self._settings = SettingsDialog(on_saved=self._on_settings_saved)
        self._settings.show()
        self._settings.raise_()
        self._settings.activateWindow()

    def _on_settings_saved(self) -> None:
        """Apply what Settings just wrote.

        The shortcut has to be re-bound, not merely remembered -- the
        stored value is what survives the next `--setup` (Linux) or process
        restart (Windows), but neither GNOME nor a still-running
        `RegisterHotKey` registration knows about a change to it on their
        own. On Windows this goes through the platform seam for real
        (SNX-91's own acceptance criterion: changing the shortcut re-registers
        it without restarting the app) -- `HotkeyEventFilter.is_available()`
        is what tells the two paths apart, the same capability check
        `install_hotkey_listener()` uses, rather than this method branching
        on `sys.platform` itself. On Linux this still calls setup_desktop
        directly rather than through the seam, the same reasoning as
        `main()`'s `--setup`/`--remove` above: harmless (a printed note, not
        an exception) during Windows-hosted development.
        """
        if HotkeyEventFilter.is_available():
            message = platform.current.bind_shortcut()
        else:
            exec_path = setup_desktop.find_console_script()
            if exec_path is None:
                message = (
                    "Settings saved, but the snipux console script could not be "
                    "found, so the shortcut was not re-bound."
                )
            else:
                message = setup_desktop.bind_gnome_shortcut(exec_path)
        self._report_shortcut(message)

    def _report_shortcut(self, message: str) -> None:
        """Show a bind/rebind report to the user -- the tray (or stdout with
        no tray available), same as every other place this controller
        reports something the user didn't directly ask to see mid-flow.
        Shared by `_on_settings_saved()` and `install_hotkey_listener()`
        rather than each inlining the same tray-or-print check.
        """
        if self._tray_available:
            self._tray_icon.showMessage("Snipux", message, QSystemTrayIcon.MessageIcon.Information)
        else:
            print(message)

    def install_hotkey_listener(self) -> None:
        """Register the Windows global capture hotkey (defaulting to
        Ctrl+Alt+S) and start listening for it (SNX-91). A no-op everywhere
        else -- GNOME already owns the keybinding on Linux and invokes
        `snipux --snip` itself, so there is nothing here for this process to
        listen for.

        Called once by `app.py`'s `_become_resident()`, for the process
        that is actually going to stay resident -- not from `__init__`,
        which every test in test_app.py constructs directly. `RegisterHotKey`
        is a real OS-level registration for as long as this call holds it
        (the whole point, per CLAUDE.md and the ticket: the resident process
        owns the key while it runs), so it belongs to the one process that
        is really going to run, not to every `AppController` a test happens
        to build.

        A clash with another application's own hotkey -- the one failure
        mode `RegisterHotKey` documents -- is reported by name rather than
        swallowed; `platform.current.registered_shortcut` staying `None`
        afterwards is how that is told apart from success without parsing
        the report string (see `WindowsPlatform.bind_shortcut`).
        """
        if not HotkeyEventFilter.is_available():
            return

        self.hotkey_filter = HotkeyEventFilter(self.start_capture)
        QApplication.instance().installNativeEventFilter(self.hotkey_filter)
        # Best-effort: Windows already releases the registration itself the
        # moment this process's thread goes away, clean exit or not (see
        # WindowsPlatform.unbind_shortcut), so this is tidiness for a clean
        # Quit, not the mechanism a restart's re-registration depends on.
        QApplication.instance().aboutToQuit.connect(platform.current.unbind_shortcut)

        message = platform.current.bind_shortcut()
        if platform.current.registered_shortcut is None:
            self._report_shortcut(message)

    def run_first_launch_setup(self) -> None:
        """Install desktop integration once, the first time this process is
        ever the resident instance (SNX-95) -- so `snipux --setup` is not a
        step anyone has to remember to run before the app is actually
        usable.

        `setup_desktop.load_setup_complete()` is the record: once it says
        setup already ran, this returns immediately without rewriting
        anything. `--setup` (`main()`'s own dispatch, untouched by this)
        stays the explicit way to redo it after a move or an upgrade, and
        `--remove` deletes the same config file this record lives in, so a
        later launch sees no record and sets up again rather than assuming
        the install is still there.

        Called once by `_become_resident()`, the same place
        `install_hotkey_listener()` is and for the same reason: this is the
        process that is actually going to stay resident, not every
        `AppController` a test happens to construct directly.

        A platform with nothing behind the seam yet (macOS today) must not
        stop the app from starting just because it can't set itself up --
        `UnimplementedPlatformError` is caught and reported through
        `_report_shortcut` the same way any other step that can't run is,
        the "a failure must not stop the rest" rule CLAUDE.md states for
        capture backends, applied one level up here. Every other failure --
        no graphical session, no permission to write a shortcut -- is
        already a step-level note `install_desktop_integration()` itself
        prints and recovers from (see `setup_desktop.run_setup()`), so
        there is nothing left for this method to catch.
        """
        if setup_desktop.load_setup_complete():
            return

        try:
            platform.current.install_desktop_integration()
        except platform.UnimplementedPlatformError as exc:
            # Recorded anyway: retrying (and re-printing this note) on
            # every single launch would be worse than a user re-running
            # --setup once real support lands on this platform.
            setup_desktop.save_setup_complete(True)
            self._report_shortcut(str(exc))
            return

        setup_desktop.save_setup_complete(True)
        shortcut = setup_desktop.human_shortcut(setup_desktop.load_shortcut())
        self._report_shortcut(
            f"Snipux is set up: it starts at login, and {shortcut} starts a "
            "snip. Change the shortcut any time in Settings."
        )

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
            # open -- a tray double-click, or a second shortcut press while
            # already selecting. Opening a second overlay on top of the
            # first would be wrong, but so was the silent no-op this used to
            # be: an overlay the user has lost track of (they looked away,
            # or clicked onto another screen) turns every later press of the
            # shortcut into nothing at all, with no clue why. That is
            # indistinguishable from the keybinding having stopped working,
            # and was reported as exactly that.
            #
            # Showing the overlay they already have is both the honest
            # answer and the actionable one -- it is right there, and Esc
            # closes it. `_reveal` covers the case where the request landed
            # inside the window's own opacity-0 reveal delay, which would
            # otherwise raise something still invisible.
            self._overlay._reveal()
            self._overlay.raise_()
            self._overlay.activateWindow()
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
            # Read fresh for each snip, the same reasoning `_on_captured`'s
            # own `load_review_window()` call uses -- the Settings toggle
            # must take effect on the very next capture, not just the next
            # launch.
            hints_enabled=setup_desktop.load_hints_enabled(),
            geometry_provider=self._geometry_provider,
            # So a delayed capture (SNX-50's re-grab) and Window/Full
            # screen mode inside the overlay itself have the same registry
            # the initial capture above used, rather than an inert default
            # that could never actually re-capture anything.
            registry=self._registry,
            # SNX-62: the one place `self._overlay` is cleared -- see
            # `_on_overlay_dismissed` and the guard above.
            on_dismissed=self._on_overlay_dismissed,
            # Fires only for a real capture, never for a cancelled snip --
            # see `OverlayWindow._report_capture`.
            on_captured=self._on_captured,
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

    def _on_captured(self, image, path) -> None:
        """Open the review window for a finished snip, when it is turned on.

        Off unless asked for: the overlay already annotates in place, so a
        window after every capture is a change to the core flow rather than
        a default to inherit. Read fresh each time rather than cached at
        startup, so toggling it in Settings takes effect on the next snip
        instead of the next launch.
        """
        # The chooser is the authority: it is seeded from Settings when the
        # overlay opens, so its value is either what Settings says or what
        # the user changed it to for this snip. Either way it is the more
        # current answer, and per the handoff a per-snip override never
        # writes back to the stored preference.
        if self._overlay is not None:
            if self._overlay.outcome != "review":
                return
        elif not setup_desktop.load_review_window():
            return
        # A copy of the image, not the overlay's own: the overlay is about
        # to close, and `rendered_image()` hands back a QImage backed by
        # buffers it owns.
        review = ReviewWindow(image.copy(), saved_path=path)
        # Held for the same reason `_overlay` and `_settings` are -- a
        # parentless widget is fair game for the GC while it is on screen.
        # A list, not one slot: taking several snips in a row should leave
        # several windows open, which is most of the point of having one.
        self._reviews.append(review)
        review.closed.connect(lambda w=review: self._forget_review(w))
        review.show()
        review.raise_()
        review.activateWindow()

    def _forget_review(self, window) -> None:
        """Drop a closed review window, so a long session doesn't
        accumulate every snip it ever took.
        """
        if window in self._reviews:
            self._reviews.remove(window)

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


# The process's QApplication, kept alive here for as long as the process
# lives. PyQt collects a QApplication nothing holds a Python reference to,
# exactly like the parentless widgets elsewhere in this file -- and a
# collected QApplication takes the thread's event dispatcher with it, which
# is precisely the state `_ensure_qapplication` exists to avoid. A local
# variable is not enough: the one in `run_resident_app` goes out of scope
# between building the app and claiming the socket.
_QAPPLICATION: QApplication | None = None


def _ensure_qapplication() -> QApplication:
    """The process's `QApplication`, building one if there isn't one yet.

    Must be called before `Transport.try_claim()`, never after. `try_claim`
    builds a `QLocalServer` and starts it listening, and Qt gives a socket
    notifier created with no `QApplication` alive a thread with no event
    dispatcher. Constructing the `QApplication` afterwards does not adopt
    it -- it orphans it, prints "QSocketNotifier: current thread's event
    dispatcher has already been destroyed", and `newConnection` never fires
    again for the life of the process.

    That is not a cosmetic warning. It meant the resident accepted no
    forwarded requests at all, so `snipux --snip` -- and therefore the
    keyboard shortcut, its only real caller -- did nothing, silently and
    always, while the tray's own Snip item (a direct call, no socket) kept
    working and made it look like the shortcut alone was cursed.

    Reuses an existing instance rather than constructing unconditionally:
    PyQt raises on a second QApplication in one process, which is exactly
    what a test suite's shared fixture provides.
    """
    global _QAPPLICATION
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    # Held module-level, not merely returned: a caller that drops the
    # returned value -- which `run_resident_app` legitimately does, having
    # no further use for it before `_become_resident` asks again -- would
    # otherwise let it be collected between here and `try_claim()`.
    _QAPPLICATION = app
    return app


def _become_resident(
    registry: BackendRegistry,
    transport: Transport,
    start_capture_immediately: bool = False,
    open_settings_immediately: bool = False,
) -> int:
    """Build the `QApplication`/`AppController` and run the event loop.

    Assumes `transport.try_claim()` has already returned True for
    `transport` -- calling try_claim() a second time on the same transport
    would have it probe its own not-yet-listening state and misreport
    itself as not primary, so becoming primary is always a precondition
    here rather than something this function repeats.

    Shared by `run_resident_app()` (a bare `snipux` launch), `main()`'s
    `--snip` path, and its `--settings` path (SNX-78), whenever nothing was
    resident yet: refusing to start one there made whether Super+Shift+S
    did anything at all depend on invisible state the user had no way to
    see (SNX-53), and the same would be true of a `--settings` launch on a
    machine with no tray to fall back on. `start_capture_immediately`/
    `open_settings_immediately` are what tell the three callers apart -- a
    bare launch opens idle to the tray, `--snip` opens the overlay right
    away, and `--settings` opens Settings right away, each the same as if
    it had forwarded its request to an instance that was already up.

    `install_hotkey_listener()` runs here, once, for the same reason: this
    is the process that is actually resident, so it is the one that should
    hold the Windows global hotkey registration (SNX-91) -- not every
    `AppController` a test happens to construct directly.

    `run_first_launch_setup()` (SNX-95) runs here too, for the identical
    reason: whether desktop integration has ever run is a real, one-time
    action against the OS (writing files, binding a shortcut), not
    something every `AppController` a test builds should also trigger.

    Called in exactly this order, not the other way around: on a genuinely
    first-ever launch, `install_hotkey_listener()` has already bound the
    real shortcut by the time `run_first_launch_setup()` runs -- so the
    shortcut works immediately, with nothing left for a restart to pick up
    later (SNX-101). `WindowsPlatform.install_desktop_integration()`
    depends on this ordering too: it says nothing about when the shortcut
    takes effect, exactly because it never runs before this already has.

    `platform.current.ensure_stable_install()` (SNX-103) runs first, ahead
    of either: a portable Windows build relocates itself to a stable
    per-user location here, on *every* launch that gets this far, not just
    a first one -- unlike `run_first_launch_setup()` just below, which
    only ever calls into desktop integration once and then stays silent
    forever after, this has to keep running so that a newer download run
    over an already-set-up older install still replaces it. It has to
    happen before `run_first_launch_setup()` reaches
    `install_desktop_integration()`, too: that is what points the Start
    Menu/Startup shortcuts at the relocated copy instead of wherever this
    process happened to be launched from. A no-op everywhere this isn't a
    portable build (see `Platform.ensure_stable_install()`'s own
    docstring), so this costs every other platform nothing.
    """
    # Already built by whoever called `try_claim()` -- see
    # `_ensure_qapplication`, which must run before the claim, not after.
    app = _ensure_qapplication()

    platform.current.ensure_stable_install()

    controller = AppController(registry, transport)
    controller.install_hotkey_listener()
    controller.run_first_launch_setup()
    if start_capture_immediately:
        controller.start_capture()
    if open_settings_immediately:
        controller.open_settings()
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

    # Before try_claim(), never after -- see `_ensure_qapplication`.
    _ensure_qapplication()

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

    `reattach_console()` (SNX-100) runs first, before anything else here
    has a chance to `print()` -- this is the one function every launch
    path (`python -m snipux`, the pip-installed console script, and the
    windowed PyInstaller build) actually goes through, which is what makes
    it the right place for a step that has to happen before any of the
    below can safely write to stdout/stderr at all.
    """
    reattach_console()
    if sys.argv[1:]:
        return main()
    return run_resident_app()
