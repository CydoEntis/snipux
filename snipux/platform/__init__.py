"""The platform seam: everything snipux needs from the OS beyond portable Qt.

SNX-85: snipux is going cross-platform (Windows to full parity, macOS
later), and almost none of it needs to know that -- overlay, shapes, marks,
review, settings and the chooser are all ordinary Qt and already behave
identically everywhere PyQt6 runs. What actually differs by platform is
three things the app asks for at its edges:

  * installing/removing whatever makes an installed snipux launchable and
    discoverable outside a terminal (a desktop entry, an autostart entry, a
    global shortcut -- collectively "desktop integration")
  * (re)binding/unbinding that global shortcut on its own, independent of
    the rest of desktop integration -- what Settings does after a user
    changes it
  * where a saved image should go when the user hasn't chosen otherwise
  * (SNX-86) which `capture.CaptureBackend`s can even be tried here --
    `app.build_default_registry()` used to answer this itself by branching
    on `capture.detect_session_type()`, which has no answer at all on a
    platform with no notion of an X11/Wayland session type
  * (SNX-119) which `recording.RecordingBackend`s can even be tried here --
    the same seam as the line above, one operation later: nothing outside
    `platform/` should branch on `sys.platform` to pick a recorder either

`reserved_margins()` (and `reserved_top()`, its top edge) join
`ensure_stable_install()` as operations with a portable default rather than
required ones -- see their own docstrings.
`relaunch_without_console()` (#52) is another: only Windows has a console
that can take the tray app down with it.

This module is the one place that interface (`Platform`) is defined, and the
one place an implementation is picked -- from `sys.platform`, at import
time, into the module-level `current`. Nothing outside this package should
ever branch on `sys.platform`, or reach for gsettings, `.desktop` files, or
XDG paths directly, to get at any of this -- that is what makes this the one
seam to fill in for a new OS, rather than one of several call sites to find.

`linux.py` is today's only real implementation, and is a thin adapter onto
`snipux.setup_desktop`, which already implements every operation below --
see its own module docstring. That module has its own, much larger test
suite (`tests/test_setup_desktop.py`) predating this seam, so the behaviour
lives there unchanged rather than being duplicated or rewritten here.
`windows.py`/`darwin.py` are stubs: every method raises
`UnimplementedPlatformError`, naming both the platform and the operation,
rather than pretending to work -- so an unimplemented platform fails loudly
right here, at the seam, instead of leaving a half-finished setup or a
capture with no way to save it. Adding a real macOS implementation later
means filling `darwin.py` in against this interface; nothing else should
need to change.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QPoint, QMargins, QRect, QSize

if TYPE_CHECKING:
    from snipux.capture import BackendRegistry
    from snipux.recording import RecorderRegistry


class Platform(ABC):
    """What the app needs from the OS it's running on. See this module's
    own docstring for why these seven operations are the whole interface.
    """

    @abstractmethod
    def install_desktop_integration(self, *, shortcut: str | None = None) -> int:
        """Set up whatever makes an installed snipux launchable and
        discoverable outside a terminal. `shortcut`, if given, is bound
        (and remembered) instead of whatever default applies. Returns a
        process exit code: 0 on success.
        """

    @abstractmethod
    def remove_desktop_integration(self) -> int:
        """Undo everything `install_desktop_integration()` did. Returns a
        process exit code: 0 on success.
        """

    @abstractmethod
    def bind_shortcut(self, shortcut: str | None = None) -> str:
        """(Re)bind the global shortcut that starts a snip, without
        touching the rest of desktop integration -- what Settings calls
        after a user changes their shortcut. `shortcut` defaults to
        whatever is already remembered. Returns a one-line, human-readable
        report of what happened; never raises.
        """

    @abstractmethod
    def unbind_shortcut(self) -> str:
        """The counterpart to `bind_shortcut()`. Returns a one-line,
        human-readable report; never raises.
        """

    @abstractmethod
    def default_save_folder(self) -> Path:
        """Where a snip should be saved when the user hasn't chosen
        otherwise.
        """

    @abstractmethod
    def build_capture_registry(self) -> "BackendRegistry":
        """The `capture.BackendRegistry` this platform can capture with.

        What `app.build_default_registry()` asks for instead of branching
        on `capture.detect_session_type()` itself (SNX-86) -- so a platform
        with no session-type concept at all (Windows, macOS) has a real
        answer instead of no branch matching. A platform with nothing
        implemented yet must still return a registry that says so -- see
        `capture.UnsupportedPlatformBackend` -- rather than an empty one or
        a raised exception, since `--list-backends` has to work everywhere.
        """

    @abstractmethod
    def build_recording_registry(self) -> "RecorderRegistry":
        """The `recording.RecorderRegistry` this platform can record with
        (SNX-119) -- what a caller asks for instead of branching on
        `sys.platform` itself, the same seam `build_capture_registry()`
        already is for capture.

        Unlike `build_capture_registry()`, a platform with nothing
        implemented yet raises `UnimplementedPlatformError` here rather
        than handing back a registry containing a placeholder backend:
        recording has no `--list-backends`-style caller yet that needs a
        real answer on every platform, and `recording.py` has no
        `UnsupportedPlatformBackend` of its own to construct one from.
        """

    def ensure_stable_install(self) -> Path | None:
        """SNX-103: relocate this running process to a stable, durable
        location before anything else points at it, if that is even a
        thing this platform's distribution needs. Not one of the six
        required operations above -- most platforms have no answer to
        give: a `pip`/`pipx` install or a source checkout already runs
        from a location a package manager, not this app, is responsible
        for keeping stable, so the default here is a plain no-op every
        platform inherits unless it overrides this.

        `WindowsPlatform` is the one override today: a portable, single-
        file `snipux.exe` -- the route for everyone the installer cannot
        serve, since Smart App Control blocks an unsigned one outright --
        has no package manager behind it at all, so it has to make that
        guarantee about itself. Run *from* the installer it is a no-op,
        because that installs to the very path this would copy to. `app._become_resident()`
        calls this once, on every launch
        that becomes the resident instance, before it does anything that
        might point a shortcut at this process's own, possibly-about-to-
        be-deleted launch location.

        Returns the stable path this process relocated itself to, or
        `None` when there was nothing to relocate -- either this base
        no-op, or a real override that had nothing to do (already running
        from that stable location, or not a build that needs one at all).
        Never raises: a platform that can't relocate itself reports why
        through whatever channel its own override already uses for a note
        (see e.g. `WindowsPlatform.install_desktop_integration`'s), the
        same "a step that can't run is reported, not crashed on" rule as
        every other operation on this interface.
        """
        return None

    def relaunch_without_console(self) -> bool:
        """#52: start the resident app again in a process that no console
        can take down, and return True so the caller can simply exit.

        `app.cli()` asks this before a bare `snipux` becomes the tray app.
        Only Windows answers True (see `WindowsPlatform`'s override): a
        console-stub launcher there runs snipux inside a console, and
        closing that console ends everything attached to it. Nothing on
        Linux or macOS ties a process's life to the terminal that started it
        in a way this could fix, so the default is False -- "run in place",
        which is what every launch did before this existed.

        Never raises. False is also the answer when a platform that would
        relaunch cannot, so the caller always has a working fallback.
        """
        return False


    def reserved_margins(self, screen) -> QMargins:
        """Logical pixels along each edge of `screen` that the desktop's own
        chrome owns -- a GNOME top bar, a dock, a Windows taskbar on any
        edge -- and will paint over an always-on-top window regardless of
        what that window thinks it covers.

        Chrome placement only. The capture still grabs the whole virtual
        desktop in one shot, per CLAUDE.md's one rule; this decides where
        the chooser, the floating bar and everything clamped alongside them
        may be *drawn*, which is an entirely different question from what is
        in the frame.

        Not one of the six required operations: `QScreen` answers it
        portably wherever the platform tells Qt the truth, so the default
        below is that portable answer and a platform overrides it only
        where Qt is wrong (`LinuxPlatform`, under X11). Empty margins are
        what a platform that cannot tell returns, and the cost of being
        wrong is chrome drawn where the shell covers it, never a capture
        that misses pixels.
        """
        geometry = screen.geometry()
        available = screen.availableGeometry()
        return QMargins(
            max(0, available.left() - geometry.left()),
            max(0, available.top() - geometry.top()),
            max(0, geometry.right() - available.right()),
            max(0, geometry.bottom() - available.bottom()),
        )

    def active_screen_geometry(self) -> QRect | None:
        """The screen the desktop considers active, when it can tell us.

        Qt normally exposes this through the global cursor position. Some
        Wayland compositors deliberately report a useless cursor position to
        clients, though, so their platform implementation may provide the
        compositor's own answer. ``None`` keeps the portable primary-screen
        fallback used everywhere else.
        """
        return None

    def reserved_top(self, screen) -> int:
        """The top edge of `reserved_margins`, for the callers that hang
        from it and need nothing else.
        """
        return self.reserved_margins(screen).top()

    def set_app_identity(self) -> None:
        """Tell the OS this process is Snipux, before any window exists.

        Needed where the OS names a running app after its executable, and
        that executable is the Python interpreter. Never raises; the
        default does nothing, which is right where the OS already names
        the app from its own entry (Linux's `.desktop` file).
        """

    def update_asset_name(self, version: str) -> str | None:
        """Which file on the release this build updates itself from, or
        None when it cannot -- which is the default.

        None is not a failure: a pip install updates with `snipux --update`
        and a `.deb` needs root, so for those the honest answer is that the
        release page is where the user goes. A platform that returns a name
        is promising `install_update` knows what to do with that file.
        """
        return None

    def install_update(self, downloaded: "Path") -> bool:
        """Apply the file `update_asset_name` named, and say whether it is
        going ahead. Called only after the user has clicked Update.

        True means the application is about to be replaced and should stop:
        every route here ends with this process exiting so its own file can
        be written, and with the new one started in its place. False means
        nothing happened and snipux carries on as it was.
        """
        return False

    def take_keyboard_focus(self, widget) -> bool:
        """Make `widget` the window the keyboard is actually talking to, and
        say whether it worked. Called straight after `raise_()` and
        `activateWindow()`, on a widget that is already shown.

        Chrome only, but not cosmetic: every keyboard shortcut in the
        application depends on it. Qt's `activateWindow()` is a *request*,
        and on Windows it is one the system is allowed to refuse -- a
        process that does not own the foreground and did not receive the
        last input event cannot simply take it, which is the rule that stops
        background applications stealing your typing. Snipux runs into it
        from the front: it is woken by a global hotkey while another
        application is in front, puts a fullscreen window up, and that
        window is on top, visible, and not the one the keyboard is pointed
        at. Measured: with the overlay up, `GetForegroundWindow()` still
        returned the chat window behind it.

        True by default -- on Linux `activateWindow()` is honoured and there
        is nothing more to do, so the overlay's existing call already did
        the job and this reports it. A platform that needs more than that
        overrides this and says whether its own attempt worked.
        """
        return True

    def skip_map_animation(self, widget) -> bool:
        """Ask the desktop to show `widget` without its window-opening
        animation, and say whether it will. Called before `widget` is first
        shown, while its native window does not exist yet.

        Chrome only. The overlay is a frozen picture of the desktop, and a
        compositor that scales a newly mapped window into place makes that
        picture look like the screen zooming. Where this returns False the
        overlay maps itself transparent and reveals itself once the animation
        is over -- `OverlayWindow._REVEAL_DELAY_MS` on every snip, nearly half
        the time one took to appear. So True is worth that much, and must only
        be returned where the animation really is skipped.

        False by default: nothing is known to skip it on Windows or macOS,
        and neither has been measured.
        """
        return False

    def place_capture_overlay(self, widget, screen) -> bool:
        """Prepare ``widget`` to cover ``screen`` without real fullscreen.

        Called before the widget's native window exists. False keeps the
        portable Wayland fullscreen path; a compositor-specific platform may
        install a pre-map placement rule and return True.
        """
        return False

    def exclude_from_capture(self, widget) -> bool:
        """Ask the OS to leave `widget` out of screen captures while
        leaving it visible on screen. True if it took.

        Chrome only, like `reserved_top` and `records_audio`: it decides
        whether the recording bar and the region outline can be *trusted*
        not to film themselves, never what the recorder captures.

        Defaults to False -- "this platform cannot" -- because most cannot.
        Linux has no equivalent and this was introspected, not assumed:
        `org.gnome.Shell.Screencast`'s `ScreencastArea` takes `draw-cursor`
        and `framerate` and nothing else, and it captures the composited
        output, so a window on screen is a window in the file.

        **Callers must keep working when this returns False.** It is an
        improvement on top of correct placement, never a replacement for
        it: the bar still has to be positioned clear of the recorded area,
        because on every platform but one that is the only thing keeping it
        out of the recording.
        """
        return False

    def records_audio(self) -> bool:
        """Whether this platform's recorder can capture an audio track.

        Chrome only, like `reserved_top`: it decides whether the recording
        bar's audio control is offered live or inert, never what the
        recorder actually does. The recorder's own backend is still the
        thing that would carry audio if there were any.

        Defaults to True because a recorder that cannot do audio is the
        exception rather than the rule -- `QMediaRecorder` on Windows takes
        a source and records one. Linux overrides it: the whole Wayland
        route is `org.gnome.Shell.Screencast`, which has no audio option to
        pass, so there is nothing a control could be wired to.

        Answered here rather than by a `sys.platform` test at the bar,
        because this is the one place that question is allowed to be asked
        (CLAUDE.md), and because "can this machine record audio" is exactly
        the shape of thing that will differ again on macOS.
        """
        return True

    def audio_unavailable_reason(self) -> str:
        """Why `records_audio()` is False, for the control to carry.

        The handoff's rule is that an option which cannot work is shown
        with its reason rather than hidden -- hiding it is the same lie
        told quietly, and a user who cannot see why has no way to tell a
        missing feature from a broken one. Empty when audio *is* available,
        since there is nothing to explain.
        """
        return ""

    def audio_source_unavailable_reason(self, source: str) -> str:
        """Why the recording bar's audio `source` ("system", "mic" or
        "off", `design.tokens.AUDIO_SOURCES`) cannot be chosen here, or ""
        when it can.

        Per source because `records_audio()` is one answer for all of them,
        and Windows has two different ones: a microphone Qt can open, and
        desktop sound it cannot. "off" is always available -- a recording
        with no audio track is something every recorder can make.
        """
        if source == "off" or self.records_audio():
            return ""
        return self.audio_unavailable_reason()

    def places_windows(self) -> bool:
        """Whether a window of snipux's own lands where snipux puts it.

        Chrome only: it decides whether the recording chrome that has to
        sit clear of the recorded area -- the bar, the red outline, the
        countdown -- may be windows of their own. Where it is False they
        live inside the overlay while it is up, and are not shown once it
        closes, since a window the compositor placed could land inside the
        recording. Measured on GNOME 46 Wayland: the outline's four strips
        cascaded straight across the recorded region.

        Defaults to False; Windows and X11 answer True.
        """
        return False

    def place_recording_controls(
        self, widget, top_left: QPoint, size: QSize
    ) -> bool:
        """Place the live recording bar at ``top_left`` if this desktop can.

        This is narrower than :meth:`places_windows`: a Wayland compositor
        may expose a compositor-specific command for one small control window
        while still refusing ordinary client positioning.  The recording
        flow uses this only after its fullscreen selection surface has gone,
        never for overlays or monitor-sized windows.

        The portable answer is the ordinary Qt move on platforms which
        honour it.  Other platforms return False so the caller can remove a
        bar that might otherwise be placed inside the recording.
        """
        if not self.places_windows():
            return False
        widget.resize(size)
        widget.move(top_left)
        return True

    def show_recording_frame(self, widgets) -> bool:
        """Map the windows forming the live recording boundary.

        The widgets already carry their requested absolute geometries.  This
        operation is separate from :meth:`place_recording_controls` because
        four border strips can have duplicate sizes, so a compositor-specific
        implementation may need to map and identify them one at a time.
        """
        if not self.places_windows():
            return False
        for widget in widgets:
            widget.show()
        return True

    def uses_single_recording_frame_window(self) -> bool:
        """Whether the live boundary should use one transparent surface."""
        return False

    def can_pin(self) -> bool:
        """Whether a pin (SNX-83) can be placed at the selection's exact
        rect and kept on top here.

        Chrome only, like `exclude_from_capture` and `records_audio`: it
        decides whether Pin is offered live on the destination menu or
        greyed with `pin_unavailable_reason()`, never anything about the
        window itself once one is allowed to open.

        Defaults to False. A pin needs two things a client can only ask the
        compositor for on some platforms: placing its own window at a given
        screen rect, and asking to stay on top of everything else. Windows
        answers True for both; Linux answers True under X11 and False under
        Wayland, where neither is available to a client at all -- see
        `LinuxPlatform.can_pin`.
        """
        return False

    def pin_unavailable_reason(self) -> str:
        """Why `can_pin()` is False, for the greyed destination-menu row to
        carry -- the handoff's rule that an option which cannot work says
        why, the same as `audio_unavailable_reason()` and
        `text_recognition_unavailable_reason()`. Empty when pinning is
        available.
        """
        return "Not supported on this platform yet"

    def records_cursor(self) -> bool:
        """Whether a recording here can be asked to include or leave out the
        mouse pointer.

        Chrome only, like `can_pin` and `records_audio`: it decides whether
        the chooser row offers the toggle live or greyed with
        `cursor_toggle_unavailable_reason()`, never what the recorder then
        does with the answer.

        Defaults to False, so a platform that has not thought about it does
        not offer a control that silently does nothing -- which is exactly
        what Settings did before this existed: a switch that read as
        universal and only ever reached GNOME.
        """
        return False

    def cursor_toggle_unavailable_reason(self) -> str:
        """Why `records_cursor()` is False, for the greyed control to carry
        -- the same rule as `pin_unavailable_reason()`: an option that
        cannot work says why, because a user who cannot see the reason has
        no way to tell a limit from a bug. Empty where it is available.
        """
        return "Not supported on this platform yet"

    def recognizes_text(self) -> bool:
        """Whether `recognize_text` can read text out of an image here.

        Decides whether automatic hiding of sensitive text is offered live
        or greyed with `text_recognition_unavailable_reason()`. Defaults to
        False: only Windows ships an OCR engine that needs no install, and a
        platform without one should say so rather than silently hide
        nothing.
        """
        return False

    def text_recognition_unavailable_reason(self) -> str:
        """Why `recognizes_text()` is False, for the greyed control to
        carry. Empty when recognition is available."""
        return "Windows only for now"

    def recognize_text(self, image) -> list:
        """The words in `image` (a `QImage`), as a list of lines, each a
        list of `sensitive.RecognizedWord` with rects in `image`'s own
        pixels. Never raises: a platform that cannot recognise, or a
        recognition that fails, returns no lines."""
        return []


class UnimplementedPlatformError(NotImplementedError):
    """Raised by a stub platform implementation (`windows.py`/`darwin.py`
    today) for an operation that platform doesn't support yet. Names both
    the platform and the operation, rather than a bare
    `NotImplementedError` that gives no clue which of the five methods
    above was actually called.
    """

    def __init__(self, platform_name: str, operation: str):
        self.platform_name = platform_name
        self.operation = operation
        super().__init__(f"{operation} is not implemented on {platform_name} yet")


def is_windows() -> bool:
    """Whether this process is running on Windows.

    For the few places below the seam -- a backend's own `is_available()`,
    an error message -- that need the answer without a `Platform` object.
    Read live rather than cached, so a test that sets `sys.platform` sees
    its own value. Defined above `current` on purpose: `capture.py`,
    `recording.py` and `setup_desktop.py` import this package at their top,
    while `_select()` below imports modules that import them back, so these
    must already exist when that cycle re-enters this half-built module.
    """
    return sys.platform == "win32"


def is_linux() -> bool:
    """Whether this process is running on Linux. See `is_windows()`."""
    return sys.platform.startswith("linux")


def os_name() -> str:
    """The OS as a person would name it: `Linux`, `Windows`, `macOS`, or
    the raw `sys.platform` for anything else. See `is_windows()`."""
    if is_linux():
        return "Linux"
    return {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, sys.platform)


def _select() -> Platform:
    """The one place `sys.platform` is read to choose an implementation --
    every other module reaches `current` instead.
    """
    if sys.platform.startswith("linux"):
        from . import linux

        return linux.LinuxPlatform()
    if sys.platform == "win32":
        from . import windows

        return windows.WindowsPlatform()
    if sys.platform == "darwin":
        from . import darwin

        return darwin.DarwinPlatform()
    raise RuntimeError(f"Snipux has no platform support for {sys.platform!r}")


# Selected once, at import time (the acceptance criterion this exists to
# satisfy), not lazily on first use -- so a platform with no implementation
# at all fails the moment snipux starts, not partway through whatever first
# happened to touch this module.
current: Platform = _select()
