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

import json
import os
import re
import subprocess
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QCoreApplication, QEventLoop, QPoint, QMargins, QRect, QSize, Qt
from PyQt6.QtGui import QGuiApplication

from snipux import capture, ffmpeg, recording, setup_desktop

from . import Platform

# Type-only: `capture.py`/`recording.py` import this package at their top, so
# a name imported out of either here could still be undefined when
# `platform/__init__.py`'s `_select()` imports this module.
if TYPE_CHECKING:
    from snipux.capture import BackendRegistry
    from snipux.recording import RecorderRegistry


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
        return setup_desktop.bind_linux_shortcut(exec_path, shortcut)

    def unbind_shortcut(self) -> str:
        return setup_desktop.unbind_linux_shortcut()

    def default_save_folder(self) -> Path:
        return setup_desktop.default_save_folder()

    def active_screen_geometry(self) -> QRect | None:
        """The focused Hyprland output, matched back to Qt's geometry.

        Qt reports ``QCursor.pos() == (0, 0)`` on Hyprland's Wayland
        backend, even when the pointer is on another output. Hyprland keeps
        the focused output in ``hyprctl monitors -j`` and updates it as the
        pointer moves, so use that answer only for a real Hyprland session.
        Every failure degrades to the base class's portable fallback.
        """
        if not setup_desktop.is_hyprland_session():
            return None
        try:
            result = subprocess.run(
                ["hyprctl", "monitors", "-j"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0:
                return None
            focused_name = next(
                str(monitor["name"])
                for monitor in json.loads(result.stdout)
                if monitor.get("focused") and monitor.get("name")
            )
        except (OSError, StopIteration, ValueError, TypeError, subprocess.SubprocessError):
            return None

        for screen in QGuiApplication.screens():
            if screen.name() == focused_name:
                return QRect(screen.geometry())
        return None

    def records_cursor(self) -> bool:
        """True: GNOME's screencast takes a `draw-cursor` option, which
        `GnomeScreencastBackend` passes straight through."""
        return True

    def build_capture_registry(self) -> BackendRegistry:
        return capture.build_linux_registry()

    def build_recording_registry(self) -> RecorderRegistry:
        return recording.build_linux_registry()

    def reserved_margins(self, screen) -> QMargins:
        """Desktop bars and docks which Qt does not report here.

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

        Hyprland exposes the equivalent per-output reservation in
        ``hyprctl monitors -j``. Snipux's Hyprland overlay deliberately no
        longer enters fullscreen, so Omarchy's bar stays visible and its
        reservation must inset capture chrome even though the frozen pixels
        themselves still cover the complete monitor.

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
        if not portable.isNull():
            return portable
        session_type = capture.detect_session_type()
        if (
            session_type == "wayland"
            and QGuiApplication.platformName() == "wayland"
            and setup_desktop.is_hyprland_session()
        ):
            return self._hyprland_reserved_margins(screen)
        if session_type != "x11":
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

    def _hyprland_reserved_margins(self, screen) -> QMargins:
        """Return Hyprland's cached ``left, top, right, bottom`` extents."""
        cached = getattr(self, "_hyprland_reserved_cache", None)
        if cached is None:
            cached = {}
            try:
                result = subprocess.run(
                    ["hyprctl", "monitors", "-j"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                monitors = json.loads(result.stdout) if result.returncode == 0 else []
                for monitor in monitors:
                    name = monitor.get("name")
                    reserved = monitor.get("reserved")
                    if (
                        name
                        and isinstance(reserved, list)
                        and len(reserved) == 4
                        and all(isinstance(value, int) for value in reserved)
                    ):
                        cached[str(name)] = QMargins(*reserved)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                pass
            self._hyprland_reserved_cache = cached
        return cached.get(screen.name(), QMargins())

    def skip_map_animation(self, widget) -> bool:
        """Arrange for the capture overlay to map without animation.

        GNOME Shell animates the mapping of `NORMAL`, `DIALOG` and
        `MODAL_DIALOG` windows only (`windowManager.js`, `_mapWindow`), and
        maps every other type at once. Of the types it leaves alone, splash
        took keyboard focus soonest when tried with real overlays on a GNOME 46
        X11 desk: utility and notification windows still had none 120ms after
        mapping, and the overlay lives on the keyboard. Bypassing the window
        manager skips the animation too, but such a window never becomes the
        active window at all (see the flags in `OverlayWindow.__init__`).

        Hyprland can apply a rule before the first map. Qt Wayland does not
        support ``setWindowOpacity``; attempting to hide the map animation
        that way leaves the animation visible and prints a warning for every
        overlay surface. Give capture surfaces an exact private title and
        install a runtime-only rule for that title. The rule disappears on a
        Hyprland reload/restart and cannot affect Settings or other windows.
        """
        if (
            capture.detect_session_type() == "wayland"
            and QGuiApplication.platformName() == "wayland"
            and setup_desktop.is_hyprland_session()
        ):
            widget.setWindowTitle("snipux-capture-overlay")
            if getattr(self, "_hyprland_overlay_rule_ready", False):
                return True
            code = (
                "_G.snipux_capture_overlay_rule = "
                "_G.snipux_capture_overlay_rule or hl.window_rule({ "
                "name = 'snipux-capture-overlay', match = { title = "
                "'^snipux-capture-overlay$' }, no_anim = true, "
                "animation = 'none' })"
            )
            try:
                result = subprocess.run(
                    ["hyprctl", "eval", code],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            if result.returncode != 0:
                return False
            self._hyprland_overlay_rule_ready = True
            return True

        if capture.detect_session_type() != "x11":
            return False
        if QGuiApplication.platformName() != "xcb":
            return False
        widget.setAttribute(Qt.WidgetAttribute.WA_X11NetWmWindowTypeSplash, True)
        return True

    def place_capture_overlay(self, widget, screen) -> bool:
        """Place a capture surface with a retained, output-specific rule.

        A true fullscreen client makes Hyprland hide every ordinary window
        on that output, then restore them when the snip closes. That is the
        desktop flash. Hyprland's documented static window-rule effects can
        instead float, size and place the surface before its first map,
        covering the same pixels without changing the workspace's fullscreen
        state.
        """
        if not getattr(self, "_hyprland_overlay_rule_ready", False):
            return False
        if screen is None:
            return False

        output = screen.name()
        if not output:
            return False
        suffix = re.sub(r"[^A-Za-z0-9_]", "_", output)
        title = f"snipux-capture-overlay-{suffix}"
        variable = f"snipux_capture_overlay_rule_{suffix}"
        rule_name = f"snipux-capture-overlay-{output}"
        title_pattern = f"^{re.escape(title)}$"
        code = (
            f"_G.{variable} = _G.{variable} or hl.window_rule({{ "
            f"name = {json.dumps(rule_name)}, "
            f"match = {{ title = {json.dumps(title_pattern)} }}, "
            "no_anim = true, animation = 'none', float = true, "
            f"monitor = {json.dumps(output + ' silent')}, "
            "move = { 0, 0 }, size = { 'monitor_w', 'monitor_h' }, "
            "border_size = 0, rounding = 0 })"
        )
        try:
            result = subprocess.run(
                ["hyprctl", "eval", code],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        # Static rule matching reads the initial title. Set it only after the
        # rule exists, and still before show_on_screen creates the native
        # window with winId().
        widget.setWindowTitle(title)
        return True

    def places_windows(self) -> bool:
        """True under X11. Under Wayland a client cannot place its own
        window at all; the compositor does."""
        return capture.detect_session_type() == "x11"

    def place_recording_controls(
        self, widget, top_left: QPoint, size: QSize
    ) -> bool:
        """Place Snipux's small live-recording bar on Hyprland Wayland.

        Wayland intentionally ignores a client's requested top-left.  Unlike
        GNOME, Hyprland exposes an IPC dispatcher for placing a particular
        floating window, so use that narrow compositor capability without
        claiming that arbitrary Qt windows are placeable.  The title is
        private to this one frameless control window and there can only be
        one active recording, making the selector exact and unambiguous.

        Pinning keeps Stop reachable if the user changes workspace while a
        recording is live.  Every failure degrades to False; the controller
        then removes the bar rather than risk filming it.
        """
        if not setup_desktop.is_hyprland_session():
            return super().place_recording_controls(widget, top_left, size)

        # The first placement happens before the live HUD is mapped.  Giving
        # Hyprland its floating geometry then prevents the ordinary tiled
        # configure that would otherwise resize every window on the workspace
        # for a frame.  Later calls reposition the already-visible HUD as its
        # controls change width.
        if hasattr(widget, "isVisible") and not widget.isVisible():
            return self._prepare_hyprland_recording_window(
                widget, top_left, size, role="controls"
            )

        selector = self._hyprland_recording_window(
            size, title=widget.windowTitle()
        )
        if selector is None:
            return False
        return self._place_hyprland_recording_window(selector, top_left, size)

    def show_recording_frame(self, widgets) -> bool:
        """Place and map each live-recording edge on Hyprland.

        Each edge gets an exact static rule before it maps.  It therefore
        enters the compositor already floating at its final geometry instead
        of briefly joining the tiled layout and making the desktop jump.

        Only the four narrow border windows reach this path.  The portable
        live scrim uses monitor-sized translucent panels; leaving those off
        Hyprland avoids making a large compositor surface part of recording
        startup merely for decoration.
        """
        # X11 (and tests exercising the portable contract) can use Qt's
        # requested geometries directly.  Check that capability first: the
        # login environment may still name Hyprland while Qt itself is
        # deliberately running on another platform, as the headless suite
        # does.
        if self.places_windows():
            return super().show_recording_frame(widgets)
        if not setup_desktop.is_hyprland_session():
            return super().show_recording_frame(widgets)

        for number, widget in enumerate(widgets):
            top_left = QPoint(widget.pos())
            size = QSize(widget.size())
            if not self._prepare_hyprland_recording_window(
                widget, top_left, size, role=f"frame-{number}"
            ):
                return False
            widget.show()
        return True

    def _prepare_hyprland_recording_window(
        self,
        widget,
        top_left: QPoint,
        size: QSize,
        *,
        role: str,
    ) -> bool:
        """Install exact static geometry before recording chrome maps."""
        screen = QGuiApplication.screenAt(
            QPoint(
                top_left.x() + size.width() // 2,
                top_left.y() + size.height() // 2,
            )
        )
        if screen is None or not screen.name():
            return False

        geometry = screen.geometry()
        local_x = top_left.x() - geometry.x()
        local_y = top_left.y() - geometry.y()
        identity = (
            f"{role}-{screen.name()}-{top_left.x()}-{top_left.y()}-"
            f"{size.width()}-{size.height()}"
        )
        suffix = re.sub(r"[^A-Za-z0-9_]", "_", identity)
        title = f"snipux-recording-{identity}"
        title_pattern = f"^{re.escape(title)}$"
        variable = f"snipux_recording_rule_{suffix}"
        code = (
            f"_G.{variable} = _G.{variable} or hl.window_rule({{ "
            f"name = {json.dumps(title)}, "
            f"match = {{ title = {json.dumps(title_pattern)} }}, "
            "no_anim = true, animation = 'none', float = true, pin = true, "
            "no_initial_focus = true, no_blur = true, no_shadow = true, "
            f"monitor = {json.dumps(screen.name() + ' silent')}, "
            f"move = {{ {local_x}, {local_y} }}, "
            f"size = {{ {size.width()}, {size.height()} }}, "
            "border_size = 0, rounding = 0 })"
        )
        try:
            result = subprocess.run(
                ["hyprctl", "eval", code],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False

        # Qt Tool surfaces publish an empty title to Hyprland.  Once the
        # selection overlay has closed these can be ordinary frameless
        # windows; WindowStaysOnTopHint and the compositor rule keep their HUD
        # behaviour while making the exact initial-title match available.
        flags = widget.windowFlags()
        flags &= ~Qt.WindowType.Tool
        flags |= Qt.WindowType.Window
        widget.setWindowFlags(flags)
        widget.setWindowTitle(title)
        return True

    def uses_single_recording_frame_window(self) -> bool:
        """Avoid four independently configured surfaces on Hyprland."""
        return (
            not self.places_windows()
            and setup_desktop.is_hyprland_session()
        )

    @staticmethod
    def _place_hyprland_recording_window(
        selector: str,
        top_left: QPoint,
        size: QSize,
    ) -> bool:
        """Float, pin, size and place one already-identified client."""
        selector_lua = json.dumps(selector)
        code = (
            f"local w={selector_lua}; "
            "hl.dispatch(hl.dsp.window.set_prop({prop='no_anim', value='1', window=w})); "
            "hl.dispatch(hl.dsp.window.set_prop({prop='no_blur', value='1', window=w})); "
            "hl.dispatch(hl.dsp.window.set_prop({prop='no_shadow', value='1', window=w})); "
            "hl.dispatch(hl.dsp.window.set_prop({prop='decorate', value='0', window=w})); "
            "hl.dispatch(hl.dsp.window.set_prop({prop='rounding', value='0', window=w})); "
            "hl.dispatch(hl.dsp.window.float({action='set', window=w})); "
            "hl.dispatch(hl.dsp.window.pin({action='set', window=w})); "
            f"hl.dispatch(hl.dsp.window.resize({{x={size.width()}, "
            f"y={size.height()}, relative=false, window=w}})); "
            f"hl.dispatch(hl.dsp.window.move({{x={top_left.x()}, y={top_left.y()}, "
            "relative=false, window=w})); "
            "hl.dispatch(hl.dsp.window.alter_zorder({mode='top', window=w}))"
        )
        try:
            result = subprocess.run(
                ["hyprctl", "eval", code],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    @staticmethod
    def _hyprland_process_window_addresses() -> set[str] | None:
        """Return every mapped Hyprland client owned by this process."""
        try:
            result = subprocess.run(
                ["hyprctl", "clients", "-j"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return None
            clients = json.loads(result.stdout)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return None
        return {
            client["address"]
            for client in clients
            if client.get("pid") == os.getpid() and client.get("address")
        }

    @classmethod
    def _hyprland_new_recording_window(
        cls, known: set[str]
    ) -> str | None:
        """Wait briefly for the one newly mapped frame edge."""
        attempts = 12
        for attempt in range(attempts):
            addresses = cls._hyprland_process_window_addresses()
            if addresses is None:
                return None
            new = addresses - known
            if len(new) == 1:
                return f"address:{new.pop()}"
            if len(new) > 1:
                return None
            if attempt < attempts - 1:
                time.sleep(0.025)
                QCoreApplication.processEvents(
                    QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                )
        return None

    @staticmethod
    def _hyprland_recording_window(
        size: QSize, *, title: str = ""
    ) -> str | None:
        """Return the address selector for this process's mapped HUD.

        The Hyprland path changes the original Qt ``Tool`` into an ordinary
        frameless window before its first map, so its private initial title
        is the stable identity even while the bar changes size between live
        and finished states.  Keep the exact-size lookup as a fallback for
        older/tool surfaces whose Wayland title Hyprland reports as empty.

        Mapping reaches Hyprland asynchronously.  Give it a short bounded
        window to appear instead of declaring failure on the first IPC read
        and removing controls that would have existed one frame later.
        """
        attempts = 12
        for attempt in range(attempts):
            try:
                result = subprocess.run(
                    ["hyprctl", "clients", "-j"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                clients = json.loads(result.stdout) if result.returncode == 0 else []
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                return None
            titled = [
                client
                for client in clients
                if title
                and client.get("pid") == os.getpid()
                and title in (client.get("title"), client.get("initialTitle"))
                and client.get("address")
            ]
            if len(titled) == 1:
                return f"address:{titled[0]['address']}"
            candidates = [
                client
                for client in clients
                if client.get("pid") == os.getpid()
                and client.get("floating") is True
                and client.get("size") == [size.width(), size.height()]
                and client.get("address")
            ]
            if len(candidates) == 1:
                return f"address:{candidates[0]['address']}"
            if attempt < attempts - 1:
                time.sleep(0.025)
                QCoreApplication.processEvents(
                    QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                )
        return None

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
        """Whether the selected Linux recording route can carry sound."""
        if (
            capture.detect_session_type() == "wayland"
            and shutil.which("gpu-screen-recorder") is not None
        ):
            return True
        capabilities = ffmpeg.probe()
        return capabilities is not None and capabilities.records_sound

    def audio_unavailable_reason(self) -> str:
        if (
            capture.detect_session_type() == "wayland"
            and shutil.which("gpu-screen-recorder") is not None
        ):
            return ""
        capabilities = ffmpeg.probe()
        if capabilities is None:
            return (
                "Recording sound on Linux needs ffmpeg installed -- GNOME's "
                "screen recorder has no audio of its own."
            )
        if capabilities.records_sound:
            return ""
        return (
            "This ffmpeg cannot record sound: it needs PulseAudio input and "
            "the Opus encoder."
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
