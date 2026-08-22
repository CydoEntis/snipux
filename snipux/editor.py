"""The annotation window.

Per CLAUDE.md's one architectural rule, everything here is ordinary drawing
on the frozen `Frame` the overlay already captured and cropped — no code
path in this module asks the compositor for pixels. This ticket adds only
the display + coordinate-mapping widget at the heart of that window;
drawing tools are a later ticket (see SPEC.md item 3).
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

from snipux.capture import Frame


class Canvas(QWidget):
    """Displays a `Frame`'s image centered, letterboxed, and never upscaled.

    Built from a `Frame` (not a bare `QImage`), mirroring `Overlay` in
    overlay.py — the editor hands this whatever `Frame` the overlay
    confirmed (already cropped to the user's selection via `Frame.crop()`),
    and future tickets (crop tool, redraw-after-annotate) will want the
    `Frame`'s logical geometry, not just its pixels.

    Like `Overlay`'s `_to_local`/`_to_absolute`, coordinate-space helpers
    here are small, named, pure functions computed on demand from current
    widget geometry — there is no cached scale or target-rect state to
    invalidate on resize, just one source of truth (`self.rect()` +
    `self.image.size()`) recomputed every call.
    """

    def __init__(self, frame: Frame, parent=None):
        super().__init__(parent)
        self._frame = frame

    @property
    def image(self):
        return self._frame.image

    def _target_rect(self) -> QRectF:
        """Where self.image is drawn in widget-local coordinates: centered,
        scaled to fit self.rect() without exceeding 1.0 (never upscaled),
        preserving aspect ratio. Recomputed from current widget size and
        image size on every call — nothing cached to invalidate on resize.
        """
        image_size = self.image.size()
        widget_size = self.size()
        if image_size.width() <= 0 or image_size.height() <= 0:
            return QRectF()  # degenerate image: nothing to draw, no target

        scale = min(
            widget_size.width() / image_size.width(),
            widget_size.height() / image_size.height(),
            1.0,  # the "never upscale past 100%" rule
        )
        target_w = image_size.width() * scale
        target_h = image_size.height() * scale
        x = (widget_size.width() - target_w) / 2
        y = (widget_size.height() - target_h) / 2
        return QRectF(x, y, target_w, target_h)

    def widget_to_image(self, point: QPointF) -> QPointF | None:
        """Widget-local point -> image-pixel point, or None if point falls
        in the letterbox margin outside the drawn image.
        """
        target = self._target_rect()
        if target.width() <= 0 or target.height() <= 0 or not target.contains(point):
            return None
        scale = target.width() / self.image.width()
        local = point - target.topLeft()
        return QPointF(local.x() / scale, local.y() / scale)

    def image_to_widget(self, point: QPointF) -> QPointF:
        """Image-pixel point -> widget-local point. Total (no None case):
        callers hold image-space points that came from inside the image by
        construction, unlike widget-space points which can land anywhere
        including the margin.

        Guards the same degenerate-image case widget_to_image guards via
        _target_rect()'s 0x0 fallback, so this can't divide by zero the way
        an unguarded version would (self.image.width() == 0 and
        target.width() == 0.0 together, if this weren't checked first).
        There's no meaningful scale to invert for an empty image, so this
        returns target.topLeft() (itself (0, 0) in that case) rather than
        raising — degrading the same way widget_to_image degrades to None,
        just without a None case to return through.
        """
        target = self._target_rect()
        if target.width() <= 0 or target.height() <= 0 or self.image.width() <= 0:
            return target.topLeft()
        scale = target.width() / self.image.width()
        return target.topLeft() + QPointF(point.x() * scale, point.y() * scale)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        painter.drawImage(self._target_rect(), self.image)
        painter.end()  # never left open across a pixmap read, per CLAUDE.md
