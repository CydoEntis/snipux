"""Capture-backend interface and the frozen-frame `Frame` type.

Per CLAUDE.md's one architectural rule, the entire virtual desktop is
captured in a single shot and everything downstream (selection, cropping,
annotation) operates on that frozen frame rather than asking the compositor
for pixels again. This module holds the `Frame` type and the
`CaptureBackend`/`BackendRegistry` abstraction that later, platform-specific
tickets register real backends into. No real backend lives here yet.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
    """
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
        virtual_rect = _virtual_desktop_geometry()
        origin = virtual_rect.topLeft()

        # One session-wide ratio, taken from the primary screen, rather
        # than each screen's own devicePixelRatio() — Frame/crop() can only
        # represent a single scale factor for the whole image, so
        # compositing per-screen ratios would get the canvas size right
        # while silently misplacing/missizing whichever monitor doesn't
        # match. GNOME/X11 scaling is session-wide in practice anyway; true
        # per-monitor DPI on X11 would need a Frame-level model change,
        # which is out of scope here.
        ratio = QGuiApplication.primaryScreen().devicePixelRatio()

        image = QImage(
            round(virtual_rect.width() * ratio),
            round(virtual_rect.height() * ratio),
            QImage.Format.Format_RGB32,
        )
        image.fill(0)

        painter = QPainter(image)
        for screen in QGuiApplication.screens():
            pixmap = screen.grabWindow(0)
            geometry = QRectF(screen.geometry())
            target = QRectF(
                (geometry.x() - origin.x()) * ratio,
                (geometry.y() - origin.y()) * ratio,
                geometry.width() * ratio,
                geometry.height() * ratio,
            )
            painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        # Closed before Frame() is constructed below, never left open
        # across a read of the image it painted, per CLAUDE.md.
        painter.end()

        return Frame(image=image, logical_origin=origin, logical_size=virtual_rect.size())


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
