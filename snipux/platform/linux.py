"""The Linux `Platform`: a thin adapter onto `snipux.setup_desktop`, plus
(SNX-86) the session-type-driven capture backend selection that used to
live in `app.build_default_registry()`.

Every desktop-integration operation here already exists in
`setup_desktop.py` -- `.desktop` entries, the GNOME custom-keybinding dance,
XDG paths -- and is covered by its own, much larger test suite
(`tests/test_setup_desktop.py`) that predates this seam. This module does
not reimplement or duplicate any of that; it only gives it a name in the
shape `snipux/platform/__init__.py` defines, so callers reach it through
`platform.current` instead of importing `setup_desktop` (a Linux specific)
directly. `build_capture_registry()` forwards to `capture.build_linux_registry()`
the same way -- the session-type-driven Wayland/X11/both selection is real
logic that lives in `capture.py` alongside the registries it chooses
between, not duplicated here. `build_recording_registry()` (SNX-119)
forwards to `recording.build_linux_registry()` for the same reason.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PyQt6.QtGui import QGuiApplication

from snipux import capture, recording, setup_desktop
from snipux.capture import BackendRegistry
from snipux.recording import RecorderRegistry

from . import Platform


class LinuxPlatform(Platform):
    def install_desktop_integration(self, *, shortcut: str | None = None) -> int:
        return setup_desktop.run_setup(shortcut=shortcut)

    def remove_desktop_integration(self) -> int:
        return setup_desktop.run_remove()

    def bind_shortcut(self, shortcut: str | None = None) -> str:
        # find_console_script()'s failure is reported here, not inside
        # bind_gnome_shortcut() -- the same split setup_desktop.run_setup()
        # already makes between "no console script" (fatal) and every other
        # step (a note) -- see its own docstring.
        exec_path = setup_desktop.find_console_script()
        if exec_path is None:
            return (
                "Settings saved, but the snipux console script could not be "
                "found, so the shortcut was not re-bound."
            )
        return setup_desktop.bind_gnome_shortcut(exec_path, shortcut)

    def unbind_shortcut(self) -> str:
        return setup_desktop.unbind_gnome_shortcut()

    def default_save_folder(self) -> Path:
        return setup_desktop.default_save_folder()

    def build_capture_registry(self) -> BackendRegistry:
        return capture.build_linux_registry()

    def build_recording_registry(self) -> RecorderRegistry:
        return recording.build_linux_registry()

    def reserved_top(self, screen) -> int:
        """GNOME's top bar, which Qt does not report here.

        `QScreen.availableGeometry()` comes back equal to `geometry()` for
        every monitor on Ubuntu/GNOME under X11 -- measured on a three
        monitor desktop where `_NET_WORKAREA` said `0, 32, 6400, 1337`, so
        the shell had reserved 32px and Qt passed none of it on. The base
        implementation's portable answer is therefore zero here, and the
        chooser hung its 54px panel flush against an edge the shell was
        already painting 32px of its own over. The armed tab is 26px, so
        it vanished outright.

        The property itself is the only source that knows. Wayland has no
        `_NET_WORKAREA` equivalent to shell out for, and does not need one:
        `show_on_screen` fullscreens the overlay onto a single output
        there, and GNOME hides its top bar for a fullscreen window. That
        is reasoning, not an observation: this codebase has still never
        been launched on a Wayland session, so it remains unverified (see
        TODO.md). Treat it as the expectation to check first if the
        chooser ever turns up hidden on Wayland.

        That is a reason to skip the X11-only `xprop` fallback below, not
        a reason to stop asking Qt: `portable` above is still a real query
        -- `QScreen.availableGeometry()`, read whatever the session type --
        so a compositor that *did* reserve top-edge space on Wayland would
        already have been returned by the line above this comment, before
        session type is even consulted. Returning `portable` here again
        (rather than a hardcoded `0`) is what keeps that true: zero comes
        back because Wayland genuinely reserved nothing, never because
        this function gave up asking.
        """
        portable = super().reserved_top(screen)
        if portable or capture.detect_session_type() != "x11":
            return portable
        if QGuiApplication.platformName() != "xcb":
            # An offscreen or minimal Qt platform has no shell painting
            # over anything, whatever `XDG_SESSION_TYPE` still says about
            # the login session -- and the headless suite runs inside a
            # real X11 login. Without this it would shell out to `xprop`
            # and inset chrome by a developer's own GNOME bar, which is a
            # test that passes or fails depending on whose desk it runs on.
            return portable
        return _x11_reserved_top(screen)

    def records_audio(self) -> bool:
        """False. `org.gnome.Shell.Screencast` takes `draw-cursor` and
        `framerate` and nothing else -- there is no audio option in the
        interface, so there is nothing to wire a control to.

        Capturing from PipeWire ourselves and muxing it is a different
        piece of work from this one, and would pull in a dependency the
        project has so far refused (CLAUDE.md: a fourth is a decision worth
        raising in the ticket). See docs/design/flow/divergences.md 2.
        """
        return False

    def audio_unavailable_reason(self) -> str:
        return (
            "GNOME's screen recorder has no audio track. Recording audio on "
            "Linux needs a capture route Snipux does not have yet."
        )


def _x11_reserved_top(screen) -> int:
    """`_NET_WORKAREA`'s top offset, as it applies to `screen`.

    The property is a single rect for the whole virtual desktop, so it can
    say how much of the desktop's top edge is spoken for but not which
    monitor is showing the bar. A monitor whose own top edge sits at the
    desktop's top gets the inset; one mounted lower is already clear of it
    and gets nothing.

    Two monitors both flush with the desktop's top edge would both be
    inset, even though only one carries the bar. That is the harmless
    direction to be wrong in -- chrome drawn 32px low on one monitor,
    against chrome that cannot be clicked at all -- and beats guessing
    from which monitor is primary, since a bar can be moved.

    Shells out to `xprop` and returns 0 if anything at all goes wrong,
    the same "degrade, never raise" rule `X11WindowGeometryProvider`
    already follows around `wmctrl`.
    """
    try:
        result = subprocess.run(
            ["xprop", "-root", "_NET_WORKAREA"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return 0

    _, _, values = result.stdout.partition("=")
    numbers = []
    for field in values.split(",")[:4]:
        try:
            numbers.append(int(field.strip()))
        except ValueError:
            return 0
    if len(numbers) < 4:
        return 0

    workarea_top = numbers[1]
    return max(0, workarea_top - screen.geometry().top())
