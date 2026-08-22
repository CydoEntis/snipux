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
from abc import ABC, abstractmethod
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRect, QRectF, QSizeF
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


class CaptureError(Exception):
    """Raised by `BackendRegistry.capture()` when every backend fails.

    Carries every `(backend_name, exception)` pair collected along the way,
    not just the last one, so failures can be reported together per
    CLAUDE.md's "a capture backend that fails must not stop the next one"
    rule.
    """

    def __init__(self, failures: list[tuple[str, Exception]]):
        self.failures = failures
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
