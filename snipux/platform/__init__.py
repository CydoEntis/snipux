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

`reserved_top()` joins `ensure_stable_install()` as an operation with a
portable default rather than a required one -- see its own docstring.

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
        file `snipux.exe` (the only Windows distribution route -- SNX-104
        dropped the Inno Setup installer that Smart App Control was
        blocking outright) has no package manager behind it at all, so it
        has to make that guarantee about itself. `app._become_resident()`
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


    def reserved_top(self, screen) -> int:
        """Logical pixels of `screen`'s top edge that the desktop's own
        chrome owns -- a GNOME top bar, a Windows taskbar docked to the
        top -- and will paint over an always-on-top window regardless of
        what that window thinks it covers.

        Chrome placement only. The capture still grabs the whole virtual
        desktop in one shot, per CLAUDE.md's one rule; this decides where
        the pre-snip chooser and the close button may be *drawn*, which is
        an entirely different question from what is in the frame.

        Not one of the six required operations: `QScreen` answers it
        portably wherever the platform tells Qt the truth, so the default
        below is that portable answer and a platform overrides it only
        where Qt is wrong (`LinuxPlatform`, under X11). Zero is always a
        safe answer -- it is what every version before this returned, and
        the cost of being wrong is chrome drawn slightly low, never a
        capture that misses pixels.
        """
        return max(0, screen.availableGeometry().top() - screen.geometry().top())

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
