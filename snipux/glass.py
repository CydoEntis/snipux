"""The glass every bar and menu over the frozen frame is painted on.

The handoff's bars are glass: a translucent fill over `backdrop-filter:
blur(16px)` of whatever is behind them (docs/design/bars/README.md and
docs/design/flow/README.md, "Qt notes"). Qt has no backdrop filter, and needs
none here. Every surface this paints sits over `OverlayWindow`, which paints
one frozen frame edge to edge, so the pixels behind a surface are already in
memory: `Glass` crops them from under the surface, blurs the crop once, and
paints it beneath the fill.

**Cached per placement.** A paint only works out where the surface is and
compares that with where its crop was taken. The blur runs when the surface
comes to rest somewhere new -- shown, moved, resized, or over a new frame --
and never per paint. While the surface is still moving (dragged, or
following a selection being re-framed) the crop from where it last rested
stands in, and a fresh one is taken once it has stayed put for `SETTLE_MS`.

**Where no blur can be had** -- nothing says what is painted behind the
surface, the surface is off the frame, or the blur comes back empty -- the
fill rises to `tokens.BarColor.FALLBACK_BG_ALPHA` and nothing else changes.
`force_fallback` forces that, for tests.

**Alpha, never opacity.** The crop and the fill are both painted by the
surface itself, under controls that stay fully opaque.

**Coordinates.** Three spaces, and every rect below is in one of them:

- *global logical* -- where Qt has placed a widget on the desktop. It is the
  one space a child widget and a top-level popup share, so it is how a
  surface is found wherever it really is: on any monitor, in its host's
  window or in one of its own.
- *host-local logical* -- the host window's own coordinates, the space it
  paints the frozen frame in.
- *image pixels* -- the frozen frame's own pixels, more of them than logical
  ones under display scaling. Converted per axis, as `capture.Frame.crop`
  converts, and never by a screen's device pixel ratio: how many pixels the
  frame has per logical unit is a fact about the capture, not about the
  screen the widget happens to be on.

A host is any widget with a `glass_backdrop()` method. It is the surface's
nearest ancestor that has one -- a popup's parent counts -- or the widget
handed to `Glass.set_host`, for a popup with no parent to find one through.
`glass_backdrop()` returns the image the host paints behind its chrome and
the host-local logical rect it paints that image across; or None, where the
host paints a ground of its own and its chrome keeps the fill it was designed
with, with no blur and no fallback.

A module of its own because `overlay.py` and `chooser.py` both paint glass,
and `chooser.py` cannot import `overlay.py`.
"""

from __future__ import annotations

import math
import time
import weakref

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSizeF, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPixmap, QTransform
from PyQt6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene, QWidget

from snipux.design import tokens

# How long a surface has to stay put before where it is counts as a
# placement. Longer than the gap between two moves of a drag, short enough
# that the crop has caught up by the time the eye is back on the surface.
SETTLE_MS = 150

# How far a blur reaches, in radii. `QGraphicsBlurEffect`'s own bounding rect
# reaches two and a half radii and a pixel past its source, so a crop blurred
# with this much of the frame around it has edges no different from its
# middle.
_REACH = 3

# The test hook: True paints every surface as if no blur could be had.
force_fallback = False


def _now() -> float:
    """Seconds, for telling a move from motion. A function so a test can
    hold the clock still."""
    return time.monotonic()


def rounded(rect: QRectF, radius: float) -> QPainterPath:
    """A rounded rect as a path, for `Glass.paint`."""
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def blur(image: QImage, radius: float) -> QImage:
    """`image` blurred by `radius` of its own pixels, the same size.

    Through a `QGraphicsBlurEffect` on a `QGraphicsPixmapItem`, rendered once
    into a `QImage`. That is the one public route to the blur Qt's own
    effects use (`qt_blurImage` is private), it runs wherever a
    `QApplication` does, the offscreen platform included, and it is an
    exponential blur whose cost does not grow with the radius. Measured on a
    2560x1440 frame, a crop for the stills bar or a menu takes under a
    millisecond with a pixel to a logical pixel and about three with two,
    where blurring the whole frame takes about twenty-five.

    Not a downscale and upscale, which is several times cheaper again but
    leaves a blocky smear rather than a blur, and whose strength cannot be
    set in pixels to match the handoff's 16px.
    """
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(image))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    result = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    # Source given explicitly: the scene's own rect has grown by the blur's
    # reach, and rendering that would shrink the image to fit.
    scene.render(painter, QRectF(result.rect()), QRectF(image.rect()))
    painter.end()
    return result


def _image_rect(local: QRectF, painted_across: QRectF, scale_x: float, scale_y: float) -> QRect:
    """`local`, a host-local logical rect, as the image pixels painted there
    when the image is stretched across `painted_across` (host-local logical)
    at `scale_x` by `scale_y` image pixels per logical one.

    Both edges rounded and then subtracted, as `capture.Frame.crop` rounds
    them, so under fractional scaling a crop's edge lands on the pixel the
    frame's own edge does rather than one to the side of it.
    """
    left = (local.left() - painted_across.left()) * scale_x
    top = (local.top() - painted_across.top()) * scale_y
    x0, y0 = round(left), round(top)
    x1 = round(left + local.width() * scale_x)
    y1 = round(top + local.height() * scale_y)
    return QRect(x0, y0, x1 - x0, y1 - y0)


def _bands(start: int, length: int, inner_start: int, inner_length: int) -> list[tuple[int, int, int, int]]:
    """One axis of `_clamped_copy`: (from, to, source start, source length)
    for the stretch before the image, across it, and after it."""
    end, inner_end = start + length, inner_start + inner_length
    bands = [
        (start, inner_start, inner_start, 1),
        (inner_start, inner_end, inner_start, inner_length),
        (inner_end, end, inner_end - 1, 1),
    ]
    return [band for band in bands if band[1] > band[0]]


def _clamped_copy(image: QImage, rect: QRect) -> QImage:
    """`rect` of `image`, both in image pixels, with whatever lies past an
    edge of the image repeated from that edge.

    The chooser row hangs from the top of the frame, and a bar or a menu can
    sit against any other edge. Blurred with nothing past the edge, the
    ground would thin towards transparent along it.
    """
    inner = rect.intersected(image.rect())
    if inner == rect:
        return image.copy(rect).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    result = QImage(rect.size(), QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    # No smooth transform: a one-pixel edge stretched without it is that
    # pixel repeated.
    for x0, x1, source_x, source_w in _bands(rect.left(), rect.width(), inner.left(), inner.width()):
        for y0, y1, source_y, source_h in _bands(rect.top(), rect.height(), inner.top(), inner.height()):
            painter.drawImage(
                QRect(x0 - rect.left(), y0 - rect.top(), x1 - x0, y1 - y0),
                image,
                QRect(source_x, source_y, source_w, source_h),
            )
    painter.end()
    return result


def _blurred_crop(image: QImage, rect: QRect, radius: float) -> QImage | None:
    """`rect` of `image` blurred by `radius`, all in image pixels.

    Blurred with the blur's reach of the image around it and then cut back,
    so the crop's own edges are blurred with what really lies past them.

    Made opaque again at the end, un-premultiplying as it goes: the blur's
    fixed-point arithmetic leaves even a solid image a few levels short of
    opaque, and the desktop the crop stands for has no transparency in it.
    """
    margin = math.ceil(radius * _REACH)
    padded = _clamped_copy(image, rect.adjusted(-margin, -margin, margin, margin))
    blurred = blur(padded, radius)
    if blurred.isNull():
        return None
    crop = blurred.copy(QRect(margin, margin, rect.width(), rect.height()))
    return crop.convertToFormat(QImage.Format.Format_RGB32)


class Glass:
    """One surface's glass: the blurred crop of the frozen frame behind it,
    cached, and the paint that puts it under the surface's fill.

    Built by the base every surface shares -- `overlay._Chrome`,
    `chooser._Surface` -- and by the few that are plain widgets, so a surface
    has only to call `paint` from its `paintEvent`.
    """

    def __init__(self, widget: QWidget):
        # Weak, both: the glass lives on the widget, and a popup's host is a
        # window that must be free to close before the popup is collected.
        self._widget = weakref.ref(widget)
        self._host: "weakref.ref[QWidget] | None" = None
        # The crop, the placement it was taken for, and the rect of the image
        # it was taken from (image pixels).
        self._crop: QImage | None = None
        self._crop_placement: tuple | None = None
        self._crop_rect: QRect | None = None
        # The placement last painted at, and when the surface last went from
        # one placement to another -- see `_still_moving`.
        self._placement: tuple | None = None
        self._moved_at: float | None = None
        self._settle: QTimer | None = None

    def set_host(self, host: QWidget | None) -> None:
        """Be glass over `host`'s backdrop, for a popup with no parent to
        find a host through (`flowbars.FlowMenu`)."""
        self._host = None if host is None else weakref.ref(host)

    def host(self) -> QWidget | None:
        """The widget whose painting is behind this surface, or None."""
        if self._host is not None:
            host = self._host()
            if host is not None:
                return host
        widget = self._widget()
        node = None if widget is None else widget.parentWidget()
        while node is not None:
            if callable(getattr(node, "glass_backdrop", None)):
                return node
            node = node.parentWidget()
        return None

    @property
    def crop_rect(self) -> QRect | None:
        """The rect of the frame the cached crop was taken from, in image
        pixels; None before one has been."""
        return None if self._crop_rect is None else QRect(self._crop_rect)

    def backdrop(self) -> QImage | None:
        """The blurred crop the next paint puts under the fill, or None where
        it puts none."""
        host = self.host()
        return self._backdrop(host, None if host is None else host.glass_backdrop())

    def paint(self, painter: QPainter, path: QPainterPath, fill: QColor) -> None:
        """Fill `path`, in the surface's own logical coordinates, with the
        glass: the crop behind the surface, then `fill` over it.

        Leaves the painter's pen off and its brush the fill; a surface
        strokes its border afterwards.
        """
        widget = self._widget()
        host = self.host()
        source = None if host is None else host.glass_backdrop()
        crop = None
        if host is None or source is not None:
            crop = self._backdrop(host, source)
            if crop is None:
                fill = QColor(fill)
                fill.setAlphaF(max(fill.alphaF(), tokens.BarColor.FALLBACK_BG_ALPHA))
        painter.setPen(Qt.PenStyle.NoPen)
        if crop is not None and widget is not None:
            surface = QRectF(widget.rect())
            texture = QBrush(crop)
            # Image pixels -> the surface's logical coordinates: the crop
            # covers exactly the surface's rect, whatever the scale.
            texture.setTransform(
                QTransform.fromScale(surface.width() / crop.width(), surface.height() / crop.height())
            )
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setBrush(texture)
            painter.drawPath(path)
            painter.restore()
        painter.setBrush(fill)
        painter.drawPath(path)

    def _backdrop(self, host: QWidget | None, source) -> QImage | None:
        widget = self._widget()
        if force_fallback or widget is None or host is None or source is None:
            return None
        image, painted_across = source  # image pixels; host-local logical
        if image.isNull() or painted_across.isEmpty():
            return None
        # Global logical: where the surface really is, child or popup, on
        # whichever monitor it has been put.
        global_top_left = widget.mapToGlobal(QPoint(0, 0))
        # Host-local logical: the same place, in the space the host paints
        # the frame in.
        local = QRectF(QPointF(host.mapFromGlobal(global_top_left)), QSizeF(widget.size()))
        scale_x = image.width() / painted_across.width()
        scale_y = image.height() / painted_across.height()
        rect = _image_rect(local, painted_across, scale_x, scale_y)  # image pixels
        if rect.isEmpty() or not rect.intersects(image.rect()):
            return None
        radius = tokens.BarMetric.BACKDROP_BLUR * max(scale_x, scale_y)  # image pixels
        placement = (image.cacheKey(), rect.x(), rect.y(), rect.width(), rect.height(), radius)
        if placement == self._crop_placement:
            return self._crop
        if self._still_moving(placement, widget):
            return self._crop
        crop = _blurred_crop(image, rect, radius)
        if crop is None:
            return None
        self._crop, self._crop_placement, self._crop_rect = crop, placement, rect
        return crop

    def _still_moving(self, placement: tuple, widget: QWidget) -> bool:
        """Whether the surface is on its way somewhere, so the crop from
        where it last rested should stand in for now.

        A move that follows another within `SETTLE_MS` is motion. The first
        move away from a resting place is not, so a surface that is shown,
        refitted or sent to another monitor is cropped straight away, and a
        drag costs two blurs -- its first step and where it ends -- rather
        than one per mouse move. While in motion a timer repaints the
        surface once it has stayed put, and that paint takes the crop.
        """
        now = _now()
        settle = SETTLE_MS / 1000
        if placement != self._placement:
            previous_move = self._moved_at
            if self._placement is not None:
                self._moved_at = now
            self._placement = placement
            moving = previous_move is not None and now - previous_move < settle
        else:
            moving = self._moved_at is not None and now - self._moved_at < settle
        if not moving or self._crop is None:
            return False
        if self._settle is None:
            self._settle = QTimer(widget)
            self._settle.setSingleShot(True)
            self._settle.setTimerType(Qt.TimerType.PreciseTimer)
            self._settle.timeout.connect(widget.update)
        self._settle.start(SETTLE_MS)
        return True
