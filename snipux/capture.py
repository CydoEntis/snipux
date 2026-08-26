"""Capture-backend interface and the frozen-frame `Frame` type.

Per CLAUDE.md's one architectural rule, the entire virtual desktop is
captured in a single shot and everything downstream (selection, cropping,
annotation) operates on that frozen frame rather than asking the compositor
for pixels again. This module holds the `Frame` type and the
`CaptureBackend`/`BackendRegistry` abstraction that later, platform-specific
tickets register real backends into. No real backend lives here yet.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from jeepney import DBusAddress, MatchRule, new_method_call
from jeepney.io.blocking import open_dbus_connection

from PyQt6.QtCore import QPointF, QRect, QRectF, QSizeF, QUrl
from PyQt6.QtGui import QGuiApplication, QImage, QPainter


@dataclass
class Frame:
    """A captured virtual-desktop image plus the logical geometry it covers.

    `logical_origin`/`logical_size` are in logical (unscaled) coordinates —
    the same space window managers report monitor geometry in. `image` is
    the actual captured pixels, which may be a different size than
    `logical_size` under display scaling. The ratio between them is derived
    per-axis in `crop()` rather than trusted from a reported DPI value,
    because fractional scaling setups misreport it (see CLAUDE.md).
    """

    image: QImage
    logical_origin: QPointF
    logical_size: QSizeF

    def crop(self, logical_rect: QRectF) -> "Frame":
        """Return a new `Frame` covering `logical_rect` of this frame.

        `logical_rect` is in the same absolute, virtual-desktop coordinate
        space as `logical_origin` — it is not re-zeroed to this frame. The
        returned `Frame`'s `logical_origin`/`logical_size` are exactly
        `logical_rect`'s top-left/size, so callers (overlay, editor) can keep
        reasoning in logical coordinates after cropping.

        Scaling uses independent x/y ratios (image pixels per logical unit
        on each axis) rather than one scalar, since nothing guarantees the
        two axes scale identically — a single shared ratio would silently
        produce wrong crops on a mixed-DPI multi-monitor setup.
        """
        scale_x = self.image.width() / self.logical_size.width()
        scale_y = self.image.height() / self.logical_size.height()

        # Translate the absolute logical rect into image-local coordinates
        # by subtracting this frame's origin *before* scaling — this is
        # what keeps a negative virtual-desktop origin (a monitor above or
        # left of the primary) correct.
        px_x = (logical_rect.x() - self.logical_origin.x()) * scale_x
        px_y = (logical_rect.y() - self.logical_origin.y()) * scale_y

        # Width/height are the *difference* of rounded edges, not an
        # independently-rounded width/height. Under fractional scaling
        # (1.25x, 1.5x — GNOME's common case) rounding width separately from
        # x can put a crop's right edge a pixel away from where the next
        # crop's left edge rounds to, so adjacent crops would fail to tile
        # exactly. Rounding both edges and subtracting keeps them consistent.
        left = round(px_x)
        top = round(px_y)
        right = round(px_x + logical_rect.width() * scale_x)
        bottom = round(px_y + logical_rect.height() * scale_y)

        pixel_rect = QRect(left, top, right - left, bottom - top)
        cropped_image = self.image.copy(pixel_rect)

        return Frame(
            image=cropped_image,
            logical_origin=QPointF(logical_rect.x(), logical_rect.y()),
            logical_size=QSizeF(logical_rect.width(), logical_rect.height()),
        )


class CaptureBackend(ABC):
    """A way of grabbing the virtual desktop on a particular session type."""

    @abstractmethod
    def name(self) -> str:
        """A short, human-readable identifier, e.g. 'grim' or 'qt-native'."""

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this backend can plausibly run in the current session."""

    def unavailable_reason(self) -> str | None:
        """Why `is_available()` is False, or None if it is True.

        Kept as a separate accessor rather than folded into
        `is_available()` so `BackendRegistry.available()`'s filter stays a
        plain boolean check.
        """
        return None

    @abstractmethod
    def capture(self) -> Frame:
        """Grab the entire virtual desktop in a single shot."""


def _missing_backend_advice() -> str:
    """A package to suggest installing, chosen for the detected session type.

    Session type is read fresh here (rather than threaded in from the
    caller) so `CaptureError` can compute its own message from just the
    failures list. Naming a session-specific package rather than every
    known backend's matters: telling a Wayland user to install maim (an
    X11-only tool) would send them chasing a fix that can't work for them.

    Checked first, before any of that Linux-only reasoning: on Windows
    "install grim/maim via apt" is not merely unhelpful, it's advice for a
    different OS entirely. `build_windows_registry()`'s own backends gate
    themselves with `is_available()`, so this only fires there if neither
    can even be tried -- still worth a platform-appropriate message rather
    than falling through to Linux's.
    """
    if sys.platform == "win32":
        return "check that this build of Snipux includes Windows capture support"
    session_type = detect_session_type()
    if session_type == "wayland":
        return "install grim (e.g. `sudo apt install grim`)"
    if session_type == "x11":
        return "install maim (e.g. `sudo apt install maim`)"
    return "install grim for Wayland or maim for X11 (e.g. `sudo apt install grim maim`)"


class CaptureError(Exception):
    """Raised by `BackendRegistry.capture()` when every backend fails.

    Carries every `(backend_name, exception)` pair collected along the way,
    not just the last one, so failures can be reported together per
    CLAUDE.md's "a capture backend that fails must not stop the next one"
    rule. `failures` is empty specifically when no backend was even
    available to try (a freshly installed machine with no capture tooling)
    — that case gets its own message rather than falling through to "all
    capture backends failed: " with nothing after the colon, since a
    fresh-install user is exactly who this message needs to be useful for.
    """

    def __init__(self, failures: list[tuple[str, Exception]]):
        self.failures = failures
        if not failures:
            super().__init__(f"no capture backend is available: {_missing_backend_advice()}")
            return
        summary = "; ".join(f"{name}: {exc}" for name, exc in failures)
        super().__init__(f"all capture backends failed: {summary}")


class BackendRegistry:
    """An ordered collection of `CaptureBackend`s, tried in order."""

    def __init__(self, backends: list[CaptureBackend] | None = None):
        self._backends = list(backends) if backends else []

    def __iter__(self):
        return iter(self._backends)

    def __len__(self) -> int:
        return len(self._backends)

    def add(self, backend: CaptureBackend) -> None:
        self._backends.append(backend)

    def available(self) -> list[CaptureBackend]:
        """Backends whose `is_available()` is True, in registration order."""
        return [b for b in self._backends if b.is_available()]

    def capture(self) -> Frame:
        """Try available backends in order; return the first successful Frame.

        A backend raising does not stop the next one from being tried. If
        every available backend fails, raises `CaptureError` carrying all
        collected failures.
        """
        failures: list[tuple[str, Exception]] = []
        for backend in self.available():
            try:
                return backend.capture()
            except Exception as exc:  # noqa: BLE001 - collected, not swallowed
                failures.append((backend.name(), exc))
        raise CaptureError(failures)


def detect_session_type() -> str:
    """Return 'wayland', 'x11', or 'unknown' based on XDG_SESSION_TYPE.

    Read at runtime, never assumed, per CLAUDE.md — the session type
    determines backend order and must reflect the environment the process
    is actually running in.
    """
    session_type = os.environ.get("XDG_SESSION_TYPE")
    if session_type == "wayland":
        return "wayland"
    if session_type == "x11":
        return "x11"
    return "unknown"


def _virtual_desktop_geometry() -> QRectF:
    """Union of every screen's logical geometry, in absolute logical coords.

    Same union-of-rects pattern `create_overlays()` uses in overlay.py,
    extracted once here so the Qt-native and shell-out X11 backends below
    don't each recompute it independently.
    """
    union: QRectF | None = None
    for screen in QGuiApplication.screens():
        geometry = QRectF(screen.geometry())
        union = geometry if union is None else union.united(geometry)
    return union if union is not None else QRectF()


def _x11_shell_backend_available(binary: str) -> tuple[bool, str | None]:
    """Shared "am I usable" check for every shell-out X11 backend.

    Session type is checked before the binary so the reported reason is
    the more useful one when both would fail — e.g. on Wayland with `maim`
    installed, the reason should say "not an X11 session", not report a
    missing binary that isn't actually the blocker.
    """
    if detect_session_type() != "x11":
        return False, "not an X11 session"
    if shutil.which(binary) is None:
        return False, f"{binary} not found on PATH"
    return True, None


def _grab_all_screens_composited(ratio: float) -> Frame:
    """Grab every screen individually via `QScreen.grabWindow(0)` and
    composite them into one image at `ratio`, each placed at its own offset
    from the virtual desktop's origin.

    Shared by every Qt-native backend (X11, Windows): there is no single Qt
    call that hands back the whole virtual desktop in one shot on any
    platform, `grabWindow(0)` only ever returns that one screen's own
    pixels -- so covering every monitor means grabbing each and placing it
    by its own logical geometry, same as this used to do inline in
    `QtNativeX11Backend.capture()` before Windows needed the identical
    logic (SNX-88).

    Raises if any screen's grab comes back null -- silently painting
    nothing for that monitor would produce a frame that looks complete but
    is missing real content, which is worse than failing loudly and letting
    `BackendRegistry.capture()` move on to the next backend.
    """
    virtual_rect = _virtual_desktop_geometry()
    origin = virtual_rect.topLeft()

    image = QImage(
        round(virtual_rect.width() * ratio),
        round(virtual_rect.height() * ratio),
        QImage.Format.Format_RGB32,
    )
    image.fill(0)

    painter = QPainter(image)
    for screen in QGuiApplication.screens():
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            painter.end()
            raise RuntimeError("qt-native: grabWindow returned an empty image for a screen")
        geometry = QRectF(screen.geometry())
        target = QRectF(
            (geometry.x() - origin.x()) * ratio,
            (geometry.y() - origin.y()) * ratio,
            geometry.width() * ratio,
            geometry.height() * ratio,
        )
        painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
    # Closed before Frame() is constructed below, never left open across a
    # read of the image it painted, per CLAUDE.md.
    painter.end()

    return Frame(image=image, logical_origin=origin, logical_size=virtual_rect.size())


class QtNativeX11Backend(CaptureBackend):
    """Captures via Qt's own `QScreen.grabWindow` — no process spawn.

    Registered first: it costs nothing to check and spawns nothing to run,
    so it's preferred over shelling out whenever it's usable.
    """

    def name(self) -> str:
        return "qt-native"

    def is_available(self) -> bool:
        return detect_session_type() == "x11"

    def unavailable_reason(self) -> str | None:
        return None if self.is_available() else "not an X11 session"

    def capture(self) -> Frame:
        # One session-wide ratio, taken from the primary screen, rather
        # than each screen's own devicePixelRatio() — Frame/crop() can only
        # represent a single scale factor for the whole image, so
        # compositing per-screen ratios would get the canvas size right
        # while silently misplacing/missizing whichever monitor doesn't
        # match. GNOME/X11 scaling is session-wide in practice anyway; true
        # per-monitor DPI on X11 would need a Frame-level model change,
        # which is out of scope here.
        ratio = QGuiApplication.primaryScreen().devicePixelRatio()
        return _grab_all_screens_composited(ratio)


class _ShellOutX11Backend(CaptureBackend):
    """Shared plumbing for capture backends that shell out to a screenshot
    tool. Subclasses only supply a name, the binary to check for, and the
    argv that writes a capture to a given path.
    """

    def __init__(self, backend_name: str, binary: str):
        self._name = backend_name
        self._binary = binary

    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        available, _reason = _x11_shell_backend_available(self._binary)
        return available

    def unavailable_reason(self) -> str | None:
        _available, reason = _x11_shell_backend_available(self._binary)
        return reason

    def _command(self, path: str) -> list[str]:
        """The argv to run, writing the capture to `path`."""
        raise NotImplementedError

    def capture(self) -> Frame:
        # These tools capture in X-server pixel space and have no notion of
        # Qt's logical coordinate system, so the logical geometry to report
        # comes from Qt, not from the tool's own output.
        virtual_rect = _virtual_desktop_geometry()

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        path = tmp.name
        try:
            # A missing binary here (rather than at is_available() time) is
            # a TOCTOU race, not a "can't happen" — it still raises and lets
            # BackendRegistry.capture() move on to the next backend.
            subprocess.run(self._command(path), check=True)
            image = QImage(path)
            if image.isNull():
                raise RuntimeError(f"{self._name}: produced an unreadable image")
        finally:
            os.remove(path)

        return Frame(
            image=image,
            logical_origin=virtual_rect.topLeft(),
            logical_size=virtual_rect.size(),
        )


class MaimBackend(_ShellOutX11Backend):
    """Second in priority order: a lightweight, X11-only screenshot tool."""

    def __init__(self):
        super().__init__("maim", "maim")

    def _command(self, path: str) -> list[str]:
        return ["maim", path]


class ImportBackend(_ShellOutX11Backend):
    """Third in priority order: ImageMagick's `import`."""

    def __init__(self):
        super().__init__("import", "import")

    def _command(self, path: str) -> list[str]:
        # -window root is what makes this non-interactive; plain `import`
        # waits for the user to click a target window/region.
        return ["import", "-window", "root", path]


class ScrotBackend(_ShellOutX11Backend):
    """Fourth in priority order: scrot."""

    def __init__(self):
        super().__init__("scrot", "scrot")

    def _command(self, path: str) -> list[str]:
        return ["scrot", path]


class XwininfoWindowGeometryProvider:
    """Window geometry from `xwininfo`, for X11 sessions without `wmctrl`.

    Same duck-typed shape as `X11WindowGeometryProvider`, and used only when
    that one cannot run. `wmctrl` is not installed by default on Ubuntu, so
    without this fallback Window mode silently reverted to Region on a stock
    desktop -- the mode was in the menu, picking it did nothing visible, and
    the only clue was a toast. `xwininfo` ships in x11-utils, which a GNOME
    session already pulls in.

    One `xwininfo -root -children` call lists the root's direct children
    with their geometry, which is what a window picker needs. Its output is
    less curated than `wmctrl -lG`: panels, docks and 1x1 helper windows are
    in it too, so anything unmapped or implausibly small is dropped here
    rather than offered as something to capture.
    """

    _CACHE_SECONDS = 0.2
    # Below this, in either axis, a "window" is a helper or an input proxy,
    # not something a user meant to capture.
    _MIN_SIDE = 80

    # Windows that exist but are nobody's idea of a capture target. The
    # compositor's backdrop is the one that actually matters: it spans the
    # whole desktop and sits above everything, so left in the list it would
    # be the answer to every hover.
    _NOT_WINDOWS = frozenset({"mutter guard window"})

    # "     0x3400011 "snipux": ("snipux" "Snipux")  1200x800+100+50  +100+50"
    _LINE_RE = re.compile(
        r'^\s*(0x[0-9a-f]+)\s+(?:"([^"]*)")?.*?'
        r"\s(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s+\+(-?\d+)\+(-?\d+)\s*$"
    )

    def __init__(self):
        self._cache: list[tuple[str, QRectF]] | None = None
        self._cache_time: float | None = None

    def is_available(self) -> bool:
        return detect_session_type() == "x11" and shutil.which("xwininfo") is not None

    def list_windows(self) -> list[tuple[str, QRectF]]:
        now = time.monotonic()
        if (
            self._cache is not None
            and self._cache_time is not None
            and now - self._cache_time < self._CACHE_SECONDS
        ):
            return self._cache
        windows = self._list_windows_uncached()
        self._cache = windows
        self._cache_time = now
        return windows

    def _list_windows_uncached(self) -> list[tuple[str, QRectF]]:
        if shutil.which("xwininfo") is None:
            return []
        try:
            result = subprocess.run(
                ["xwininfo", "-root", "-children"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return []

        desktop = _virtual_desktop_geometry()
        windows: list[tuple[str, QRectF]] = []
        for line in result.stdout.splitlines():
            match = self._LINE_RE.match(line)
            if match is None:
                continue
            _win_id, title, width, height, _x, _y, abs_x, abs_y = match.groups()
            try:
                rect = QRectF(float(abs_x), float(abs_y), float(width), float(height))
            except ValueError:
                continue
            if rect.width() < self._MIN_SIDE or rect.height() < self._MIN_SIDE:
                continue
            if (title or "") in self._NOT_WINDOWS:
                continue
            # A window the exact size of the whole virtual desktop is the
            # compositor's own backdrop, not something anybody meant to
            # capture -- and offered as a pick it would swallow every real
            # window behind it, since it is on top of all of them.
            if rect.contains(desktop) or rect == desktop:
                continue
            windows.append((title or "", rect))
        # Reversed: xwininfo lists children bottom-of-stack first, and
        # `window_at` takes the first match, which must be the topmost.
        windows.reverse()
        return windows

    def window_at(self, point: QPointF) -> QRectF | None:
        for _title, rect in self.list_windows():
            if rect.contains(point):
                return rect
        return None


class X11WindowGeometryProvider:
    """Real per-window geometry source for X11's window-selection mode.

    Duck-types `overlay.GeometryProvider`'s shape (`is_available()`,
    `window_at()`) without importing it, since `overlay.py` already imports
    `Frame` from this module and importing back would create a cycle.
    """

    # overlay.py's window mode calls window_at() from mouseMoveEvent, i.e.
    # at mouse-move frequency (easily 100+/s) while the user hovers looking
    # for a window to click. Re-running `wmctrl` that often would make the
    # hover-highlight stutter behind the cursor, so results are cached for
    # a short interval instead of shelling out on every call. The window
    # list changing (opened/closed/moved) mid-hover a few hundred
    # milliseconds late is not something a user can perceive; a stale
    # cursor is.
    _CACHE_SECONDS = 0.2

    def __init__(self):
        self._cache: list[tuple[str, QRectF]] | None = None
        self._cache_time: float | None = None

    def list_windows(self) -> list[tuple[str, QRectF]]:
        """(title, absolute logical rect) for every on-screen window.

        One shelled-out `wmctrl -lG` call, not one process per window —
        and not one process per call either, thanks to the short-lived
        cache described above. Returns `[]` — not an exception — when
        `wmctrl` is missing or fails, consistent with CLAUDE.md's "a
        backend that fails must not stop the next one" applied to this
        provider too.
        """
        now = time.monotonic()
        if (
            self._cache is not None
            and self._cache_time is not None
            and now - self._cache_time < self._CACHE_SECONDS
        ):
            return self._cache

        windows = self._list_windows_uncached()
        self._cache = windows
        self._cache_time = now
        return windows

    def _list_windows_uncached(self) -> list[tuple[str, QRectF]]:
        if shutil.which("wmctrl") is None:
            return []
        try:
            result = subprocess.run(
                ["wmctrl", "-lG"], check=True, capture_output=True, text=True
            )
        except (OSError, subprocess.CalledProcessError):
            return []

        windows = []
        for line in result.stdout.splitlines():
            # Columns: id desktop x y width height host title... — capped
            # split count so a title containing spaces survives intact
            # instead of being chopped into extra fields.
            fields = line.split(None, 7)
            if len(fields) < 8:
                continue
            _win_id, _desktop, x, y, width, height, _host, title = fields
            try:
                rect = QRectF(float(x), float(y), float(width), float(height))
            except ValueError:
                continue
            windows.append((title, rect))
        return windows

    def is_available(self) -> bool:
        # Mirrors CaptureBackend.is_available()'s shape intentionally, per
        # overlay.GeometryProvider's docstring.
        return detect_session_type() == "x11" and shutil.which("wmctrl") is not None

    def window_at(self, point: QPointF) -> QRectF | None:
        # wmctrl -lG's output order isn't a guaranteed stacking order, so
        # this is "first match", not "topmost match" — a real limitation,
        # not a silent claim of correctness it can't back up.
        for _title, rect in self.list_windows():
            if rect.contains(point):
                return rect
        return None


def build_x11_registry() -> BackendRegistry:
    """The real X11 `BackendRegistry`: qt-native, maim, import, scrot."""
    registry = BackendRegistry()
    registry.add(QtNativeX11Backend())
    registry.add(MaimBackend())
    registry.add(ImportBackend())
    registry.add(ScrotBackend())
    return registry


def _wayland_shell_backend_available(binary: str) -> tuple[bool, str | None]:
    """Wayland counterpart of `_x11_shell_backend_available`.

    Kept as its own function rather than generalized across session types —
    Wayland's shell-out backend (grim) has different flags/output-selection
    needs than the X11 tools, and CLAUDE.md scopes platform differences to
    stay confined within this file, not necessarily into one shared helper.
    """
    if detect_session_type() != "wayland":
        return False, "not a Wayland session"
    if shutil.which(binary) is None:
        return False, f"{binary} not found on PATH"
    return True, None


class GrimBackend(CaptureBackend):
    """First in priority order: grim, the wlroots screenshot utility.

    Run with no `-o`/`-g` it stitches every output into one image, which is
    exactly the "whole virtual desktop in one shot" contract `Frame`
    expects — logical geometry still comes from `_virtual_desktop_geometry()`
    rather than grim's own output, since grim has no notion of Qt's logical
    coordinate space, same reason `_ShellOutX11Backend` does that.
    """

    def name(self) -> str:
        return "grim"

    def is_available(self) -> bool:
        available, _reason = _wayland_shell_backend_available("grim")
        return available

    def unavailable_reason(self) -> str | None:
        _available, reason = _wayland_shell_backend_available("grim")
        return reason

    def capture(self) -> Frame:
        virtual_rect = _virtual_desktop_geometry()

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        path = tmp.name
        try:
            # grim owns nothing after the call returns; the tempfile is
            # ours to create and remove, same as every X11 shell-out
            # backend.
            subprocess.run(["grim", path], check=True)
            image = QImage(path)
            if image.isNull():
                raise RuntimeError("grim: produced an unreadable image")
        finally:
            os.remove(path)

        return Frame(
            image=image,
            logical_origin=virtual_rect.topLeft(),
            logical_size=virtual_rect.size(),
        )


class PortalScreenshotBackend(CaptureBackend):
    """Second in priority order: xdg-desktop-portal's Screenshot method,
    over the session bus via jeepney.

    The portal's request/response handshake has a documented failure mode:
    if the client sends the `Screenshot` call and only afterwards starts
    listening for the `Request` object's `Response` signal, a fast-replying
    portal's response can arrive in the gap and be missed forever. Avoiding
    that is the entire reason `capture()` below computes the request path
    and subscribes to it (`_subscribe`) *before* sending the request
    (`_send_request`), rather than the more obvious "call, then wait for
    reply" shape.
    """

    _BUS_NAME = "org.freedesktop.portal.Desktop"
    _OBJECT_PATH = "/org/freedesktop/portal/desktop"
    _INTERFACE = "org.freedesktop.portal.Screenshot"
    _REQUEST_INTERFACE = "org.freedesktop.portal.Request"
    _RESPONSE_TIMEOUT_SECONDS = 30

    def name(self) -> str:
        return "portal"

    def is_available(self) -> bool:
        # No D-Bus round-trip here: opening a connection isn't free, and
        # is_available() is called by BackendRegistry.available() on every
        # capture() attempt across the whole registry, not just this one.
        return detect_session_type() == "wayland"

    def unavailable_reason(self) -> str | None:
        return None if self.is_available() else "not a Wayland session"

    def _new_handle_token(self) -> str:
        # Must be unique per request: reusing a token would collide with
        # the Request object path of an earlier, possibly still-pending
        # call from this same connection.
        return f"snipux_{uuid.uuid4().hex}"

    def _request_path(self, unique_name: str, handle_token: str) -> str:
        # Per the xdg-desktop-portal spec, the Request object path is
        # deterministic from the caller's unique bus name (':1.23' becomes
        # '1_23') and the handle_token passed in the call's options — known
        # before the call is sent, not extracted from its reply. That's
        # what makes subscribing-before-sending possible at all.
        sender = unique_name.lstrip(":").replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{sender}/{handle_token}"

    def _subscribe(self, connection, request_path: str):
        """Start listening for `request_path`'s `Response` signal.

        Split out from `capture()` so a test can assert this runs strictly
        before `_send_request()` — the ordering the portal's documented
        failure mode depends on.
        """
        rule = MatchRule(
            type="signal",
            interface=self._REQUEST_INTERFACE,
            member="Response",
            path=request_path,
        )
        return connection.filter(rule)

    def _send_request(self, connection, handle_token: str) -> None:
        """Send the `Screenshot` method call. Must run after `_subscribe`."""
        portal = DBusAddress(
            self._OBJECT_PATH, bus_name=self._BUS_NAME, interface=self._INTERFACE
        )
        message = new_method_call(
            portal,
            "Screenshot",
            "sa{sv}",
            (
                "",
                {
                    "handle_token": ("s", handle_token),
                    # A process the keybinding just spawned (see SNX-67) has
                    # no window yet, so it has no parent to hand the portal
                    # for a *modal* dialog — and with `modal` left at the
                    # spec's default of true, GNOME's portal backend refuses
                    # the request outright rather than show an unparented
                    # modal one, with no dialog ever appearing on screen. A
                    # long-lived resident instance can go on to open windows
                    # of its own, so it doesn't hit this; a freshly spawned
                    # one asking for its very first frame does. Asking for a
                    # non-modal dialog is what lets GNOME show it regardless
                    # of whether the caller has a window at all.
                    "modal": ("b", False),
                    # Explicit, not just relying on the spec default of
                    # false: we always want the whole frozen frame handed
                    # back untouched, never a picker that lets the user crop
                    # before we ever see pixels — that would bypass "select
                    # in our own overlay" from CLAUDE.md's one architectural
                    # rule.
                    "interactive": ("b", False),
                },
            ),
        )
        connection.send(message)

    def capture(self) -> Frame:
        virtual_rect = _virtual_desktop_geometry()

        connection = open_dbus_connection(bus="SESSION")
        try:
            handle_token = self._new_handle_token()
            request_path = self._request_path(connection.unique_name, handle_token)

            # Subscribe before sending — see the class docstring. This
            # ordering, not the D-Bus plumbing around it, is the point of
            # this backend.
            filter_handle = self._subscribe(connection, request_path)
            try:
                self._send_request(connection, handle_token)
                response = connection.recv_until_filtered(
                    filter_handle.queue, timeout=self._RESPONSE_TIMEOUT_SECONDS
                )
            finally:
                filter_handle.close()

            response_code, results = response.body
            if response_code == 1:
                # The user was shown the permission/picker dialog and
                # dismissed it — retrying with the same action is exactly
                # the right next step, so say that rather than the generic
                # "did not succeed".
                raise RuntimeError(
                    "portal: screenshot request was cancelled — press the "
                    "shortcut again and approve the permission prompt"
                )
            if response_code != 0:
                # Any other non-zero code (2 = error, and anything the spec
                # doesn't define) is not something pressing the shortcut
                # again fixes by itself, so point at the portal
                # installation instead. Either way `results` has no "uri"
                # to read, so this must raise here rather than letting a
                # bare KeyError stand in for it below.
                raise RuntimeError(
                    f"portal: screenshot request failed (response code {response_code}) "
                    "— check that xdg-desktop-portal and a Wayland portal "
                    "backend (e.g. xdg-desktop-portal-gnome) are installed and running"
                )
            uri = results["uri"][1]
            image_path = QUrl(uri).toLocalFile()
            image = QImage(image_path)
            if image.isNull():
                raise RuntimeError("portal: produced an unreadable image")
        finally:
            connection.close()

        return Frame(
            image=image,
            logical_origin=virtual_rect.topLeft(),
            logical_size=virtual_rect.size(),
        )


class GnomeShellHelperBackend(CaptureBackend):
    """Third and last-resort fallback: GNOME Shell's own D-Bus screenshot
    method, `org.gnome.Shell.Screenshot`.

    Unlike the portal, this is a single synchronous method call with no
    request/response-signal handshake, so no subscribe-before-send concern
    applies here. Unlike the portal (which owns wherever it saves its
    screenshot and hands back a URI we merely read), we supply `filename`
    to this call ourselves — same as every `_ShellOutX11Backend` subclass
    creating its own tempfile before shelling out — so it gets the same
    tempfile/`finally`-cleanup treatment.
    """

    _BUS_NAME = "org.gnome.Shell"
    _OBJECT_PATH = "/org/gnome/Shell"
    _INTERFACE = "org.gnome.Shell"

    def name(self) -> str:
        return "gnome-shell-helper"

    def is_available(self) -> bool:
        return detect_session_type() == "wayland"

    def unavailable_reason(self) -> str | None:
        return None if self.is_available() else "not a Wayland session"

    def capture(self) -> Frame:
        virtual_rect = _virtual_desktop_geometry()

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        path = tmp.name
        try:
            connection = open_dbus_connection(bus="SESSION")
            try:
                shell = DBusAddress(
                    self._OBJECT_PATH, bus_name=self._BUS_NAME, interface=self._INTERFACE
                )
                message = new_method_call(
                    shell, "Screenshot", "bbs", (False, False, path)
                )
                reply = connection.send_and_get_reply(message)
            finally:
                connection.close()

            success, filename = reply.body
            if not success:
                # No backend after this one, so this surfaces to the
                # caller wrapped in BackendRegistry.capture()'s CaptureError.
                raise RuntimeError("gnome-shell-helper: Screenshot() reported failure")

            image = QImage(filename)
            if image.isNull():
                raise RuntimeError("gnome-shell-helper: produced an unreadable image")
        finally:
            os.remove(path)

        return Frame(
            image=image,
            logical_origin=virtual_rect.topLeft(),
            logical_size=virtual_rect.size(),
        )


def build_wayland_registry() -> BackendRegistry:
    """The real Wayland `BackendRegistry`: grim, portal, gnome-shell-helper."""
    registry = BackendRegistry()
    registry.add(GrimBackend())
    registry.add(PortalScreenshotBackend())
    registry.add(GnomeShellHelperBackend())
    return registry


def build_linux_registry() -> BackendRegistry:
    """The real Linux `BackendRegistry`, chosen by `detect_session_type()`.

    Wayland gets `build_wayland_registry()`, X11 gets `build_x11_registry()`,
    and an unrecognised session type gets both, concatenated -- every
    backend already gates itself with its own `is_available()`, so offering
    both lets whatever is actually installed be found instead of failing
    outright because the session type couldn't be determined. This is what
    `platform.linux.LinuxPlatform.build_capture_registry()` (SNX-86) calls;
    it used to be `app.build_default_registry()`'s own logic before that
    choice moved behind the platform seam.
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


class QtNativeWindowsBackend(CaptureBackend):
    """Captures via Qt's own `QScreen.grabWindow`, composited across every
    screen by `_grab_all_screens_composited` -- the same routine
    `QtNativeX11Backend` uses.

    Unlike Wayland, where a client cannot read the screen directly and
    `grabWindow` comes back black, Windows lets Qt read real pixels this
    way -- confirmed on an actual three-monitor Windows desktop (one
    screen to the right of the primary, one above-and-left of it, i.e. a
    virtual desktop with both a negative x and a negative y origin): every
    monitor came back with its own real, distinct content, not a black
    image and not three copies of the primary. Registered first, same
    reasoning as `QtNativeX11Backend`: free to check, nothing to spawn.
    """

    def name(self) -> str:
        return "qt-native"

    def is_available(self) -> bool:
        return sys.platform == "win32"

    def unavailable_reason(self) -> str | None:
        return None if self.is_available() else "not running on Windows"

    def capture(self) -> Frame:
        ratio = QGuiApplication.primaryScreen().devicePixelRatio()
        return _grab_all_screens_composited(ratio)


class _BitmapInfoHeader(ctypes.Structure):
    """Win32 `BITMAPINFOHEADER`, just enough of it for `GetDIBits()` below
    -- ctypes has no symbolic version of this struct built in."""

    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class Win32GdiBackend(CaptureBackend):
    """Fallback for Windows when Qt's own `grabWindow` doesn't cover the
    whole virtual desktop -- goes straight to the Win32 GDI API via
    `ctypes` instead. Stdlib, not a new dependency; see CLAUDE.md on why
    that distinction matters here.

    One `BitBlt` from the desktop DC, sized and positioned by
    `GetSystemMetrics(SM_*VIRTUALSCREEN)`, grabs every monitor's pixels in
    a single call -- the OS handing back the whole virtual desktop at
    once, rather than one grab per monitor composited afterwards
    client-side the way the qt-native backend does.

    The resulting image's *physical* pixel size can differ from Qt's own
    logical geometry under display scaling; `Frame.crop()` already
    tolerates exactly that mismatch (see its own docstring), so logical
    origin/size here are still taken from `_virtual_desktop_geometry()`,
    same as every shell-out backend in this file — not trusted from GDI's
    own physical-pixel numbers, which have no notion of Qt's logical space.
    """

    _SM_XVIRTUALSCREEN = 76
    _SM_YVIRTUALSCREEN = 77
    _SM_CXVIRTUALSCREEN = 78
    _SM_CYVIRTUALSCREEN = 79
    _SRCCOPY = 0x00CC0020

    def name(self) -> str:
        return "win32-gdi"

    def is_available(self) -> bool:
        return sys.platform == "win32"

    def unavailable_reason(self) -> str | None:
        return None if self.is_available() else "not running on Windows"

    def capture(self) -> Frame:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        width = user32.GetSystemMetrics(self._SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(self._SM_CYVIRTUALSCREEN)
        phys_x = user32.GetSystemMetrics(self._SM_XVIRTUALSCREEN)
        phys_y = user32.GetSystemMetrics(self._SM_YVIRTUALSCREEN)
        if width <= 0 or height <= 0:
            raise RuntimeError("win32-gdi: GetSystemMetrics reported an empty virtual screen")

        screen_dc = user32.GetDC(0)
        if not screen_dc:
            raise RuntimeError("win32-gdi: GetDC(0) failed")
        try:
            image = self._blit_to_image(gdi32, screen_dc, phys_x, phys_y, width, height)
        finally:
            # Ours to release: GetDC(0) hands back a shared DC to the whole
            # screen, not one this call owns, and leaking it degrades every
            # other app's drawing until the process exits.
            user32.ReleaseDC(0, screen_dc)

        if image.isNull():
            raise RuntimeError("win32-gdi: produced an unreadable image")

        virtual_rect = _virtual_desktop_geometry()
        return Frame(
            image=image,
            logical_origin=virtual_rect.topLeft(),
            logical_size=virtual_rect.size(),
        )

    def _blit_to_image(self, gdi32, screen_dc, x, y, width, height) -> QImage:
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
        try:
            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, x, y, self._SRCCOPY):
                raise RuntimeError("win32-gdi: BitBlt failed")

            header = _BitmapInfoHeader()
            header.biSize = ctypes.sizeof(_BitmapInfoHeader)
            header.biWidth = width
            # Negative: a top-down DIB, matching QImage's own row order --
            # without this the image comes back upside down.
            header.biHeight = -height
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0  # BI_RGB

            buffer = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
                raise RuntimeError("win32-gdi: GetDIBits failed")

            # .copy(): QImage(buffer, ...) only aliases the buffer, which
            # goes out of scope (and can be freed) once this method
            # returns -- the Frame this builds must own its own pixels.
            return QImage(
                buffer, width, height, width * 4, QImage.Format.Format_RGB32
            ).copy()
        finally:
            gdi32.SelectObject(mem_dc, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)


def build_windows_registry() -> BackendRegistry:
    """The real Windows `BackendRegistry`: qt-native first, then the Win32
    GDI fallback for whenever it doesn't cover the whole virtual desktop.
    """
    registry = BackendRegistry()
    registry.add(QtNativeWindowsBackend())
    registry.add(Win32GdiBackend())
    return registry


class _RECT(ctypes.Structure):
    """Win32 `RECT`, shared by `GetWindowRect` and
    `DwmGetWindowAttribute(..., DWMWA_EXTENDED_FRAME_BOUNDS, ...)` below --
    ctypes has no symbolic version of this struct built in either, same as
    `_BitmapInfoHeader` above.
    """

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    """Win32 `MONITORINFO`, just enough of it for `GetMonitorInfoW()` below
    -- ctypes has no symbolic version of this struct either, same as
    `_RECT` above. `cbSize` must be set to `sizeof(_MonitorInfo)` before
    the call, per the Win32 convention every size-prefixed struct here
    follows.
    """

    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


class WindowsWindowGeometryProvider:
    """Real per-window geometry source for Windows' window-selection mode
    (SNX-90).

    Duck-types `overlay.GeometryProvider`'s shape (`is_available()`,
    `window_at()`) without importing it, same reasoning as
    `X11WindowGeometryProvider` above: `overlay.py` already imports `Frame`
    from this module, so importing back would create a cycle.

    Unlike X11 -- where a client cannot enumerate other windows itself on
    Wayland, so `wmctrl`/`xwininfo` have to ask a window manager instead --
    Windows has no such restriction: `EnumWindows` answers directly, via
    `ctypes`, no new dependency (per CLAUDE.md).

    Several Windows-specific wrinkles decide whether the result actually
    looks right:

    * `GetWindowRect` includes the invisible resize border DWM draws
      around a modern window (a few pixels of dead space for resize
      grabbing that is not part of what the user sees) -- padding the
      snapped selection out unless the *extended frame bounds* DWM tracks
      separately (`DwmGetWindowAttribute` with
      `DWMWA_EXTENDED_FRAME_BOUNDS`) are used instead. `GetWindowRect` is
      only a fallback, for the rare case `DwmGetWindowAttribute` itself
      fails.
    * A window can be enumerated by `EnumWindows` while hidden, minimised,
      or cloaked (DWM hides a window this way during a virtual-desktop
      switch or a suspended UWP app, without closing it) -- all three are
      dropped here, or the picker would snap to something the user cannot
      see.
    * The shell's own desktop and workspace windows (Progman/WorkerW,
      hosting the desktop icons/wallpaper) and the taskbar
      (Shell_TrayWnd/Shell_SecondaryTrayWnd) are real, visible,
      non-minimised top-level windows -- they pass every check above and
      would otherwise be offered as pickable "windows" (SNX-94: probing an
      empty area of the desktop returned the entire virtual desktop, as
      wide as every monitor combined, as though it were one). Excluded by
      class name (`_is_shell_window`), since that is what actually
      identifies them -- there is no visibility flag or attribute that
      says "this is shell chrome, not an application".
    * As a second, independent line of defense, a window whose rect is
      larger than the monitor it sits on is never offered either
      (`_is_larger_than_its_monitor`) -- Progman/WorkerW would already
      fail this (they span the whole virtual desktop, wider than any one
      monitor), but this also catches anything else sized past its own
      monitor without happening to be one of the four named shell
      classes. A window exactly the size of the monitor it's on -- a
      genuine full-screen window, e.g. a borderless game -- is not
      "larger than" it and passes this check.
    """

    # Mirrors X11WindowGeometryProvider._CACHE_SECONDS: overlay.py's window
    # mode calls window_at() from mouseMoveEvent, at mouse-move frequency,
    # so re-enumerating every window on every hover would make the
    # highlight stutter behind the cursor.
    _CACHE_SECONDS = 0.2
    _DWMWA_CLOAKED = 14
    _DWMWA_EXTENDED_FRAME_BOUNDS = 9
    _MONITOR_DEFAULTTONEAREST = 2

    # Win32 class names of the shell's own chrome -- Progman and WorkerW
    # host the desktop wallpaper/icons (WorkerW is created alongside
    # Progman on modern Windows and can also span the whole virtual
    # desktop), Shell_TrayWnd is the primary taskbar and
    # Shell_SecondaryTrayWnd is a taskbar on a secondary monitor. None of
    # these is something a user ever means by "window".
    _SHELL_CLASS_NAMES = frozenset(
        {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
    )

    def __init__(self):
        self._cache: list[tuple[str, QRectF]] | None = None
        self._cache_time: float | None = None

    def is_available(self) -> bool:
        return sys.platform == "win32"

    def list_windows(self) -> list[tuple[str, QRectF]]:
        """(title, absolute logical rect) for every visible, non-minimised,
        non-cloaked top-level window, topmost first.

        `EnumWindows` already visits windows in top-to-bottom Z-order, so
        -- unlike `XwininfoWindowGeometryProvider`, which has to reverse
        `xwininfo`'s bottom-first order -- no re-sorting is needed to make
        "the topmost window wins" (window_at()'s first match) true.
        """
        now = time.monotonic()
        if (
            self._cache is not None
            and self._cache_time is not None
            and now - self._cache_time < self._CACHE_SECONDS
        ):
            return self._cache
        windows = self._list_windows_uncached()
        self._cache = windows
        self._cache_time = now
        return windows

    def _list_windows_uncached(self) -> list[tuple[str, QRectF]]:
        if not self.is_available():
            return []
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi

        windows: list[tuple[str, QRectF]] = []

        def _visit(hwnd, _lparam) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True  # keep enumerating
            if self._is_cloaked(dwmapi, hwnd):
                return True
            if self._is_shell_window(user32, hwnd):
                return True
            rect = self._frame_bounds(dwmapi, user32, hwnd)
            if rect is None:
                return True
            if self._is_larger_than_its_monitor(user32, hwnd, rect):
                return True
            windows.append((self._window_title(user32, hwnd), rect))
            return True

        # ctypes.WINFUNCTYPE is only constructed here, guarded by
        # is_available() above -- it doesn't exist off Windows at all, so
        # referencing it at module/class-body scope would break importing
        # this module on Linux, where the rest of this file's tests run.
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_visit)
        user32.EnumWindows(enum_proc, 0)
        return windows

    def _is_cloaked(self, dwmapi, hwnd) -> bool:
        cloaked = ctypes.c_int(0)
        ok = dwmapi.DwmGetWindowAttribute(
            hwnd, self._DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return ok == 0 and cloaked.value != 0

    def _is_shell_window(self, user32, hwnd) -> bool:
        """True for the shell's own desktop/workspace/taskbar chrome (see
        `_SHELL_CLASS_NAMES`) -- these are real, visible, non-minimised,
        non-cloaked top-level windows, so class name is the only thing
        that actually distinguishes them from an application window.
        """
        buffer = ctypes.create_unicode_buffer(256)
        if not user32.GetClassNameW(hwnd, buffer, len(buffer)):
            return False
        return buffer.value in self._SHELL_CLASS_NAMES

    def _monitor_rect(self, user32, hwnd) -> QRectF | None:
        """The rect (absolute logical coordinates, same space as
        `_frame_bounds`) of the monitor `hwnd` sits on -- `MONITOR_
        DEFAULTTONEAREST` so a window straddling two monitors, or one
        whose reported position is briefly off every monitor mid-move,
        still gets an answer instead of none at all. `None` only when
        Windows itself can't answer (no monitors, or a stale handle),
        which `_is_larger_than_its_monitor` treats as "can't tell, so
        don't drop the window for it".
        """
        hmonitor = user32.MonitorFromWindow(hwnd, self._MONITOR_DEFAULTTONEAREST)
        if not hmonitor:
            return None
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return None
        monitor = info.rcMonitor
        width = monitor.right - monitor.left
        height = monitor.bottom - monitor.top
        if width <= 0 or height <= 0:
            return None
        return QRectF(monitor.left, monitor.top, width, height)

    def _is_larger_than_its_monitor(self, user32, hwnd, rect: QRectF) -> bool:
        """True when `rect` doesn't fit within the monitor `hwnd` sits on.

        A defense in depth alongside `_is_shell_window`, not a duplicate
        of it: Progman/WorkerW would already fail this (they span the
        whole virtual desktop), but this also catches anything else
        sized past its own monitor without being one of the four named
        shell classes. Strictly greater-than, so a window exactly the
        size of the monitor it's on -- a genuine full-screen window -- is
        never dropped by this check.
        """
        monitor_rect = self._monitor_rect(user32, hwnd)
        if monitor_rect is None:
            return False
        return rect.width() > monitor_rect.width() or rect.height() > monitor_rect.height()

    def _frame_bounds(self, dwmapi, user32, hwnd) -> QRectF | None:
        rect = _RECT()
        ok = dwmapi.DwmGetWindowAttribute(
            hwnd,
            self._DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if ok != 0 and not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None
        return QRectF(rect.left, rect.top, width, height)

    def _window_title(self, user32, hwnd) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def window_at(self, point: QPointF) -> QRectF | None:
        for _title, rect in self.list_windows():
            if rect.contains(point):
                return rect
        return None


class UnsupportedPlatformBackend(CaptureBackend):
    """Placeholder registered by a platform whose `Platform.build_capture_registry()`
    (see `snipux/platform/__init__.py`) has no real backend implemented yet
    -- macOS today (SNX-88 gave Windows a real one).

    Always unavailable, so `BackendRegistry.capture()` never reaches
    `capture()` below -- CLAUDE.md's "no backend constructed on a platform
    it cannot run on" is about running one, and this is only ever named,
    never run. What it buys over the registry staying empty is that
    `unavailable_reason()` names the platform: `--list-backends` on Windows
    would otherwise print "no backends registered", which reads exactly
    like a bug rather than the honest, still-unimplemented state it is.
    """

    def __init__(self, platform_name: str):
        self._platform_name = platform_name

    def name(self) -> str:
        return f"{self._platform_name.lower()}-native"

    def is_available(self) -> bool:
        return False

    def unavailable_reason(self) -> str | None:
        return f"{self._platform_name} capture is not implemented yet"

    def capture(self) -> Frame:
        # Never reached in practice: is_available() is always False, so
        # BackendRegistry.capture() skips straight past this backend.
        # Raising plainly here (rather than pretending to succeed) is what
        # keeps that true if something ever calls capture() directly.
        raise NotImplementedError(self.unavailable_reason())
