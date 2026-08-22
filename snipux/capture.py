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
from abc import ABC, abstractmethod
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRect, QRectF, QSizeF
from PyQt6.QtGui import QImage


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
