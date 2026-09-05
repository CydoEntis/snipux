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

`copy_file_to_clipboard`/`finish_recording` sit next to them for the same
reason: recording has no bitmap to put on the clipboard, only a file, so
copying it is a sibling function rather than a branch inside
`copy_image_to_clipboard`.
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import (
    QBuffer,
    QElapsedTimer,
    QEventLoop,
    QIODevice,
    QMimeData,
    QObject,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QWidget

from snipux.flowbars import CountdownNumeral, FlowMenu, RecordingBar, RegionFrame
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
from snipux.recording import RecorderRegistry, RecordingError
from snipux.player import PlayerWindow
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


def finish_recording(path: Path, after: str) -> None:
    """Carry out whichever of recording's two destinations `after` names.

    The backends behind `platform.current.build_recording_registry()`
    already write straight to `path` while recording runs, so by the time
    this is called the file exists on disk either way -- there is no
    flatten-then-write step here the way stills has. "Instant" copies the
    finished file; "Save" is a no-op, because the file is already where it
    needs to be.

    Written as "only 'instant' acts" rather than "only 'save' is inert" so
    that a third destination added later stays inert here by default
    instead of silently being treated as a copy. "open" is inert for
    exactly that reason: it lands the file the way "save" does and the
    window is opened by the controller, which is the only thing that can
    own a window.
    """
    if after == "instant":
        copy_file_to_clipboard(path)


# What the pill reads between the Start press and the recorder actually
# running. Not a clock: see `_show_recording_chrome`.
_STARTING_LABEL = "Starting"

# How long to let the recording outline reach the screen before starting
# the backend anyway. It normally needs a frame or two; this only has to
# be long enough that a slow compositor is not mistaken for a stuck one.
_CHROME_PAINT_BUDGET_MS = 150


class _RecorderStarter(QObject):
    """Runs a recorder's blocking `start()` off the UI thread.

    `org.gnome.Shell.Screencast.ScreencastArea` takes ~500ms to answer --
    measured, and reproduced with `gdbus`, so it is Shell building its
    capture pipeline rather than anything snipux does. Called from the UI
    thread that whole time, the window stops painting and every control
    stops responding: reported as the app feeling stuck between pressing
    Record and recording.

    This does not make the recording start any sooner. Nothing can: the
    first frame lands when Shell says it does. It only stops the interface
    dying while that happens.

    The connection the backend opens therefore belongs to the worker thread
    and is used from the UI thread afterwards, by `stop()`. That is safe
    because it is a plain socket used strictly one thread at a time -- the
    signal below is what orders the two -- and not because jeepney promises
    anything about concurrent use, which it does not.
    """

    done = pyqtSignal()

    def __init__(self, registry, rect, path):
        super().__init__()
        self._registry = registry
        self._rect = rect
        self._path = path
        self.result: tuple | None = None
        self.error: Exception | None = None

    def run(self) -> None:
        """Worker thread. Never touches a widget."""
        try:
            self.result = self._registry.start(self._rect, self._path)
        except Exception as exc:  # noqa: BLE001 - handed back, not swallowed
            self.error = exc
        # Queued, because this object lives on the UI thread: the receiver
        # runs there, which is what makes the result safe to read.
        self.done.emit()


def _recording_temp_dir() -> Path:
    """The one dedicated subdirectory a recording's working file lives
    under while it is in progress (recording.md ticket 9) -- created on
    demand, not assumed to exist.

    Not the bare system temp dir a placeholder `NamedTemporaryFile` used to
    land in directly: a dedicated subdirectory is what makes "delete every
    file already here at startup" (see `AppController.__init__`) mean
    "recordings this app itself left behind", rather than every other
    application's litter in `/tmp`.
    """
    directory = Path(tempfile.gettempdir()) / "snipux-recording"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _clean_up_crashed_recording_temp_files() -> None:
    """Delete every file already present in `_recording_temp_dir()`.

    Called once, at `AppController` construction, before anything else
    runs -- only one recording is ever active at a time and this process
    wasn't running a moment ago, so anything already there is necessarily
    a crash leftover (the process died mid-recording, or between finishing
    the backend and landing the file), not a live recording something else
    still needs.
    """
    for leftover in _recording_temp_dir().iterdir():
        if leftover.is_file():
            leftover.unlink(missing_ok=True)


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


# The tray renders at one small size regardless of how many resolutions
# `icon` carries -- unlike the taskbar/window-switcher use `load_app_icon`'s
# own docstring is about -- so a single fixed size is representative enough
# to paint the recording dot onto.
_TRAY_ICON_PIXMAP_SIZE = 64


def _build_recording_tray_icon(icon: QIcon) -> QIcon:
    """Derive the "recording" tray icon from the idle one: the same
    artwork with a filled dot painted over it, in
    `design.tokens.Color.DANGER_SOLID` -- the same red `overlay.py`'s own
    "Clear ink" button already uses for its danger/alert state (there as
    the translucent `DANGER_BG`; this dot needs it opaque, hence the
    sibling token -- see `DANGER_SOLID`'s own comment in tokens.py), rather
    than inventing a new colour.

    Built once, in `AppController.__init__`, and swapped for the idle icon
    (`self._idle_tray_icon`) whenever a recording starts/stops -- not
    redrawn per recording.
    """
    base = icon.pixmap(_TRAY_ICON_PIXMAP_SIZE, _TRAY_ICON_PIXMAP_SIZE)
    pixmap = QPixmap(base)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(design.color("DANGER_SOLID"))
    diameter = round(pixmap.width() * 0.42)
    painter.drawEllipse(
        pixmap.width() - diameter, pixmap.height() - diameter, diameter, diameter
    )
    # Closed before the QIcon below reads the pixmap -- never leave a
    # QPainter open across a read of the pixmap it is painting, per
    # CLAUDE.md.
    painter.end()

    return QIcon(pixmap)


def _screen_for(rect: QRectF | None, geometries: list[QRectF]) -> QRectF | None:
    """The monitor a recording of `rect` belongs to, or None if there are
    no geometries to choose from.

    Chosen by the recorded area's centre so a recording on a second
    display gets its pill on that display, rather than on whichever
    monitor happens to come first. `rect is None` is a full-screen
    recording, which has no centre worth testing -- the first geometry
    (the primary screen, in the order Qt reports them) is the answer.

    A rect spanning two monitors, or one whose centre lands in the gap
    between two non-adjacent ones, falls back to the union of them all:
    the only rect guaranteed to contain the recorded area.
    """
    if not geometries:
        return None
    if rect is None:
        return geometries[0]
    centre = rect.center()
    for geometry in geometries:
        if geometry.contains(centre):
            return geometry
    union = geometries[0]
    for geometry in geometries[1:]:
        union = union.united(geometry)
    return union


def _place_recording_hud(
    rect: QRectF | None,
    geometries: list[QRectF],
    hud_size: QSize,
    margin: float = 12.0,
) -> QRect | None:
    """Find a spot for the recording pill: top-centre of the screen the
    recording is on, moved clear of the recorded area when that is where
    the recording happens to be.

    Top-centre because that is where the chooser and the floating bar
    already sit, so the bar turns up where the user is already looking --
    and because the handoff's own rule ("centred on the selection, 16px
    below") assumes the overlay is still drawn around that selection, which
    it is not by the time a recording is live. See
    docs/design/flow/divergences.md 4: the overlay paints a frozen frame,
    so it cannot stay up over a recording, and a bar measured from a
    selection nobody can see any more is the "floats in the middle of the
    screen" complaint again.
    The rule this replaces took the first of below/above/right/left of the
    recorded rect that fit, each centred on that edge -- which put the pill
    in the middle of the screen for any region in the middle of the screen,
    with nothing tying its position to anywhere predictable.

    Staying clear of `rect` is what keeps the pill out of the recording
    itself, and is why top-centre is a preference rather than a rule: a
    region that covers the top of the screen would otherwise be filmed with
    the pill sitting in it. Below the recorded area is the only fallback,
    and there is deliberately no "above" one: the fallbacks are reached
    only when the recorded area covers the top-centre strip, and anything
    that does leaves no room above itself on the same screen by
    definition. `None` means nothing fits and the caller shows no pill.

    `rect is None` (a full-screen recording) still gets a placement, unlike
    the previous version, which returned None and made "no pill in a
    full-screen recording" true by construction. Arming needs a visible
    Start for a full-screen recording too, so that rule now lives in
    `_start_recording_ui`, which takes the pill down at the moment
    recording actually begins -- the first moment it could contaminate
    anything.
    """
    screen = _screen_for(rect, geometries)
    if screen is None:
        return None

    width, height = hud_size.width(), hud_size.height()
    x = screen.center().x() - width / 2
    x = min(max(x, screen.left()), screen.right() - width)

    # The desktop's own chrome owns the top of the screen and paints over
    # an always-on-top window regardless of what that window thinks it
    # covers -- the same thing `OverlayWindow._reserved_top` insets the
    # chooser and close button for. Without it the bar sits at y=12 on a
    # GNOME primary monitor, which is underneath a 32px shell bar: created,
    # positioned, shown, and invisible.
    top = screen.top() + _reserved_top_for(screen) + margin
    candidates = [QRectF(x, top, width, height)]
    if rect is not None:
        candidates.append(QRectF(x, rect.bottom() + margin, width, height))

    for candidate in candidates:
        if not screen.contains(candidate):
            continue
        if rect is not None and candidate.intersects(rect):
            continue
        return QRect(round(candidate.x()), round(candidate.y()), width, height)

    # Nothing fits beside the recorded area on its own screen, which is the
    # ordinary case for recording a whole monitor: top-centre is inside it
    # and "below" falls off the bottom. A second monitor is then the best
    # home there is -- it is guaranteed unfilmed, because only one screen
    # is being recorded, and the bar is the only visible Stop there is.
    #
    # Without this a full-monitor recording had no Stop button at all, and
    # was reported exactly that way: "theres no way to stop the recording,
    # i dont even see the recording button".
    for other in _other_screens_nearest_first(screen, geometries, rect):
        x = min(max(other.center().x() - width / 2, other.left()),
                other.right() - width)
        candidate = QRectF(
            x, other.top() + _reserved_top_for(other) + margin, width, height
        )
        if other.contains(candidate) and not (
            rect is not None and candidate.intersects(rect)
        ):
            return QRect(round(candidate.x()), round(candidate.y()), width, height)
    return None


def _reserved_top_for(screen: QRectF) -> int:
    """Logical pixels of `screen`'s top edge the desktop's own chrome owns.

    Resolved through `QGuiApplication.screenAt` so the platform seam can
    answer for the right monitor -- and zero whenever it cannot be
    identified, which is the same safe direction `OverlayWindow` takes: the
    cost of being wrong is a bar a few pixels high, never one drawn into a
    recording.
    """
    found = QGuiApplication.screenAt(screen.center().toPoint())
    if found is None:
        return 0
    try:
        return platform.current.reserved_top(found)
    except Exception:  # noqa: BLE001 - chrome placement, never worth raising for
        return 0


def _other_screens_nearest_first(
    recorded: QRectF, geometries: list[QRectF], rect: QRectF | None
) -> list[QRectF]:
    """Every monitor except the one being recorded, nearest first.

    Nearest so the bar turns up beside the recording rather than three
    displays away, where it is a Stop button nobody looks at. Any monitor
    the recorded area reaches into is excluded outright -- a bar there
    would be in the file, which is the one thing placement may never do.
    """
    others = [
        screen
        for screen in geometries
        if screen != recorded and not (rect is not None and screen.intersects(rect))
    ]
    return sorted(
        others,
        key=lambda screen: abs(screen.center().x() - recorded.center().x())
        + abs(screen.center().y() - recorded.center().y()),
    )


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
        description="Snip, annotate and record your screen -- a Snipping Tool "
        "workalike for Linux and Windows.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--list-backends",
        action="store_true",
        help="print each registered capture and recording backend's name, "
        "availability, and reason if unavailable -- the way to ask whether "
        "this machine can snip and whether it can record, which are "
        "separate questions with separate answers",
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
        help="install desktop integration for this installed copy of "
        "Snipux -- no repository checkout needed. On Linux: the desktop "
        "entry, the autostart entry and the GNOME shortcut. On Windows: a "
        "Start Menu shortcut, a Startup entry and the registered hotkey. "
        f"The shortcut is {setup_desktop.DEFAULT_SHORTCUT} unless "
        "--shortcut says otherwise",
    )
    group.add_argument(
        "--remove",
        action="store_true",
        help="undo everything --setup did -- the desktop/Start Menu entry, "
        "autostart entry, installed icons, bound shortcut and remembered "
        "shortcut choice -- run this before `pipx uninstall snipux` so "
        "nothing is left behind",
    )
    group.add_argument(
        "--update",
        action="store_true",
        help="fetch and install the newest Snipux from GitHub, then report "
        "what to restart -- the same pip command the README gives, without "
        "anyone having to keep a URL",
    )
    # Outside the mutually exclusive group above: this modifies --setup
    # rather than being an action of its own.
    parser.add_argument(
        "--shortcut",
        metavar="ACCELERATOR",
        help="with --setup, bind this accelerator instead of the default "
        f"{setup_desktop.DEFAULT_SHORTCUT}, and remember it so later "
        "--setup runs (every install.sh does one) keep it. Either "
        "spelling is accepted -- the readable 'Super+Shift+X' or "
        "gsettings' '<Super><Shift>x' -- since normalise_shortcut() "
        "reduces both to the same canonical form",
    )
    return parser


def _print_backends(registry, heading: str | None = None) -> None:
    """Print one registry's backends. `registry` is a `BackendRegistry` or
    a `RecorderRegistry` -- this only needs `__len__`/`__iter__` and the
    name/availability trio both already satisfy, so recording backends are
    reportable without a second copy of this.
    """
    if heading is not None:
        print(heading)
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


# Where `--update` installs from. `main` rather than a release tag: there
# are no tagged releases, and this is the same URL the README hands out, so
# the two cannot describe different things.
UPDATE_URL = (
    "https://github.com/CydoEntis/snipux/archive/refs/heads/main.tar.gz"
)


def run_update(runner=None) -> int:
    """`snipux --update`: install the newest Snipux over this one.

    Exists because the alternative was asking people to keep a
    seventy-character URL somewhere they could find it again. The command
    it runs is exactly what the README documents -- it is not a second
    update mechanism, it is the same one with something memorable in front
    of it.

    `sys.executable -m pip`, never a bare `pip`: the interpreter running
    Snipux is by definition the environment Snipux is installed into, and a
    `pip` on PATH may well belong to a different one -- which would report
    a cheerful success and upgrade nothing the user is running.

    `--upgrade` and not `--force-reinstall`. Both would fetch the newer
    code; only the first leaves the dependencies alone, and forcing would
    re-download the whole of Qt on every update.

    Refuses outright in a PyInstaller build, where `sys.executable` is
    snipux.exe rather than an interpreter: `-m pip` would fail there with
    something unreadable, and a frozen build is replaced by downloading a
    new one, not by pip.

    `runner` is the subprocess call, injected so the tests can assert on
    the command without this actually reaching the network or the
    filesystem.
    """
    if getattr(sys, "frozen", False):
        print(
            "This is a standalone build, which pip cannot update. Download "
            "the newest snipux.exe and run it -- it replaces this copy."
        )
        return 1

    command = [sys.executable, "-m", "pip", "install", "--upgrade", UPDATE_URL]
    print(f"Updating from {UPDATE_URL}")
    run = runner if runner is not None else subprocess.call
    try:
        code = run(command)
    except OSError as exc:
        # No pip, or an interpreter that cannot be re-executed. A step-level
        # note, not a traceback, the same way every other thing that cannot
        # run in this file reports itself.
        print(f"Could not run pip: {exc}")
        return 1

    if code != 0:
        print("Update failed -- nothing was changed.")
        return code

    print(
        "Updated. Quit Snipux from the tray and press your capture shortcut "
        "to start the new version."
    )
    return 0


def main(
    argv: list[str] | None = None,
    registry: BackendRegistry | None = None,
    transport: "Transport | None" = None,
    recorder_registry=None,
) -> int:
    """CLI entry point. Accepts an optional `registry` to stay testable
    without needing real backend availability on the machine running the
    tests, and an optional `transport` (same DI shape) for `--snip`.

    `recorder_registry` is the same DI shape again, for `--list-backends`.
    Injected rather than always built from `platform.current` so the tests
    below assert against backends they name themselves -- SNX-126's rule,
    that a test must not pass or fail by what the machine running it
    happens to have.
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

    if args.update:
        # Display-free, like --setup/--remove above, and handled before any
        # registry or transport is built.
        return run_update()

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
        _print_backends(registry, heading="capture backends:")
        if recorder_registry is None:
            recorder_registry = platform.current.build_recording_registry()
        print()
        _print_backends(recorder_registry, heading="recording backends:")
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
        recorder_registry: RecorderRegistry | None = None,
        disk_usage: Callable[[str], object] = shutil.disk_usage,
    ):
        # Before anything else -- including the QApplication tweak right
        # below -- so a previous run of this same process that died
        # mid-recording never gets a chance to have its leftover temp file
        # mistaken for a live one by anything constructed after this line
        # (recording.md ticket 9).
        _clean_up_crashed_recording_temp_files()

        # Must happen before any overlay window is ever shown. Without it,
        # Qt's default behavior quits the whole application the moment the
        # last visible window closes — exactly what happens the first time
        # an overlay is dismissed without ink, which would kill the
        # resident process on the very first dismissal instead of
        # returning it to idle.
        QApplication.instance().setQuitOnLastWindowClosed(False)

        # Constructor-injectable, defaulting to the real thing -- same
        # 'factory the test can swap' shape `WindowsRecorderBackend` already
        # uses for its Qt objects, so a test can fake low free space without
        # touching a real disk. See `_check_recording_disk_space`.
        self._disk_usage = disk_usage

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

        # Same None -> "build the real thing" pattern as `geometry_provider`
        # above, but a platform with nothing behind this seam yet (macOS
        # today) must not stop `AppController` from constructing at all --
        # the same "a failure must not stop the rest" rule
        # `run_first_launch_setup`'s own `UnimplementedPlatformError`
        # handling already follows for desktop integration, applied here.
        # `_recorder_registry` stays None in that case and the remembered
        # message is what `_on_recording_requested` reports later, rather
        # than raising out of `__init__` the moment a snip is actually
        # requested.
        self._recorder_registry: RecorderRegistry | None = None
        self._recorder_unavailable_message: str | None = None
        if recorder_registry is not None:
            self._recorder_registry = recorder_registry
        else:
            try:
                self._recorder_registry = platform.current.build_recording_registry()
            except platform.UnimplementedPlatformError as exc:
                self._recorder_unavailable_message = str(exc)

        # Ticket 8 (the recording HUD/stop path) reads this; this ticket
        # only writes it, as the seam ticket 8 needs.
        self._active_recording: tuple | None = None
        # Held on self, not the overlay -- see `_on_recording_requested`'s
        # own docstring for why: `_commit_selection`'s record branch closes
        # the overlay right after handing off the rect, and `closeEvent`
        # drops `AppController`'s own last reference to it (`_overlay =
        # None`) synchronously, in the same call. A timer that depended on
        # an `OverlayWindow` about to lose its last reference must not fire
        # reliably -- `DelayCountdown`'s own docstring flags this exact bug
        # for a different widget.
        self._recording_delay_timer: QTimer | None = None

        # (rect, delay, after, path) for a recording that has been chosen
        # but not yet started -- what the pill offers "Start recording"
        # for. Deliberately separate from `_active_recording`, which only
        # ever exists once a backend is genuinely running: keeping "chosen"
        # and "running" in one attribute is what made committing a
        # selection and beginning to record the same instant.
        self._armed_recording: tuple | None = None
        # Non-None exactly while the pre-recording countdown is running,
        # which is also how every other method here asks whether it is --
        # see `_stop_countdown_timer`. Parentless for the same reason as
        # `_recording_delay_timer` above.
        self._countdown_timer: QTimer | None = None
        self._countdown_remaining = 0

        # True for the whole duration of a `_stop_recording()` call, not
        # just around `backend.stop()` itself -- `WindowsRecorderBackend
        # .stop()` pumps `QApplication.processEvents()` while it blocks
        # (see its own `_wait_for_stopped()` docstring), which can dispatch
        # a second `WM_HOTKEY` (or a duplicate click on the HUD pill,
        # delivered by that same pumping) and re-enter either
        # `start_capture()` or the recording bar's Stop control before the
        # first stop has finished. `_stop_recording()` itself checks this
        # before calling `backend.stop()`, so a request that lands mid-stop
        # through either entry point is a no-op rather than a second stop
        # or a new snip.
        self._stopping_recording = False
        # True only while a backend's blocking start() is in flight on a
        # worker thread. The UI is live during that -- which is the whole
        # point -- so anything reachable in half a second has to know
        # there is a recording that has been asked for but does not yet
        # exist, and `_pending_start_request` carries what the user asked
        # for meanwhile. See `_start_recorder_responsively`.
        self._starting_recording = False
        self._pending_start_request: str | None = None
        self._recording_started_at: float | None = None
        # No QObject parent -- same reasoning as `_recording_delay_timer`
        # above: this attribute's own strong reference is what keeps it
        # alive.
        self._recording_elapsed_timer: QTimer | None = None
        # The pill widget, or None when there was no room for one (or no
        # recording is active) -- see `_place_recording_hud`.
        self._recording_hud: RecordingBar | None = None
        # The countdown numeral goes *inside* the recorded region rather
        # than on the bar -- see `CountdownNumeral`. Held separately
        # because it is torn down a stage earlier than the bar is.
        self._countdown_numeral: CountdownNumeral | None = None
        # Where the bar's centre stays while its width changes with state.
        self._recording_bar_anchor = None
        # The open dropdown, held so Python does not collect a parentless
        # popup out from under the user mid-choice.
        self._flow_menu: FlowMenu | None = None
        # Per-session, like mode and destination: a snip's own override must
        # not write back to the stored preferences (the handoff's state
        # model says so in as many words).
        self._recording_audio = design.tokens.AUDIO_DEFAULT
        # The file the finished bar is currently describing, so Discard has
        # something to remove once there is no recording left to stop.
        self._landed_recording: Path | None = None
        self._done_timer: QTimer | None = None
        # The red outline around what is being recorded. The overlay is
        # gone by then and took the frame with it, so without this there is
        # nothing on screen saying which region is live.
        self._region_frame = RegionFrame()

        self._overlay: OverlayWindow | None = None
        # Held for the same reason `_overlay` is: a parentless widget is
        # fair game for the GC while its window is still on screen.
        self._settings: SettingsDialog | None = None
        self._reviews: list[ReviewWindow] = []
        self._players: list[PlayerWindow] = []
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

        # Built once, not reconstructed per-recording -- `_start_recording_ui`/
        # `_teardown_recording_ui` just swap `self._tray_icon`'s icon between
        # these two (SNX-123 ticket 8).
        self._idle_tray_icon = icon
        self._recording_tray_icon = _build_recording_tray_icon(icon)

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
        # Disabled by default and only ever enabled while a recording is
        # actually active -- `RecordingBar`'s own docstring is deliberate
        # that its pill is one click target, so a second, distinct way to
        # end a recording (discard, not stop-and-land) lives here instead
        # (recording.md ticket 9). Flipped in the same two places
        # `_start_recording_ui`/`_teardown_recording_ui` already flip the
        # tray icon.
        self.discard_action = menu.addAction("Discard recording")
        self.discard_action.triggered.connect(self._discard_recording)
        self.discard_action.setEnabled(False)
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
        # SNX-123 ticket 8: the capture hotkey (and every other caller of
        # this single funnel -- the tray's own Snip action, --snip's
        # transport listener, a forwarded request from a second launch)
        # doubles as the stop control while a recording is running, ahead
        # of the overlay-open guard below (which is moot here anyway --
        # `_active_recording` is only ever set after `_commit_selection`'s
        # record branch has already closed the overlay). `_active_recording`
        # stays set for the whole duration of a `_stop_recording()` call
        # (see its own docstring), so a request that lands while one is
        # still unwinding sails past this check too -- `_stop_recording()`
        # itself is what turns that into a no-op rather than a second stop,
        # so every entry point (this one, and the HUD's own click handler)
        # gets the guard for free instead of each re-checking it here.
        if self._active_recording is not None:
            self._stop_recording()
            return

        # A recording that has been asked for but has not started yet. The
        # shortcut means "stop" for a running recording, and it means the
        # same here -- the alternative is opening a fresh overlay over a
        # recording that is about to begin filming it.
        if self._starting_recording:
            self._pending_start_request = "stop"
            return

        # An armed or counting-down recording is abandoned, and a fresh
        # snip opens -- this does not fall through to a `return`.
        #
        # It used to *start* the recording, on the reasoning that the
        # hotkey and the bar should always do the same thing. That was
        # wrong about which thing. This shortcut means "give me a new
        # snip" everywhere else in the app, and a user who has drawn the
        # wrong region reaches for it precisely to draw another one:
        # "i tried to cancel and redraw where i wanted to record and i went
        # to reopen the snipux so i can draw a new region and it just
        # started recording". Starting is the Record button's job, and
        # Enter's.
        if self._countdown_timer is not None or self._armed_recording is not None:
            self._cancel_armed_recording()

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
            # SNX-122: fires only for the record side of the chooser -- see
            # `_on_recording_requested`.
            on_recording_requested=self._on_recording_requested,
            # Enter fires the stage's primary action, and while a recording
            # is armed that is Record. Without it Enter fell through to the
            # stills path and copied a *screenshot* of the region.
            on_recording_start=self._begin_armed_recording,
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
        # current answer.
        if self._overlay is not None:
            opening_review = self._overlay.outcome == "review"
        else:
            opening_review = setup_desktop.load_review_window()

        if not opening_review:
            # Nothing else is about to appear, so this is the only
            # confirmation there will be -- see `_notify_capture`.
            self._notify_capture(path)
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

    def _notify_capture(self, path: "Path | None") -> None:
        """Confirm, through the tray, that a snip actually happened.

        The overlay has a toast of its own and the user never sees it.
        `copy()`/`save()` raise it and their callers immediately `close()`
        the window, and `OverlayWindow.hideEvent` takes the toast down with
        everything else -- so the confirmation is created and destroyed in
        the same turn of the event loop. That is invisible for every snip
        that dismisses, and worst for `instant`, which shows no bar, no
        overlay and no window at all: the screen simply flickers and the
        user is left to guess whether anything reached the clipboard.
        Reported as wanting "a notification or something to let us know we
        copied".

        Only when nothing else is about to appear. A snip headed for the
        review window announces itself by opening one, and a balloon saying
        so as well would be telling the user what they are already looking
        at.

        `path` is the file if one was written and None if the image went to
        the clipboard -- the same two cases `_on_captured` already receives
        and the only thing that changes the wording. The folder is named
        rather than the full path: it is the part that answers "where did
        that go", and a full path elides to uselessness in a balloon.
        """
        if path is None:
            message = "Copied to clipboard"
        else:
            message = f"Saved to {path.parent.name}/{path.name}"
        self._report_shortcut(message)

    def _open_player(self, path: Path) -> None:
        """Open a landed recording in the trim editor.

        Held in a list for the same reason review windows are: a parentless
        widget is fair game for the GC while it is on screen, and taking
        several recordings in a row should leave several windows open.
        """
        player = PlayerWindow(path)
        self._players.append(player)
        player.closed.connect(lambda w=player: self._forget_player(w))
        player.show()
        player.raise_()
        player.activateWindow()

    def _forget_player(self, window) -> None:
        if window in self._players:
            self._players.remove(window)

    def _forget_review(self, window) -> None:
        """Drop a closed review window, so a long session doesn't
        accumulate every snip it ever took.
        """
        if window in self._reviews:
            self._reviews.remove(window)

    def _on_recording_requested(
        self,
        rect: QRectF | None,
        delay: str,
        after: str = design.tokens.RECORD_AFTER_DEFAULT,
    ) -> None:
        """*Arm* a recording of `rect` (absolute logical virtual-desktop
        coordinates, or None for the whole desktop) --
        `OverlayWindow._commit_selection`'s record branch calls this and
        closes itself right after, per SNX-122.

        Committing a selection used to start the backend on the spot, so
        there was no moment between "I have chosen what to record" and "it
        is recording" and the opening seconds of every recording were of
        the user getting ready. Arming splits those two into separate,
        deliberate acts: this method only decides *what* would be recorded
        and puts up a pill offering to start it. `_begin_armed_recording()`
        is the other half.

        `after` is `OverlayWindow.outcome` at the moment the selection was
        committed ("instant" or "save", record's own "then" vocabulary) --
        carried through the armed tuple and into `_active_recording` so
        `_stop_recording()` knows, once the file is finally real, whether
        to land-and-copy or just land (recording.md ticket 9's
        `_land_recording`). Defaulting to `tokens.RECORD_AFTER_DEFAULT`
        keeps every caller that only ever passed `(rect, delay)` working
        unchanged.

        `delay` is carried unparsed and only turned into seconds in
        `_begin_armed_recording()`, which is the one place that needs a
        number -- `design.tokens.DELAYS` values ("No delay"/"3s"/"5s"/"10s")
        pass through overlay.py unparsed everywhere else.
        """
        if self._recorder_registry is None:
            self._report_shortcut(self._recorder_unavailable_message)
            return

        # `start_capture`'s re-entrancy guard only blocks a second overlay
        # while the first is genuinely still open -- and the record branch
        # of `_commit_selection` closes the overlay right after calling
        # this, which (via `_on_overlay_dismissed`) clears `self._overlay`
        # in the same call. So a second shortcut press, before this
        # recording is started or stopped, sails straight through
        # `start_capture` and lands here again. Overwriting the armed
        # tuple, countdown timer or `_active_recording` in that case would
        # strand whichever recording was already in flight -- so this
        # refuses the second request and says why, rather than silently
        # discarding the first one.
        if (
            self._armed_recording is not None
            or self._countdown_timer is not None
            or self._active_recording is not None
        ):
            self._report_shortcut(
                "Snipux is already recording, or about to start -- finish that "
                "recording before starting another."
            )
            return

        # The real filename/save-folder convention only applies once the
        # recording lands (`_land_recording`) -- while it's in progress the
        # backend just needs somewhere to write, and that somewhere is the
        # one dedicated subdirectory `_clean_up_crashed_recording_temp_files`
        # knows to sweep on the next launch if this process dies first.
        # Reserved at arm time rather than at start so that a recording
        # which is armed and then cancelled leaves nothing behind but a
        # file this same sweep would collect anyway.
        tmp = tempfile.NamedTemporaryFile(
            suffix=".mp4", delete=False, dir=str(_recording_temp_dir())
        )
        tmp.close()

        self._armed_recording = (rect, delay, after, tmp.name)
        self._show_recording_hud(rect)
        if self._recording_hud is None:
            # Nowhere to put a pill: no monitor geometry to place it
            # against, or a recorded area that covers every candidate spot.
            # The capture shortcut still starts it, so say so -- an armed
            # recording with no visible way to start would just look like
            # nothing happened.
            self._report_shortcut(
                "Ready to record -- press the capture shortcut to start."
            )

    def _show_recording_hud(self, rect: QRectF | None) -> None:
        """Put the pill up in its armed state, if there is anywhere to put
        it.

        Placement is computed once, here, and the pill then stays put for
        the whole of arming, counting down and recording -- a pill that
        jumped between those states would be harder to track than one that
        simply changes what it says.
        """
        # The same union-of-monitor-geometries source `create_overlays()`
        # in overlay.py already uses to build its own `virtual_desktop_rect`
        # for X11.
        geometries = (
            self._monitor_geometries
            if self._monitor_geometries is not None
            else self._real_monitor_geometries()
        )
        bar = RecordingBar()
        bar.set_ready()
        # Audio is the platform's answer, not the bar's: GNOME's screencast
        # has no audio option at all, so the control is offered inert with
        # the reason on it rather than hidden (divergences.md 2).
        bar.set_audio(design.tokens.AUDIO_DEFAULT)
        bar.set_audio_enabled(platform.current.records_audio())
        if not platform.current.records_audio():
            bar.audio_control().setToolTip(platform.current.audio_unavailable_reason())
        bar.set_delay_available(True)

        placement = _place_recording_hud(rect, geometries, bar.sizeHint())
        if placement is None:
            bar.deleteLater()
            return

        bar.startClicked.connect(self._begin_armed_recording)
        bar.delayClicked.connect(self._open_delay_menu)
        bar.audioClicked.connect(self._open_audio_menu)
        bar.cancelClicked.connect(self._cancel_armed_recording)
        bar.stopClicked.connect(self._stop_recording)
        bar.discardClicked.connect(self._on_discard_clicked)

        self._recording_hud = bar
        # The anchor is the centre of the spot found for the bar, not its
        # top-left: the bar's width changes with its state, and rule 1 says
        # the centre is what must stay put -- both edges move symmetrically
        # or the bar appears to slide.
        self._recording_bar_anchor = placement.center()
        self._reposition_recording_bar()
        bar.show()
        # Above the overlay, which is now still up so the region can be
        # reframed -- and which is itself a full-virtual-desktop
        # always-on-top window. Two always-on-top windows are ordered by
        # who was raised last, and the overlay was shown first, so without
        # this the bar is created, positioned and shown *behind* it: armed
        # correctly, invisible entirely. Reported as "still dont see the
        # recording option when i drag to record a region".
        bar.raise_()
        # Windows can keep the bar out of the recording outright
        # (SetWindowDisplayAffinity); everywhere else this answers False and
        # the placement above is what does the job. Called here, after
        # show(), because it needs a realised native window.
        platform.current.exclude_from_capture(bar)

    def _elapsed_text(self) -> str:
        """The running time as the clock shows it, or "0:00" if nothing is
        running. One formatting rule, so the summary cannot disagree with
        the clock the user was just watching.
        """
        if self._recording_started_at is None:
            return "0:00"
        elapsed = int(time.monotonic() - self._recording_started_at)
        minutes, seconds = divmod(elapsed, 60)
        # Zero-padded, as the spec's clock is: a mono clock that gains a
        # digit at 10:00 shifts everything right of it, and the whole
        # argument for putting the clock left of Stop was that it does not.
        return f"{minutes:02d}:{seconds:02d}"

    def _show_finished_bar(
        self, landed: Path, elapsed: str, *, copied: bool = False
    ) -> None:
        """Leave the bar up for a moment saying what was produced, with a
        way to bin it.

        The handoff's stage 6 asks the user to confirm a destination here.
        This does not: the destination was chosen in the chooser before
        recording started, and asking again after is asking the same
        question twice -- which the handoff itself objects to elsewhere.
        The common case is record, stop, paste, and a click in the middle
        of that is friction on the path most used.

        What is worth keeping from stage 6 is the other half: seeing what
        you got, and being able to throw away a bad take without going to
        find the file. So the file lands as it always did, and the bar
        stays for `DONE_LINGER_MS` carrying the summary and Discard.
        """
        bar = self._recording_hud
        if bar is None:
            return
        try:
            megabytes = landed.stat().st_size / (1024 * 1024)
            size = f"{megabytes:.1f} MB"
        except OSError:
            # The file landed and then went; say so rather than showing a
            # size that would be a guess.
            size = "size unknown"
        container = landed.suffix.lstrip(".") or "video"
        # Copy leaves the file in the temp dir and puts a *reference* on
        # the clipboard, so the summary says "copied" rather than naming a
        # container the user cannot go and find. Discard still works, and
        # still means what it says: the clipboard entry it leaves behind
        # pastes nothing, which is the correct outcome for a take the user
        # has just said they do not want.
        tail = "copied" if copied else container
        self._landed_recording = landed
        bar.set_done(f"{elapsed} · {size} · {tail}")
        self._reposition_recording_bar()

        self._done_timer = QTimer()
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self._close_recording_bar)
        self._done_timer.start(design.tokens.FlowMetric.DONE_LINGER_MS)

    def _close_recording_bar(self) -> None:
        """Take the bar down and forget what it was describing."""
        if self._done_timer is not None:
            self._done_timer.stop()
            self._done_timer = None
        if self._recording_hud is not None:
            self._recording_hud.close()
            self._recording_hud = None
        self._recording_bar_anchor = None
        self._landed_recording = None

    def _on_discard_clicked(self) -> None:
        """Discard means two different things depending on when it is
        pressed, and the bar is the same widget for both.

        While recording, it stops and throws the temp file away. Once the
        file has landed, there is no recording left to stop and the thing
        to remove is the file itself -- deleting it here is the whole point
        of leaving the bar up, since the alternative is going to find it.
        """
        if self._active_recording is not None:
            self._discard_recording()
            return
        landed = self._landed_recording
        if landed is None:
            return
        try:
            landed.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 - reported, not swallowed
            self._report_shortcut(f"Could not discard the recording: {exc}")
        else:
            self._report_shortcut(f"Recording discarded: {landed.name}")
        self._close_recording_bar()

    def _open_delay_menu(self) -> None:
        """The delay dropdown for an armed recording.

        Editable here, not just in the chooser, because this is the stage
        where a countdown actually matters -- the handoff gives this bar a
        delay control of its own for exactly that reason. Changing it
        rewrites the armed tuple rather than any stored preference: a
        per-snip override must not quietly become the user's setting.
        """
        bar = self._recording_hud
        if bar is None or self._armed_recording is None:
            return
        rect, current, after, path = self._armed_recording
        rows = [(value, value, "", "", "") for value in design.tokens.DELAYS]
        menu = FlowMenu(rows, current, design.tokens.FlowMetric.MENU_W_DELAY)

        def choose(value: str) -> None:
            if self._armed_recording is None:
                return
            self._armed_recording = (rect, value, after, path)

        menu.chosen.connect(choose)
        control = bar.delay_control()
        menu.open_below(QRect(control.mapToGlobal(control.rect().topLeft()),
                              control.size()))
        self._flow_menu = menu

    def _open_audio_menu(self) -> None:
        """The audio dropdown, opening *upward* so it never covers the
        region being recorded -- the one thing on screen the user is trying
        to look at.

        Every source is listed whatever the platform can do; the ones it
        cannot carry its reason and refuse to be chosen. Leaving them out
        would be the same lie the handoff forbids, told by omission.
        """
        bar = self._recording_hud
        if bar is None:
            return
        reason = "" if platform.current.records_audio() else (
            platform.current.audio_unavailable_reason()
        )
        rows = [
            (identifier, label, note, "",
             "" if identifier == design.tokens.AUDIO_DEFAULT else reason)
            for identifier, _icon, label, note in design.tokens.AUDIO_SOURCES
        ]
        menu = FlowMenu(rows, self._recording_audio,
                        design.tokens.FlowMetric.MENU_W_AUDIO)

        def choose(value: str) -> None:
            self._recording_audio = value
            if self._recording_hud is not None:
                self._recording_hud.set_audio(value)

        menu.chosen.connect(choose)
        control = bar.audio_control()
        menu.open_above(QRect(control.mapToGlobal(control.rect().topLeft()),
                              control.size()))
        self._flow_menu = menu

    def _live_readout(self) -> str:
        """The trailing readout on a live recording: how big the file has
        got.

        The size is read from the file as it grows rather than estimated --
        the one number that answers "is this actually recording anything",
        which a clock counting up on its own does not. It is deliberately
        the same question `_check_recording_disk_space` is already asking
        of the same file every tick.
        """
        # The size alone: the audio control beside it is labelled with its
        # own source, and naming it twice on one bar reads as two different
        # facts rather than one.
        if self._active_recording is None:
            return ""
        _backend, path, _after = self._active_recording
        try:
            megabytes = Path(path).stat().st_size / (1024 * 1024)
        except OSError:
            return ""
        return f"{megabytes:.1f} MB"

    def _show_countdown(self, seconds: int, rect) -> None:
        """Put the count on the bar and, more importantly, inside the region.

        Inside is where the user is already looking: they are watching the
        thing about to be filmed, not the chrome beside it. The pill this
        replaces carried the count on itself, and the opening seconds of a
        recording were still of somebody glancing away from the frame.

        `rect` is only passed on the first tick, when the numeral is
        created; later ticks leave it where it was rather than recomputing
        a position that cannot have changed.
        """
        if self._recording_hud is not None:
            self._recording_hud.set_counting(seconds)
            self._reposition_recording_bar()

        if rect is None:
            if self._countdown_numeral is not None:
                self._countdown_numeral.set_seconds(seconds)
            return

        # A full-screen recording has no region to sit inside that is not
        # also the whole screen, and a numeral centred on the desktop would
        # be filmed. The bar's own count carries it in that case.
        if self._countdown_numeral is None:
            self._countdown_numeral = CountdownNumeral()
        self._countdown_numeral.set_seconds(seconds)
        self._countdown_numeral.show_centered_on(rect)

    def _hide_countdown(self) -> None:
        if self._countdown_numeral is not None:
            self._countdown_numeral.close()
            self._countdown_numeral = None

    def _reposition_recording_bar(self) -> None:
        """Keep the bar centred on its anchor as its width changes.

        Every state shows a different set of controls, so the bar is a
        different width in each. Rule 1 of the handoff is that a bar must
        not shift sideways between stages, and for a centred bar that means
        holding the *centre* while both edges move -- moving the top-left
        instead is what makes it look like it slid.
        """
        bar = self._recording_hud
        if bar is None or self._recording_bar_anchor is None:
            return
        bar.adjustSize()
        bar.move(
            round(self._recording_bar_anchor.x() - bar.width() / 2),
            round(self._recording_bar_anchor.y() - bar.height() / 2),
        )

    def _begin_armed_recording(self) -> None:
        """Take an armed recording to the countdown, or straight to
        recording when no delay is set.

        "No delay" means exactly that, and starting at once is safe here in a
        way it would
        not have been before: `_place_recording_hud` guarantees the pill
        sits outside the recorded area, so clicking Start does not leave
        the pointer inside the frame. A delay of 3s/5s/10s is the user
        asking for preparation time on top of that, and gets a visible
        count -- the recording equivalent of `DelayCountdown`, shown on the
        pill itself rather than in a second widget.
        """
        if self._armed_recording is None:
            return
        # A countdown already running means Start has been pressed once
        # already: pressing it again must not build a second timer over
        # the first, which would strand the first one still counting
        # towards a recording nothing holds a handle to.
        if self._countdown_timer is not None:
            return
        rect, delay, _after, _path = self._armed_recording
        if delay == design.tokens.DELAYS[0]:
            self._start_armed_recording()
            return

        self._countdown_remaining = int(delay.rstrip("s"))
        self._show_countdown(self._countdown_remaining, rect)
        # No QObject parent -- `AppController` isn't one -- so the strong
        # Python reference this assignment creates is what keeps the timer
        # alive until it fires, the same role a parent would otherwise play.
        self._countdown_timer = QTimer()
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick_recording_countdown)
        self._countdown_timer.start()

    def _tick_recording_countdown(self) -> None:
        """One second of the pre-recording countdown."""
        self._countdown_remaining -= 1
        if self._countdown_remaining <= 0:
            self._stop_countdown_timer()
            self._start_armed_recording()
            return
        self._show_countdown(self._countdown_remaining, None)

    def _stop_countdown_timer(self) -> None:
        """Stop and drop the countdown timer, if one is running.

        Dropping the reference is what actually disposes of it -- see
        `_begin_armed_recording` on why the reference is the timer's only
        keeper -- and it doubles as the "is a countdown running" flag every
        other method here tests.
        """
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None
        self._countdown_remaining = 0

    def _cancel_armed_recording(self) -> None:
        """Throw away an armed or counting-down recording without ever
        starting a backend.

        Nothing has been recorded, so there is nothing to land and nothing
        to report but the cancellation itself. The reserved temp file is
        removed here rather than left for the next crash sweep, which
        cannot tell an abandoned reservation from a recording genuinely
        cut short.
        """
        if self._armed_recording is None:
            return
        _rect, _delay, _after, path = self._armed_recording
        self._armed_recording = None
        if self._overlay is not None:
            self._overlay.close()
        self._stop_countdown_timer()
        Path(path).unlink(missing_ok=True)
        self._teardown_recording_ui()
        self._report_shortcut("Recording cancelled.")

    def _start_armed_recording(self) -> None:
        """Actually start a backend on the armed rect -- the moment the
        recording stops being a plan.

        Reached from `_begin_armed_recording()` directly when no delay is
        set, and from `_tick_recording_countdown()` when one is.
        """
        if self._armed_recording is None:
            return
        rect, _delay, after, path = self._armed_recording
        self._armed_recording = None
        # The overlay has been up since the selection was committed so the
        # region could still be reframed, so *it* holds the rect that
        # matters -- not the one handed over at commit. Reading the one
        # from commit would film the rectangle first dragged and make the
        # ready stage's handles a lie.
        if self._overlay is not None:
            if rect is not None:
                reframed = self._overlay.absolute_selection()
                if reframed is not None:
                    rect = reframed
            # Down before the backend starts: from here the frozen frame it
            # paints would be what gets filmed.
            self._overlay.close()

        # The chrome goes up BEFORE the backend, not after. Starting a GNOME
        # screencast is a blocking D-Bus round trip -- measured at ~340ms on
        # this session -- and putting the outline up afterwards left the
        # screen carrying neither the overlay nor the recording frame for a
        # third of a second, with the UI thread wedged so nothing repainted.
        # That reads exactly as reported: a stall, then a flash as
        # everything arrives at once.
        #
        # Safe because none of it is inside the recorded area: `show_around`
        # draws its edges from `left - thickness` outwards and dims the
        # screen *minus* the rect, and `_place_recording_hud` put the pill
        # outside the frame to begin with. The one thing that would be
        # filmed -- the pill during a full-screen recording -- is closed
        # here too, which is a frame earlier than it used to be rather than
        # later.
        self._show_recording_chrome(rect)
        try:
            backend, actual_path = self._start_recorder_responsively(rect, path)
        except RecordingError as exc:
            # Nothing was ever written to this placeholder path -- an
            # empty file left behind here isn't a discarded recording
            # for ticket 9 to clean up, it's a start that never
            # happened at all.
            Path(path).unlink(missing_ok=True)
            self._teardown_recording_ui()
            self._report_shortcut(str(exc))
            return
        if actual_path != path:
            # The backend wrote somewhere else -- GNOME renames to
            # match the container it picked -- so the reserved
            # placeholder is an empty file nothing will ever write to.
            # Dropping it here rather than leaving it for the next
            # crash sweep matters because that sweep cannot tell it
            # apart from a recording genuinely cut short.
            Path(path).unlink(missing_ok=True)
        self._active_recording = (backend, actual_path, after)
        self._start_recording_ui()
        pending, self._pending_start_request = self._pending_start_request, None
        if pending == "discard":
            self._discard_recording()
        elif pending == "stop":
            # Asked for while the backend was still starting. Honoured now
            # rather than leaving a recording running that the user has
            # already said they do not want.
            self._stop_recording()

    def _start_recorder_responsively(self, rect: QRectF | None, path: str) -> tuple:
        """`registry.start()`, with the interface still alive while it runs.

        A nested event loop rather than restructuring the whole flow around
        a callback: every caller of `_start_armed_recording` -- the Start
        press, the countdown's last tick -- still gets a recording that is
        running by the time it returns, and so do the tests.

        The price of a nested loop is re-entrancy, so `_starting_recording`
        is set for its duration and the two things a user can reach in half
        a second both check it: `start_capture` (the global shortcut, the
        tray's Snip) and `_stop_recording` (the pill's Stop). A Stop
        pressed here is remembered rather than dropped -- see
        `_pending_start_request` -- because the honest answer to "stop"
        during start-up is to stop, not to ignore it.
        """
        # Only when every backend that could run says it is safe. The
        # Windows recorder builds a QScreenCapture, a QMediaCaptureSession
        # and a QMediaRecorder in start(); created on a worker thread, they
        # refuse everything stop() later asks of them from the UI thread.
        # It also has no measured need for this -- the delay being fixed is
        # GNOME's D-Bus round trip, which Qt's local objects do not pay.
        if not all(
            backend.starts_off_thread
            for backend in self._recorder_registry.available()
        ):
            return self._recorder_registry.start(rect, path)

        starter = _RecorderStarter(self._recorder_registry, rect, path)
        loop = QEventLoop()
        starter.done.connect(loop.quit)

        self._starting_recording = True
        self._pending_start_request = None
        try:
            QThreadPool.globalInstance().start(starter.run)
            loop.exec()
        finally:
            self._starting_recording = False

        if starter.error is not None:
            raise starter.error
        return starter.result

    def _show_recording_chrome(self, rect: QRectF | None) -> None:
        """Everything the user should *see* the moment a recording starts.

        Split out of `_start_recording_ui` so it can run before the
        backend's blocking start rather than after it -- see the call site
        for the third of a second that cost. Nothing here touches the
        clock: the elapsed time must be measured from when the recording
        genuinely began, not from when its outline appeared.
        """
        # A full-screen recording needs no outline: the region is the
        # screen, and a red border around the whole display would be both
        # useless and, on the edges, in the recording.
        if rect is not None:
            # The screen the recording is on, so everything else on it can
            # be dimmed -- what is *not* being filmed, which an outline
            # alone only implies. Other monitors are left alone: the bar is
            # deliberately on one of them, and so is whatever the user is
            # still working with.
            geometries = (
                self._monitor_geometries
                if self._monitor_geometries is not None
                else self._real_monitor_geometries()
            )
            self._region_frame.show_around(rect, within=_screen_for(rect, geometries))
            # Same as the bar: on Windows the outline and the live scrim
            # are marked out of the capture. They already sit outside the
            # recorded rect, so this changes nothing today -- it is what
            # stops a later change that moves either of them inside it from
            # being a silent regression on the one platform that can say so.
            for strip in self._region_frame.widgets():
                platform.current.exclude_from_capture(strip)
            if self._recording_hud is not None:
                # "Starting", not "0:00". The button has been pressed and a
                # control still offering to do the thing it is doing invites
                # a second press -- but GNOME takes ~460ms to build its
                # capture pipeline, and a clock reading 0:00 through that is
                # a claim the recording has begun. It has not, so someone
                # who starts performing on it loses the opening half second.
                # The word says which of the two is true.
                self._recording_hud.set_live(_STARTING_LABEL, size="")
                self._reposition_recording_bar()

        if rect is None and self._recording_hud is not None:
            # A full-screen recording has no "outside the recorded area"
            # for the pill to sit in, so from here on it would film
            # itself. It is shown while armed and counting -- nothing is
            # being captured then, and a full-screen recording needs a
            # visible Start as much as any other -- and comes down at the
            # exact moment that stops being true. The tray tooltip carries
            # elapsed time from here, as it always did for this case.
            self._recording_hud.close()
            self._recording_hud = None

        if self._recording_hud is None:
            # No room for the bar, so no visible Stop. That happens when the
            # recorded area leaves nowhere to put one -- a whole monitor is
            # the ordinary case, since the bar may not be drawn on the
            # screen being filmed.
            #
            # What is left is a tray icon, a tooltip counting up, and on
            # GNOME the shell's own recording dot: enough to notice a
            # recording is running, not enough to do anything about it. So
            # the one thing that cannot be worked out by looking is said
            # out loud, once, naming the shortcut the user actually has.
            self._report_shortcut(
                f"Recording. Press {setup_desktop.load_shortcut()} to stop "
                "-- the bar is hidden because it would be in the recording."
            )

        # Showing a window maps it; it does not paint it. Both happen when
        # the event loop next runs, and the very next thing the caller does
        # is block it for a third of a second -- so without this the chrome
        # would arrive at the same moment it always did and moving it
        # earlier would have bought nothing.
        #
        # Pumped until the strips are genuinely exposed rather than once:
        # a single pass leaves seven separate always-on-top windows mapped
        # but not yet on screen. Bounded, because a compositor that never
        # exposes them must not stop the recording from starting -- and
        # cheap either way against a backend start an order of magnitude
        # longer.
        #
        # User input excluded deliberately: this is a repaint, not an
        # opportunity to press Stop on a recording that has not started.
        # `QElapsedTimer`, not `time.monotonic()`: this budget has nothing
        # to do with how long the recording has run, and reading the same
        # clock the elapsed time is measured from entangles the two -- it
        # consumed the scripted clock the tests hand `_elapsed_text`.
        budget = QElapsedTimer()
        budget.start()
        while True:
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
            )
            if rect is None or self._region_frame.is_exposed():
                break
            if budget.elapsed() >= _CHROME_PAINT_BUDGET_MS:
                break

    def _start_recording_ui(self) -> None:
        """Start the clock and flip the tray, once a backend really has
        started (SNX-123 ticket 8).

        Called from `_start_armed_recording()`, right after
        `self._active_recording` is actually set -- so this never runs for
        a still-armed or still-counting recording. What the user *sees* went
        up earlier, in `_show_recording_chrome`.
        """
        self._recording_started_at = time.monotonic()

        # No QObject parent -- see `_recording_elapsed_timer`'s own
        # docstring in __init__.
        self._recording_elapsed_timer = QTimer()
        self._recording_elapsed_timer.setInterval(1000)
        self._recording_elapsed_timer.timeout.connect(self._on_recording_tick)
        self._recording_elapsed_timer.start()

        # Not gated on `self._tray_available`: every other piece of tray
        # state this class builds (`setContextMenu`, the icon itself) is
        # likewise built unconditionally, only `.show()` is gated -- an
        # icon/tooltip update on a tray icon nobody sees is harmless the
        # same way.
        self._tray_icon.setIcon(self._recording_tray_icon)
        # Same "flip it wherever the tray icon flips" reasoning as the icon
        # itself -- see the tray menu's own construction comment.
        self.discard_action.setEnabled(True)

        # Ticked once immediately, not just on the timer's first firing a
        # second from now -- a recording stopped inside that first second
        # should still have shown 0:00 somewhere, not nothing at all.
        self._on_recording_tick()

    def _teardown_recording_ui(self, *, keep_bar: bool = False) -> None:
        """Undo everything `_start_recording_ui` brought up.

        Called unconditionally from `_stop_recording()`, regardless of
        whether `backend.stop()` itself goes on to succeed -- the stop
        control should feel instant even though the backend call behind it
        may still be draining for a few seconds on Windows (see
        `_stopping_recording`'s docstring), so the visible state is torn
        down before that call is even made.
        """
        if self._recording_elapsed_timer is not None:
            self._recording_elapsed_timer.stop()
            self._recording_elapsed_timer = None
        # Belt and braces: a stop arriving while a countdown is somehow
        # still running must not leave that timer to fire into a
        # torn-down recording.
        self._stop_countdown_timer()
        # The outline goes whatever happens to the bar. It describes a
        # recording that is over, and `keep_bar=True` deliberately leaves
        # the bar standing afterwards -- hanging the outline off that would
        # leave a red rectangle around nothing until the bar timed out.
        self._region_frame.close()
        if not keep_bar and self._recording_hud is not None:
            self._recording_hud.close()
            self._recording_hud = None
            self._recording_bar_anchor = None
        self._hide_countdown()
        self._recording_started_at = None
        self._tray_icon.setIcon(self._idle_tray_icon)
        self._tray_icon.setToolTip("")
        self.discard_action.setEnabled(False)

    def _on_recording_tick(self) -> None:
        """Push the current elapsed time to every surface that shows it:
        the tray tooltip -- the one surface that survives both a
        full-screen recording (the HUD structurally can't, by
        `_place_recording_hud`'s own design) and a machine with no tray at
        all can still set harmlessly -- and the HUD's own label, when one
        is showing.

        A region recording on a machine with no tray (`_tray_available` is
        False, so nobody can see that tooltip) *and* no room for the HUD
        (`_place_recording_hud` found none) would otherwise show elapsed
        time nowhere at all -- both surfaces individually optional, but the
        acceptance criterion isn't. stdout is the same last-resort fallback
        `_report_shortcut` already uses for "no tray to hang a message on"
        (SNX-54), applied here so that combination is never silent.
        """
        text = self._elapsed_text()
        self._tray_icon.setToolTip(text)
        if self._recording_hud is not None:
            self._recording_hud.set_live(text, size=self._live_readout())
            self._reposition_recording_bar()
        elif not self._tray_available:
            print(f"Snipux is recording -- {text}")

        self._check_recording_disk_space()

    def _check_recording_disk_space(self) -> None:
        """Stop the active recording if the filesystem its temp file is
        actually growing on has dropped below
        `design.tokens.RECORDING_MIN_FREE_BYTES` free (recording.md ticket
        9) -- piggybacked on `_on_recording_tick`'s existing once-a-second
        timer rather than a second one of its own.

        Stats `Path(path).parent` -- `_recording_temp_dir()`, i.e.
        `tempfile.gettempdir()/snipux-recording` -- not the save folder
        `_land_recording` eventually moves into. The save folder is where
        the *finished* file ends up, but the file that's growing while a
        recording is in progress lives under system temp, which is
        routinely a separate, smaller filesystem (a tmpfs mount is the
        common case) -- checking the save folder's free space would miss
        that partition filling up entirely.

        `self._disk_usage` is the constructor-injected factory (defaulting
        to `shutil.disk_usage`, set in `__init__`) so a test can report an
        arbitrarily low `.free` without touching a real disk -- the same
        'factory the test can swap' shape `WindowsRecorderBackend` already
        uses for its Qt objects.

        A failure to stat the temp dir here is a silent no-op rather than
        something this method reports -- `_recording_temp_dir()` creates it
        on demand and a recording can't be active without it, so a stat
        failure here would mean something stranger than "not there yet".
        """
        if self._active_recording is None:
            return
        _backend, path, _after = self._active_recording
        try:
            free = self._disk_usage(str(Path(path).parent)).free
        except OSError:
            return
        if free >= design.tokens.RECORDING_MIN_FREE_BYTES:
            return
        self._stop_recording(reason="Recording stopped: running low on disk space.")

    def _stop_recording(self, *, reason: str | None = None) -> None:
        """Stop the in-progress recording, if there is one -- wired as the
        HUD's own `on_stop` callback and reached via `start_capture()`
        whenever the capture hotkey fires mid-recording (SNX-123 ticket 8),
        and by `_check_recording_disk_space()` above when free space runs
        low.

        `reason`, when given (only `_check_recording_disk_space()` passes
        one), is folded into the one toast `_land_recording` shows instead
        of `_land_recording`'s usual "Recording saved to ..." message --
        without this, a low-disk stop would show two tray notifications
        back to back for the same event (the landed-file message, then a
        separate low-disk message), and depending on the OS's notification
        stacking the first can be clipped before it's read. A failed stop
        or a failed landing reports its own failure instead and `reason` is
        dropped on the floor -- there is no "saved to" to fold it into.

        A request to stop with nothing recording is a no-op, not an error
        -- `_teardown_recording_ui()`/`backend.stop()` are independent
        steps, and `backend.stop()`'s own failure is reported rather than
        raised, the same "a failure must not stop the rest" rule CLAUDE.md
        states for capture backends, applied here. Landing the file
        (`_land_recording`, recording.md ticket 9) only happens once
        `backend.stop()` has actually returned without raising -- a backend
        that failed to stop cleanly gets no promise made about the state
        of its output file. Landing itself can also fail -- `shutil.move`
        into the save folder is a real cross-filesystem copy whenever the
        temp dir and save folder don't share a mount, which needs free
        space precisely in the low-disk scenario this ticket is about -- so
        that failure is reported the same way rather than left to escape
        this Qt slot uncaught.

        The `_stopping_recording` re-entrancy check lives here, at the one
        place `backend.stop()` is actually called, rather than at each of
        this method's call sites (`start_capture()`'s hotkey path and the
        HUD's own `mousePressEvent`) -- a request that lands through either
        one while a `_stop_recording()` call is already unwinding must be a
        no-op, not a second `backend.stop()` while the first is still in
        flight (see the attribute's own docstring in `__init__`), and
        checking it once here is what makes that true for every caller
        instead of only the ones that remember to ask first. The same
        guard is what keeps this from racing `_discard_recording()` over
        the same in-flight recording.

        `_active_recording` is cleared only in the `finally`, after landing
        has already run -- the ordering ticket 9 needs so that anything
        this same call graph could still reach (a second `_stop_recording`,
        a discard, another low-disk tick) never sees "no recording active"
        while the file is still mid-move.
        """
        if self._starting_recording:
            # Pressed during the backend's start-up. There is nothing to
            # stop yet, so this is remembered and acted on the moment
            # there is -- dropping it would make Stop do nothing at the
            # one time a user is most likely to press it, having just
            # watched half a second go by.
            self._pending_start_request = "stop"
            return
        if self._stopping_recording or self._active_recording is None:
            return
        self._stopping_recording = True
        # Read before the teardown clears it -- the summary needs to say how
        # long the recording ran, and by the time the file has landed there
        # is nothing left that knows.
        elapsed = self._elapsed_text()
        self._teardown_recording_ui(keep_bar=True)
        backend, path, after = self._active_recording
        landed = None
        try:
            backend.stop()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            self._report_shortcut(f"Stopping the recording failed: {exc}")
        else:
            try:
                landed = self._land_recording(path, after, reason=reason)
            except OSError as exc:
                self._report_shortcut(f"Saving the recording failed: {exc}")
        finally:
            self._active_recording = None
            self._stopping_recording = False

        if landed is None:
            self._close_recording_bar()
        else:
            self._show_finished_bar(landed, elapsed, copied=after == "instant")

    def _land_recording(
        self, path: str, after: str, *, reason: str | None = None
    ) -> Path:
        """Carry out whichever destination `after` names for a just-stopped
        recording sitting at its temp `path`.

        The two destinations are genuine alternatives now. This used to
        move the file into the save folder *unconditionally* and only then
        consider `after`, so "copy to the clipboard" also left a file
        behind -- in the stills folder, under the stills filename pattern,
        so a video landed as "Screenshot from ....mp4" among actual
        screenshots. A user who asked for the clipboard got a file they
        were never told about, and "save" took credit for a move that had
        already happened whatever they picked.

        **Save** moves into `setup_desktop.load_recording_folder()` under
        `setup_desktop.load_recording_filename_pattern()` -- recording's
        own folder and own pattern, not the stills ones it used to borrow.

        **Instant** copies and does not move. The file stays in
        `_recording_temp_dir()`, which is not a detail that can be tidied
        away: `copy_file_to_clipboard` puts a *reference* on the clipboard,
        not the video's bytes, so deleting the file would leave the user
        holding a clipboard entry that pastes nothing. "No file is kept"
        means none in the recordings folder; the temp copy is swept by
        `_clean_up_crashed_recording_temp_files()` at the next startup,
        which is the longest a clipboard reference could plausibly outlive
        the session that made it anyway.

        `reason` is `_stop_recording()`'s low-disk message, when this call
        came from `_check_recording_disk_space()` -- folded into the one
        toast this method shows instead of the plain "Recording saved to
        ..." wording, so a low-disk stop still says where the file went
        without a second, separate notification for the same event.

        Called only from `_stop_recording()`, once `backend.stop()` has
        already returned without raising -- never from `_discard_recording()`,
        which deletes the temp file outright instead of landing it. Raises
        `OSError` uncaught -- `shutil.move` into a nearly-full save folder
        is exactly the failure ticket 9 needs reported, and `_stop_recording`
        is what catches it and turns it into a toast; this method itself
        makes no promise about the temp file's fate on that path (the
        source of a failed cross-filesystem move is left where it is, for
        the next crash-cleanup sweep to find).
        """
        if after == "instant":
            finish_recording(Path(path), after)
            # The recording wording, not the stills one (locked capture-flow
            # handoff): a video goes on the clipboard as a file *reference*,
            # so it pastes into a file manager, a chat or an upload field and
            # does nothing at all in an image editor or a text box. "Copied
            # to the clipboard" is true and tells the user nothing about
            # which of those will work -- it is what produced "looks like its
            # in my clip board but its hard to know that".
            copied = design.tokens.DESTINATION_WORDING["instant"]["record"][2]
            if reason is not None:
                self._report_shortcut(f"{reason} {copied}.")
            else:
                self._report_shortcut(f"{copied}.")
            # The temp file, which is what the clipboard reference points
            # at. Handed back for the same reason a saved one is: the
            # finished bar has to be able to say how big it was and offer
            # to bin it, and Copy is the *default* destination -- returning
            # nothing here is why that bar never appeared for the
            # destination most recordings actually use.
            return Path(path)

        folder = setup_desktop.load_recording_folder()
        folder.mkdir(parents=True, exist_ok=True)
        # The extension comes from the file that actually exists rather
        # than a fixed "mp4": the backend chose the container (GNOME writes
        # WebM whatever path it was handed), and landing VP8 under a .mp4
        # name misreports it to every player and file manager downstream.
        extension = Path(path).suffix.lstrip(".") or "mp4"
        destination = Path(
            setup_desktop.preview_filename(
                folder,
                setup_desktop.load_recording_filename_pattern(),
                extension=extension,
            )
        )
        shutil.move(path, destination)
        finish_recording(destination, after)
        if after == "open":
            self._open_player(destination)
        landed = destination
        if reason is not None:
            self._report_shortcut(f"{reason} Saved to {destination}")
        else:
            self._report_shortcut(f"Recording saved to {destination}")
        return landed

    def _discard_recording(self) -> None:
        """Tray action (recording.md ticket 9): stop the in-progress
        recording and throw its temp file away rather than land it -- no
        move, no clipboard, and a distinct tray message from the one
        `_land_recording` reports.

        There is nowhere else to reach this from: `RecordingBar`'s own
        docstring is deliberate that its one pill is one click target, so
        a second, distinct way to end a recording lives on the tray's
        context menu instead (see its construction comment in `__init__`).

        Guarded exactly like `_stop_recording`'s own re-entrancy check --
        a discard while a stop (or another discard) is already unwinding
        must be a no-op, not a second `backend.stop()` over the same
        in-flight recording.
        """
        if self._starting_recording:
            # Nothing exists to discard yet; remembered and honoured the
            # moment it does.
            self._pending_start_request = "discard"
            return
        if self._stopping_recording or self._active_recording is None:
            return
        self._stopping_recording = True
        self._teardown_recording_ui()
        backend, path, _after = self._active_recording
        try:
            backend.stop()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            self._report_shortcut(f"Stopping the recording failed: {exc}")
        finally:
            Path(path).unlink(missing_ok=True)
            self._active_recording = None
            self._stopping_recording = False
        self._report_shortcut("Recording discarded.")

    def _on_overlay_dismissed(self) -> None:
        """Called once, by `OverlayWindow`'s own `on_dismissed` hook, the
        moment the current overlay's session actually ends -- Copy, Save,
        Enter, or Esc, every one of which routes through `closeEvent`
        (SNX-62). Clearing `self._overlay` here, rather than leaving
        `start_capture`'s guard to re-derive "is it still open" from
        `isVisible()`, is what makes a stale overlay unable to wedge this
        guard shut for the rest of the session: whatever ends the overlay,
        this is the one path that lets the next Snip request through.

        An armed recording goes with it. The overlay is what holds the
        region while a recording is being set up, so dismissing it -- Esc,
        most often -- is the user saying they do not want this one. Leaving
        `_armed_recording` set left a bar offering to Record a region that
        was no longer on screen, and a Record button that would have filmed
        it anyway.
        """
        self._overlay = None
        if self._armed_recording is not None or self._countdown_timer is not None:
            self._cancel_armed_recording()


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
