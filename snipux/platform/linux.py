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

from PyQt6.QtCore import QMargins, Qt
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

    def reserved_margins(self, screen) -> QMargins:
        """GNOME's top bar and dock, which Qt does not report here.

        `QScreen.availableGeometry()` comes back equal to `geometry()` for
        every monitor on Ubuntu/GNOME under X11 -- measured on a three
        monitor desktop where `_NET_WORKAREA` said `0, 32, 6400, 1337`: the
        shell had reserved 32px along the top and, with the Ubuntu Dock fixed
        to the bottom, 71px along the bottom of the tallest monitor, and Qt
        passed on none of it. The base implementation's portable answer is
        therefore nothing at all here. The chooser hung its 54px panel flush
        against an edge the shell was already painting 32px of its own over,
        and the floating bar for a selection reaching the bottom of that
        monitor sat under the dock, where none of it could be clicked.

        The property itself is the only source that knows. Wayland has no
        `_NET_WORKAREA` equivalent to shell out for, and does not need one
        for the top bar: `show_on_screen` fullscreens the overlay onto a
        single output there, and GNOME hides its top bar for a fullscreen
        window. Whether the dock gets out of a fullscreen window's way too
        has not been watched; it is the first thing to check if the bar ever
        turns up under the dock on Wayland.

        That is a reason to skip the X11-only `xprop` fallback below, not a
        reason to stop asking Qt: `portable` is still a real query --
        `QScreen.availableGeometry()`, read whatever the session type -- so a
        compositor that *did* reserve space on Wayland is returned before
        session type is even consulted. Returning `portable` rather than
        empty margins is what keeps that true: nothing comes back because
        Wayland genuinely reserved nothing, never because this gave up
        asking.
        """
        portable = super().reserved_margins(screen)
        if not portable.isNull() or capture.detect_session_type() != "x11":
            return portable
        if QGuiApplication.platformName() != "xcb":
            # An offscreen or minimal Qt platform has no shell painting
            # over anything, whatever `XDG_SESSION_TYPE` still says about
            # the login session -- and the headless suite runs inside a
            # real X11 login. Without this it would shell out to `xprop`
            # and inset chrome by a developer's own GNOME bar, which is a
            # test that passes or fails depending on whose desk it runs on.
            return portable
        return _x11_reserved_margins(screen)

    def skip_map_animation(self, widget) -> bool:
        """True under X11, where `widget` is marked a splash window.

        GNOME Shell animates the mapping of `NORMAL`, `DIALOG` and
        `MODAL_DIALOG` windows only (`windowManager.js`, `_mapWindow`), and
        maps every other type at once. Of the types it leaves alone, splash
        took keyboard focus soonest when tried with real overlays on a GNOME 46
        X11 desk: utility and notification windows still had none 120ms after
        mapping, and the overlay lives on the keyboard. Bypassing the window
        manager skips the animation too, but such a window never becomes the
        active window at all (see the flags in `OverlayWindow.__init__`).

        Wayland clients cannot choose a window type, so the reveal stays
        there, and so it does on any Qt platform that is not xcb -- the
        headless suite's offscreen one included.
        """
        if capture.detect_session_type() != "x11":
            return False
        if QGuiApplication.platformName() != "xcb":
            return False
        widget.setAttribute(Qt.WidgetAttribute.WA_X11NetWmWindowTypeSplash, True)
        return True

    def can_pin(self) -> bool:
        """True under X11, where an ordinary window can be placed at a
        given rect and asked to stay on top -- the recording bar already
        relies on the second half of that (`flowbars.py`,
        `WindowStaysOnTopHint`). False under Wayland: a Wayland client can
        neither place its own window nor ask to stay on top, so a pin there
        could meet neither of Pin's own acceptance criteria.
        """
        return capture.detect_session_type() == "x11"

    def pin_unavailable_reason(self) -> str:
        return (
            "" if self.can_pin() else
            "Wayland doesn't let an app place its own window or keep it on top."
        )

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


def _x11_reserved_margins(screen) -> QMargins:
    """`_NET_WORKAREA`'s insets, as they apply to `screen`.

    The property is a single rect for the whole virtual desktop, so it can
    say how much of each of the desktop's edges is spoken for but not which
    monitor shows the bar or the dock. A monitor whose own edge sits on the
    desktop's edge gets that side's inset; one whose edge lies inside the
    work area is already clear of it and gets nothing on that side.

    Two monitors both flush with the desktop's bottom edge would both be
    inset, even though only one carries the dock. That is the harmless
    direction to be wrong in -- chrome drawn a dock's height higher on one
    monitor, against chrome that cannot be clicked at all -- and beats
    guessing from which monitor is primary, since a dock can be moved.

    Shells out to `xprop` and reserves nothing if anything at all goes
    wrong, the same "degrade, never raise" rule `X11WindowGeometryProvider`
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
        return QMargins()

    _, _, values = result.stdout.partition("=")
    numbers = []
    for field in values.split(",")[:4]:
        try:
            numbers.append(int(field.strip()))
        except ValueError:
            return QMargins()
    if len(numbers) < 4:
        return QMargins()

    # Right and bottom are compared as exclusive edges (x + width), on both
    # sides, so a monitor that ends exactly where the work area ends is
    # inset by nothing rather than by one pixel.
    area_left, area_top, area_width, area_height = numbers
    geometry = screen.geometry()
    return QMargins(
        max(0, area_left - geometry.x()),
        max(0, area_top - geometry.y()),
        max(0, (geometry.x() + geometry.width()) - (area_left + area_width)),
        max(0, (geometry.y() + geometry.height()) - (area_top + area_height)),
    )
