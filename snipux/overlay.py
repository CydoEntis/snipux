"""Frozen-frame selection windows, one per monitor.

Per CLAUDE.md's one architectural rule, the compositor is only ever asked
for pixels once (in `capture.py`). Everything here is ordinary painting and
mouse/key handling on the `Frame` that grab already produced — no code path
in this module asks for a fresh screen read while the user is dragging.

`Overlay` never re-zeroes a selection to its own monitor: every rect it
stores or emits is in absolute logical virtual-desktop coordinates, the same
space `Frame`/`monitor_geometry` use, which is what makes a selection
spanning two monitors arithmetic rather than a special case.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, QSizeF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QTransform
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from snipux import design
from snipux.capture import Frame
from snipux.shapes import ObscuringShape, Shape, StepMarker, render_selection


class SelectionMode(Enum):
    """How the overlay turns mouse input into a selection.

    Chosen once before the overlay is shown (mirrors how the real app offers
    a mode picker before freezing the screen) — never switched mid-drag.
    """

    RECTANGLE = "rectangle"
    FREEFORM = "freeform"
    WINDOW = "window"
    FULL_SCREEN = "full_screen"


class GeometryProvider(ABC):
    """Source of per-window geometry for window-selection mode.

    X11 can answer this; Wayland compositors generally cannot, per
    CLAUDE.md's platform note. `is_available()` mirrors
    `CaptureBackend.is_available()` in capture.py so the two "can this
    platform do X" checks look the same everywhere they appear. It isn't
    load-bearing for the mode-3 fallback itself — a per-point `window_at()
    is None` already covers "no provider" and "no window here" alike — it's
    exposed so a future mode picker can grey out window mode instead of
    offering a dead option.
    """

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this platform can report window geometry at all."""

    @abstractmethod
    def window_at(self, point: QPointF) -> QRectF | None:
        """Absolute logical rect of the window under `point`, or None."""


class UnsupportedGeometryProvider(GeometryProvider):
    """Default provider: reports no windows anywhere.

    This is what makes window mode degrade to plain rectangle dragging
    everywhere until a platform-specific provider (the X11 backend ticket)
    is wired in.
    """

    def is_available(self) -> bool:
        return False

    def window_at(self, point: QPointF) -> QRectF | None:
        return None


class Overlay(QWidget):
    """One frameless, always-on-top window covering a single monitor.

    Paints that monitor's slice of the frozen `Frame`, a dimmed veil outside
    the current selection, a live size readout, and a cursor-centered
    magnifier. Selection state and the `confirmed`/`cancelled` signals carry
    absolute logical virtual-desktop rects, never monitor-local ones.
    """

    # (bounds, exact_path_or_None). `object` carries the second slot because
    # it must hold either a QPainterPath (freeform) or None (every other
    # mode); PyQt passes arbitrary Python objects through `object`. A plain
    # rectangle/window/full-screen selection is never modelled as a
    # degenerate one-rectangle path — `None` says "this is just the bounds"
    # plainly, instead of making every consumer special-case it.
    confirmed = pyqtSignal(QRectF, object)
    cancelled = pyqtSignal()

    VEIL_COLOR = QColor(0, 0, 0, 120)
    CROSSHAIR_COLOR = QColor(255, 0, 0)

    # Source square is defined in *logical* px so it covers the same
    # real-world area regardless of the monitor's scale factor; only
    # converted to image pixels at crop time (see `_paint_magnifier`).
    MAGNIFIER_SOURCE_LOGICAL_SIZE = 20.0
    # Display box is a fixed logical size, independent of the source scale
    # factor — this is what keeps the crosshair centered on the same
    # logical pixel regardless of the monitor's scaling.
    MAGNIFIER_BOX_SIZE = 120
    # Offset from the cursor so the magnifier box never sits directly under
    # the pixel it is magnifying.
    MAGNIFIER_OFFSET = QPointF(20.0, 20.0)

    def __init__(
        self,
        frame: Frame,
        monitor_geometry: QRectF,
        parent=None,
        mode: SelectionMode = SelectionMode.RECTANGLE,
        geometry_provider: GeometryProvider | None = None,
        virtual_desktop_rect: QRectF | None = None,
    ):
        super().__init__(parent)
        self._monitor_geometry = QRectF(monitor_geometry)
        # Reuses Frame.crop()'s already-tested scaling/negative-origin
        # logic instead of re-deriving it here.
        self._monitor_frame = frame.crop(monitor_geometry)
        self._mode = mode
        self._geometry_provider = geometry_provider or UnsupportedGeometryProvider()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setGeometry(
            round(monitor_geometry.x()),
            round(monitor_geometry.y()),
            round(monitor_geometry.width()),
            round(monitor_geometry.height()),
        )
        # Needed so mouseMoveEvent fires (for the magnifier) even when no
        # button is held, not just while dragging a selection.
        self.setMouseTracking(True)

        self._selection: QRectF | None = None
        # Local-logical cursor position, or None before the first move.
        self._cursor_pos: QPointF | None = None
        # Absolute-logical anchor of an in-progress left-button drag.
        self._drag_anchor: QPointF | None = None
        # In-progress freeform path, absolute logical coords. None outside
        # a freeform drag.
        self._drag_path: QPainterPath | None = None
        # Confirmed freeform path, the source of truth for "what pixels are
        # actually inside" once a freeform drag has been confirmed. None in
        # every other mode, and None during an in-progress freeform drag too
        # (that's `_drag_path`), so a stale confirmed path never lingers
        # across a new drag.
        self._selection_path: QPainterPath | None = None
        # Window rect a left-press landed on, remembered from press to
        # release in window mode — a window click is a click, not a drag,
        # for its entire duration, so this is captured once and only read.
        self._window_hit_rect: QRectF | None = None

        self._size_label = QLabel(self)
        self._size_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 160); color: white;"
            " padding: 2px 4px;"
        )
        self._size_label.hide()

        if self._mode is SelectionMode.FULL_SCREEN:
            # Full screen needs no drag: the whole virtual desktop (or, for
            # an overlay built directly without going through
            # create_overlays, this monitor alone) is selected from the
            # first paint. Goes through set_selection (not a raw attribute
            # write) so the label/veil are consistent immediately instead
            # of only catching up on the next unrelated repaint.
            self.set_selection(
                virtual_desktop_rect
                if virtual_desktop_rect is not None
                else self._monitor_geometry
            )

    # -- coordinate-space helpers -----------------------------------------
    # Every rect/point this widget touches is explicitly one of: absolute
    # logical (selection, monitor_geometry), local logical (post
    # _to_local/_to_absolute, used for painting/hit-testing), or — only for
    # the magnifier's source crop — local image-pixel. Naming follows suit.

    def _to_local(self, rect: QRectF) -> QRectF:
        """Absolute logical rect -> this widget's local logical rect."""
        origin = self._monitor_geometry.topLeft()
        return QRectF(rect.topLeft() - origin, rect.size())

    def _to_absolute(self, local_point: QPointF) -> QPointF:
        """Local logical point -> absolute logical point."""
        return local_point + self._monitor_geometry.topLeft()

    # -- public API ---------------------------------------------------------

    def set_selection(self, rect: QRectF | None) -> None:
        """Set the current selection (absolute logical coords) and repaint."""
        self._selection = rect
        self._update_size_label()
        self.update()

    # -- painting -------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawImage(QRectF(self.rect()), self._monitor_frame.image)
        self._paint_veil(painter)
        self._paint_magnifier(painter)
        painter.end()

    def _paint_veil(self, painter: QPainter) -> None:
        widget_rect = QRectF(self.rect())
        # A single even-odd fill dims everywhere except the selection hole
        # in one call, so there's no separate "dim then punch a hole" step
        # that could disagree with this one at the edge.
        path = QPainterPath()
        path.addRect(widget_rect)
        if self._selection is not None:
            local_selection = self._to_local(self._selection).intersected(widget_rect)
            if not local_selection.isEmpty():
                path.addRect(local_selection)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        painter.fillPath(path, self.VEIL_COLOR)

    def _paint_magnifier(self, painter: QPainter) -> None:
        if self._cursor_pos is None:
            return
        image = self._monitor_frame.image
        logical_size = self._monitor_frame.logical_size
        if logical_size.width() <= 0 or logical_size.height() <= 0:
            return

        # Same per-axis scale Frame.crop() derives, applied here because the
        # magnifier reads .image pixels directly rather than letting
        # drawImage stretch implicitly the way the base layer does.
        scale_x = image.width() / logical_size.width()
        scale_y = image.height() / logical_size.height()

        image_cursor_x = self._cursor_pos.x() * scale_x
        image_cursor_y = self._cursor_pos.y() * scale_y

        half_width = (self.MAGNIFIER_SOURCE_LOGICAL_SIZE / 2) * scale_x
        half_height = (self.MAGNIFIER_SOURCE_LOGICAL_SIZE / 2) * scale_y

        width = min(round(half_width * 2), image.width())
        height = min(round(half_height * 2), image.height())
        if width <= 0 or height <= 0:
            return

        left = round(image_cursor_x - half_width)
        top = round(image_cursor_y - half_height)
        left = max(0, min(left, image.width() - width))
        top = max(0, min(top, image.height() - height))

        source_rect = QRect(left, top, width, height)
        cropped = image.copy(source_rect)
        zoomed = cropped.scaled(
            self.MAGNIFIER_BOX_SIZE,
            self.MAGNIFIER_BOX_SIZE,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            # Smoothing would hide the exact pixel this tool exists to show.
            Qt.TransformationMode.FastTransformation,
        )

        # Clamped into the widget the same way _update_size_label clamps its
        # label, so a cursor near a monitor's right/bottom edge still shows
        # a (repositioned) magnifier instead of one painted off-window and
        # clipped away entirely. The crosshair still marks the exact cursor
        # pixel: the source crop above is centered on the cursor regardless
        # of where the box itself ends up on screen.
        box_x = self._cursor_pos.x() + self.MAGNIFIER_OFFSET.x()
        box_y = self._cursor_pos.y() + self.MAGNIFIER_OFFSET.y()
        box_x = max(0.0, min(box_x, self.width() - self.MAGNIFIER_BOX_SIZE))
        box_y = max(0.0, min(box_y, self.height() - self.MAGNIFIER_BOX_SIZE))
        box_rect = QRectF(
            QPointF(box_x, box_y),
            QSizeF(self.MAGNIFIER_BOX_SIZE, self.MAGNIFIER_BOX_SIZE),
        )
        painter.drawImage(box_rect, zoomed)

        center = box_rect.center()
        painter.setPen(self.CROSSHAIR_COLOR)
        painter.drawLine(QPointF(box_rect.left(), center.y()), QPointF(box_rect.right(), center.y()))
        painter.drawLine(QPointF(center.x(), box_rect.top()), QPointF(center.x(), box_rect.bottom()))

    def _update_size_label(self) -> None:
        if self._selection is None:
            self._size_label.hide()
            return

        widget_rect = QRectF(self.rect())
        local_selection = self._to_local(self._selection)
        if local_selection.intersected(widget_rect).isEmpty():
            self._size_label.hide()
            return

        # Width/height come from the absolute selection, in logical pixels
        # per the acceptance criterion — never the (possibly larger)
        # image-pixel size.
        width = round(self._selection.width())
        height = round(self._selection.height())
        self._size_label.setText(f"{width} × {height}")
        self._size_label.adjustSize()

        label_x = round(local_selection.left())
        label_y = round(local_selection.top()) - self._size_label.height() - 4
        label_x = max(0, min(label_x, self.width() - self._size_label.width()))
        label_y = max(0, min(label_y, self.height() - self._size_label.height()))
        self._size_label.move(QPoint(label_x, label_y))
        self._size_label.show()

    # -- interaction ------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            # Immediate cancel, matching Escape's semantics; no drag starts,
            # in every mode.
            self.cancelled.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        anchor = self._to_absolute(event.position())
        self._drag_anchor = anchor

        if self._mode is SelectionMode.FREEFORM:
            self._drag_path = QPainterPath()
            self._drag_path.moveTo(anchor)
            # Something to show from the very first pixel, same as
            # rectangle mode's live-drag feedback.
            self.set_selection(QRectF(anchor, QSizeF(0, 0)))
        elif self._mode is SelectionMode.WINDOW:
            self._window_hit_rect = self._geometry_provider.window_at(anchor)
            if self._window_hit_rect is not None:
                # Clicking a window highlights that window's full rect
                # before the button is even released.
                self.set_selection(self._window_hit_rect)
        # Rectangle / full screen: `_drag_anchor` alone is enough for their
        # release-time logic, nothing else to record here.

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._cursor_pos = event.position()

        if self._mode is SelectionMode.FULL_SCREEN:
            # Selection state is never touched here so the veil hole and
            # size label can't transiently shrink to a drag rect mid-move;
            # the whole desktop was already selected at construction time.
            self.update()
            return

        if self._mode is SelectionMode.FREEFORM:
            if self._drag_path is not None:
                self._drag_path.lineTo(self._to_absolute(event.position()))
                # Through set_selection, not a raw attribute write, so the
                # size label and veil hole live-update stroke by stroke.
                self.set_selection(self._drag_path.boundingRect())
            else:
                self.update()
            return

        if self._mode is SelectionMode.WINDOW:
            absolute_pos = self._to_absolute(event.position())
            if self._drag_anchor is not None and self._window_hit_rect is not None:
                # Press hit a window: a window click doesn't track the
                # mouse, the hit rect is already showing from press time.
                self.update()
            elif self._drag_anchor is not None:
                # Press missed every window: fall back to plain rectangle
                # tracking, per the acceptance criterion.
                self.set_selection(
                    QRectF(self._drag_anchor, absolute_pos).normalized()
                )
            else:
                # Plain hover: a miss actively clears any previously-shown
                # preview instead of leaving it stuck.
                self.set_selection(self._geometry_provider.window_at(absolute_pos))
            return

        # Rectangle.
        if self._drag_anchor is not None:
            absolute_pos = self._to_absolute(event.position())
            self.set_selection(QRectF(self._drag_anchor, absolute_pos).normalized())
        else:
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._drag_anchor is None:
            return

        anchor = self._drag_anchor
        self._drag_anchor = None
        absolute_pos = self._to_absolute(event.position())

        if self._mode is SelectionMode.FULL_SCREEN:
            # No distance/misfire check at all: any release confirms the
            # whole desktop, which was already selected at construction.
            self.confirmed.emit(self._selection, None)
            return

        if self._mode is SelectionMode.FREEFORM:
            path = self._drag_path
            self._drag_path = None
            # The release itself is a traced point, same as every
            # intermediate move — omitting it would silently drop the final
            # drag segment and let closeSubpath() cut straight from the
            # last *moved-to* point back to the anchor instead.
            path.lineTo(absolute_pos)
            path.closeSubpath()
            bounds = path.boundingRect()
            # Measured by the traced path's own bounding-rect diagonal, not
            # anchor-to-release distance: a closed-loop lasso back near its
            # start point has a large bounding-rect diagonal even though its
            # last pixel lands next to its first, so it isn't misfired away.
            diagonal = math.hypot(bounds.width(), bounds.height())
            if diagonal < QApplication.startDragDistance():
                self.set_selection(None)
                return
            self._selection_path = path
            self.set_selection(bounds)
            self.confirmed.emit(bounds, path)
            return

        if self._mode is SelectionMode.WINDOW and self._window_hit_rect is not None:
            rect = self._window_hit_rect
            self._window_hit_rect = None
            # Unconditional, no distance check, regardless of where the
            # release happened: a window click is a click, not a drag, for
            # its entire duration — the hit was captured at press time and
            # is only read here, never re-queried.
            self.set_selection(rect)
            self.confirmed.emit(rect, None)
            return

        if self._mode is SelectionMode.WINDOW:
            # No provider, or the press missed every window: fall through
            # to the same distance-threshold rectangle logic below, which
            # *is* the fallback the acceptance criterion asks for.
            self._window_hit_rect = None

        delta = absolute_pos - anchor
        distance = math.hypot(delta.x(), delta.y())

        if distance < QApplication.startDragDistance():
            # A press/release with no meaningful drag is a misfire per
            # SPEC.md, not a selection of nothing.
            self.set_selection(None)
            return

        rect = QRectF(anchor, absolute_pos).normalized()
        self.set_selection(rect)
        self.confirmed.emit(rect, None)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if self._selection is not None:
                # `_selection_path` is None except right after a freeform
                # drag has been confirmed by mouse release, so this carries
                # the exact shape for a completed freeform selection and
                # None for every other mode, same as a mouse-release confirm.
                self.confirmed.emit(self._selection, self._selection_path)
        else:
            super().keyPressEvent(event)


class Handle(Enum):
    """The eight drag handles around an `OverlayWindow` selection.

    Corners double as their own visible chrome -- the corner brackets *are*
    the handle, per docs/design/overlay-redesign.md's "Selection frame"
    section -- while edges get a small rounded bar. Cursor shapes and paint
    geometry are both keyed off these same names so the two can never drift
    apart from each other.
    """

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"


_CORNER_HANDLES = (
    Handle.TOP_LEFT,
    Handle.TOP_RIGHT,
    Handle.BOTTOM_LEFT,
    Handle.BOTTOM_RIGHT,
)
_EDGE_HANDLES = (Handle.TOP, Handle.BOTTOM, Handle.LEFT, Handle.RIGHT)

# Which edge(s) a handle drags. A corner handle frees the two perpendicular
# edges that meet at it; an edge handle frees just its own. Every other edge
# is the anchor for that handle's drag and is never written during it -- this
# is what "the opposite edge/corner stays anchored" (the re-framing
# acceptance criteria) reduces to in _resize_selection below.
_LEFT_HANDLES = (Handle.TOP_LEFT, Handle.BOTTOM_LEFT, Handle.LEFT)
_RIGHT_HANDLES = (Handle.TOP_RIGHT, Handle.BOTTOM_RIGHT, Handle.RIGHT)
_TOP_HANDLES = (Handle.TOP_LEFT, Handle.TOP_RIGHT, Handle.TOP)
_BOTTOM_HANDLES = (Handle.BOTTOM_LEFT, Handle.BOTTOM_RIGHT, Handle.BOTTOM)

# nwse-resize / nesw-resize on corners, ns-resize / ew-resize on edges, per
# the README's "Selection frame" section.
_HANDLE_CURSORS = {
    Handle.TOP_LEFT: Qt.CursorShape.SizeFDiagCursor,
    Handle.BOTTOM_RIGHT: Qt.CursorShape.SizeFDiagCursor,
    Handle.TOP_RIGHT: Qt.CursorShape.SizeBDiagCursor,
    Handle.BOTTOM_LEFT: Qt.CursorShape.SizeBDiagCursor,
    Handle.TOP: Qt.CursorShape.SizeVerCursor,
    Handle.BOTTOM: Qt.CursorShape.SizeVerCursor,
    Handle.LEFT: Qt.CursorShape.SizeHorCursor,
    Handle.RIGHT: Qt.CursorShape.SizeHorCursor,
}


def _l_bracket_local_path(length: float, thickness: float, radius: float) -> QPainterPath:
    """A top-left-oriented L bracket: top and left arms meeting in a box
    `(0, 0, length, length)`, with only the outer corner -- (0, 0), the tip
    that points away from the selection -- rounded.

    Canonical shape that `OverlayWindow._bracket_path` mirrors via
    `QTransform` for the other three corners, rather than re-deriving the
    same outline four times: the design is symmetric across both axes (see
    the reference's four corner `<div>`s, which differ only in which two
    CSS edges/border-radius corner they set).
    """
    path = QPainterPath()
    path.moveTo(radius, 0)
    path.lineTo(length, 0)
    path.lineTo(length, thickness)
    path.lineTo(thickness, thickness)
    path.lineTo(thickness, length)
    path.lineTo(0, length)
    path.lineTo(0, radius)
    # Quarter circle from the left-mid point (0, radius) to the top-mid
    # point (radius, 0): QPainterPath.arcTo's angles run counterclockwise
    # from 0 deg at 3 o'clock, so 180 deg is this rect's left-mid point and
    # a -90 deg sweep is the short way round to the top-mid point, tracing
    # exactly the rounded tip -- not the long way round through the bottom.
    path.arcTo(QRectF(0, 0, 2 * radius, 2 * radius), 180, -90)
    path.closeSubpath()
    return path


class OverlayWindow(QWidget):
    """The overlay redesign's shell: one frameless window spanning the whole
    virtual desktop, per docs/design/overlay-redesign.md.

    SNX-31 built the background frame and dim scrim; SNX-32 added the
    selection frame -- the marching-ants stroke, corner brackets and edge
    handles; SNX-33 made that selection re-frameable. SNX-34 (this ticket)
    adds the ink layer itself: `_marks` are stored and painted in this
    widget's own window coordinates, clipped to the selection, per
    docs/design/overlay-redesign.md's "Ink lives in screen coordinates" --
    so re-framing moves the clip over marks that never move themselves.
    SNX-38 adds the eraser: `erase_at`/`undo_erase`, hit-testing marks via
    `Shape.hit_test` only while `set_eraser_active` has armed it, per the
    spec's "Marks become hit-testable only while the eraser is active."
    SNX-39 adds the general undo/redo/clear stack over `_marks` itself
    (`undo`/`redo`/`clear`, distinct from the eraser's own single-slot
    `undo_erase`) plus `copy`/`save`, which render `_marks` fresh at the
    moment they're called -- replacing the old editor.py flow's bug of
    copying the un-annotated capture once, before any annotation could
    exist. The floating bar and the drawing-tool mouse handling that would
    actually call `add_mark` from a live drag are still later tickets in
    the same arc; this class exists so they have a shell to attach to.

    Unlike `Overlay` above -- one instance per monitor, selection kept in
    absolute logical virtual-desktop coordinates so per-monitor crops tile
    correctly -- this is a *single* window covering the whole desktop, and
    per the spec's state table (`sel: QRect # window coords`) its selection
    is kept in window coordinates: local to this widget's own top-left, not
    the virtual desktop's. `frame` is expected to be a single capture
    already spanning every monitor -- what `BackendRegistry.capture()`
    returns -- not a per-monitor crop.

    Per CLAUDE.md's one architectural rule, the compositor is asked for
    pixels exactly once, upstream in `capture.py`; the frame handed in here
    is already frozen, and the spec's own deviation note applies: this never
    uses `QScreen.grabWindow(0)`, which returns black on Wayland.
    """

    # Marching ants: a QTimer at ~30fps advancing the dashed pen's offset,
    # per the README's Qt note -- not tied to paintEvent's own cadence, so
    # the animation speed doesn't drift with however often something else
    # triggers a repaint.
    _ANTS_TIMER_INTERVAL_MS = 33

    # Straddle offsets and corner radii the design specifies by pixel value
    # in the README's prose (and the HTML reference) rather than as a named
    # `tokens.Metric` constant -- unlike arm length/thickness and handle
    # dimensions, which *are* tokenized and read from there below.
    _CORNER_BRACKET_OFFSET = 2
    _CORNER_BRACKET_RADIUS = 3
    _EDGE_HANDLE_OFFSET = 5
    _EDGE_HANDLE_RADIUS = 5
    _CORNER_HIT_OFFSET = 7

    # Re-framing clamps (SNX-33), likewise given by the README's "Re-framing"
    # prose as plain pixel values rather than a tokens.Metric entry -- the
    # minimum size itself *is* tokenized (SEL_MIN_W/H below), since that one
    # the ticket explicitly overrides from the spec's default.
    _TOP_CLEARANCE = 52  # keeps the selection clear of the top hint HUD
    _BAR_ROOM = 130  # keeps room below the selection for the floating bar

    def __init__(self, frame: Frame, parent=None):
        super().__init__(parent)
        self._frame = frame

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        # The frame's own logical origin/size *is* the virtual desktop's
        # bounds -- a full capture already spans every monitor, so no union
        # of screen geometries needs computing here the way create_overlays()
        # does for the per-monitor Overlay above.
        self.setGeometry(
            round(frame.logical_origin.x()),
            round(frame.logical_origin.y()),
            round(frame.logical_size.width()),
            round(frame.logical_size.height()),
        )
        # Needed for the handle cursors below to update on a plain hover,
        # not just while a button is held -- mirrors `Overlay.setMouseTracking`
        # above for the same reason.
        self.setMouseTracking(True)

        # Window coordinates, per the class docstring -- None until
        # set_selection is called.
        self._selection: QRect | None = None

        # Handle currently being dragged (SNX-33 re-framing), and the
        # selection as it stood the moment that drag started. The anchor is
        # read-only for the drag's whole duration -- every edge it doesn't
        # own comes from here, never from the live selection -- which is
        # what keeps the opposite edge/corner from creeping as the mouse
        # moves. None outside a handle drag.
        self._active_handle: Handle | None = None
        self._resize_anchor: QRect | None = None

        # The ink layer (SNX-34): overlay-window coordinates, the same
        # space `_selection` lives in above -- never translated relative to
        # the selection, per the class docstring. Paint order is the list
        # order, mirroring shapes.py's render().
        self._marks: list[Shape] = []
        # Redo stack (SNX-39): whole marks popped off the *end* of `_marks`
        # by undo(), in the order undo() popped them -- so redo() popping
        # this list's own end and appending back to `_marks` restores each
        # one to exactly the draw-order position it was undone from. Never
        # touched by erase_at/undo_erase, which is its own single-slot undo
        # scoped to the eraser tool alone -- see that pair's docstrings.
        self._redo: list[Shape] = []

        # Eraser tool state (SNX-38). False until a caller (the floating
        # bar's eraser button, a later ticket) arms it via
        # set_eraser_active -- marks are only ever hit-tested while this is
        # True, per docs/design/overlay-redesign.md's "Drawing": "hit-
        # testable only while the eraser is active," so ordinary drawing
        # never pays for it.
        self._eraser_active = False
        # (index, shape) most recently removed by erase_at, restorable via
        # undo_erase() -- see its own docstring. None once nothing has been
        # erased yet, or once undo_erase() has already consumed it.
        self._erased_mark: tuple[int, Shape] | None = None

        self._dash_offset = 0.0
        self._ants_timer = QTimer(self)
        self._ants_timer.setInterval(self._ANTS_TIMER_INTERVAL_MS)
        self._ants_timer.timeout.connect(self._advance_ants)

    def set_selection(self, rect: QRect | None) -> None:
        """Set the current selection (window coordinates) and repaint."""
        self._selection = rect
        self.update()

    def add_mark(self, shape: Shape) -> None:
        """Append `shape` to the ink layer and repaint.

        `shape`'s points must already be in this widget's own window
        coordinates -- the same space mouse events and `_selection` use --
        never translated to be relative to the selection. That is what lets
        a re-frame leave every mark exactly where it was drawn: only the
        selection's clip rect moves, per the class docstring.

        Clears the redo stack (SNX-39): per docs/design/overlay-redesign.md's
        "Undo / redo", "any new mark clears the redo stack" -- a mark
        committed after an undo makes whatever was undone unreachable by
        redo again, same as any ordinary undo/redo history.
        """
        self._marks.append(shape)
        self._redo.clear()
        self.update()

    @property
    def marks(self) -> tuple[Shape, ...]:
        """Ink layer contents, in paint order. A copy, not the live list,
        mirroring `Canvas.shapes` in editor.py."""
        return tuple(self._marks)

    # -- undo / redo / clear (SNX-39) --------------------------------------
    # Two stacks of whole marks, per docs/design/overlay-redesign.md's
    # "Undo / redo": undo pops the newest mark off `_marks` onto `_redo`;
    # redo pops it back. Both are plain end-of-list push/pop, which is what
    # keeps a redone mark landing at exactly the draw-order position it was
    # undone from -- there is no index bookkeeping to get wrong.

    @property
    def can_undo(self) -> bool:
        return bool(self._marks)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> None:
        """Move the newest mark from the ink layer to the redo stack.

        A no-op with nothing to undo -- mirrors `Canvas.undo`'s guard in
        editor.py, just against `_marks` being empty rather than a history
        index, since this class keeps no separate history list.
        """
        if not self._marks:
            return
        self._redo.append(self._marks.pop())
        self.update()

    def redo(self) -> None:
        """Move the newest undone mark from the redo stack back onto the
        ink layer, at the same position in draw order it was undone from.

        A no-op with nothing to redo -- either nothing has been undone yet,
        or a mark committed since (see `add_mark`) already cleared the
        stack.
        """
        if not self._redo:
            return
        self._marks.append(self._redo.pop())
        self.update()

    def clear(self) -> None:
        """Discard every mark and both stacks in a single step.

        Per the spec: "Clear-ink empties both and toasts" -- and clearing
        is explicitly *not* itself undoable, unlike an ordinary undo/redo
        entry: the cleared marks are dropped outright rather than pushed
        onto `_redo`, so a subsequent undo() has nothing left to pop and
        cannot bring them back.
        """
        self._marks = []
        self._redo = []
        self.update()

    def set_eraser_active(self, active: bool) -> None:
        """Arm/disarm the eraser tool (SNX-38).

        While active, a plain left-click inside the selection that doesn't
        land on a resize handle removes the topmost mark under the cursor
        instead of being a no-op -- see mousePressEvent -- and the cursor
        over the selection switches from the drawing crosshair to a
        pointer, per docs/design/overlay-redesign.md's "Selection frame"
        cursor table ("crosshair for every tool except the eraser, which
        is pointer") -- see mouseMoveEvent.
        """
        self._eraser_active = active

    def erase_at(self, point: QPointF) -> Shape | None:
        """Remove and return the topmost mark under `point` (this widget's
        own window coordinates -- the same space `_marks` lives in), or
        None if nothing is there.

        Walks `_marks` back to front, mirroring `_paint_marks`'s (and
        render()'s) own draw-order-is-paint-order contract, so an overlap
        resolves to whichever mark is actually visible at that pixel --
        per docs/design/overlay-redesign.md's "Drawing": "a click deletes
        the topmost mark under the cursor." A miss removes nothing and
        raises nothing: not every click lands on ink, and that is not an
        error.

        The removed (index, shape) pair is stashed for undo_erase() to
        restore at the same list position it was removed from -- see that
        method's own docstring. A later erase_at call overwrites it, same
        as this tool has only ever removed one mark at a time.
        """
        for index in range(len(self._marks) - 1, -1, -1):
            if self._marks[index].hit_test(point):
                shape = self._marks.pop(index)
                self._erased_mark = (index, shape)
                self.update()
                return shape
        return None

    def undo_erase(self) -> None:
        """Restore the mark most recently removed by erase_at to its
        original position in draw order.

        A no-op if nothing has been erased since the last erase_at/
        undo_erase call -- this is a single slot of undo scoped to the
        eraser tool itself, not the general multi-action undo/redo stack
        docs/design/overlay-redesign.md's "Undo / redo" section describes,
        which is a later ticket's concern.
        """
        if self._erased_mark is None:
            return
        index, shape = self._erased_mark
        self._marks.insert(index, shape)
        self._erased_mark = None
        self.update()

    def rendered_image(self) -> QImage:
        """The final exported image: `_marks` flattened onto the current
        selection's crop of the frozen frame, translated from window
        coordinates to the cropped image's own origin exactly once -- see
        `shapes.render_selection` and docs/design/overlay-redesign.md's
        "Ink lives in screen coordinates".
        """
        if self._selection is None:
            raise ValueError("no selection to export")
        return render_selection(self._frame, self._marks, QRectF(self._selection))

    # -- copy / save (SNX-39) ----------------------------------------------
    # Both render fresh from `rendered_image()` at the moment they're
    # called, which is the actual fix this ticket makes: the old editor.py
    # flow (Editor.__init__) copied the raw, un-annotated capture to the
    # clipboard exactly once, before any annotation could exist, so the
    # clipboard never reflected marks made afterwards. Neither method
    # imports from app.py at module level -- app.py imports this module at
    # its own top level (to build overlays), so a top-level import back
    # would be circular; deferred here the same way
    # `AppController._on_confirmed` defers importing `Editor`.

    def copy(self) -> None:
        """Flatten the marks present *right now* onto the selection's crop
        and place the result on the clipboard.
        """
        from snipux.app import copy_image_to_clipboard

        copy_image_to_clipboard(self.rendered_image())

    # Subdirectory of ~/Pictures saves land in -- per the spec's "Save
    # writes a timestamped PNG to ~/Pictures/snipux, creating the
    # directory." `app.save_image`'s own default (bare ~/Pictures, used by
    # editor.py's still-existing Editor) doesn't know about this
    # subdirectory, so it's supplied here rather than changed there.
    SAVE_SUBDIRECTORY = "snipux"

    def save(self) -> Path:
        """Flatten the marks present *right now* onto the selection's crop
        and write it as a timestamped PNG under ~/Pictures/snipux, creating
        that directory if it doesn't exist yet. Returns the path written.
        """
        from snipux.app import save_image

        directory = Path.home() / "Pictures" / self.SAVE_SUBDIRECTORY
        return save_image(self.rendered_image(), directory)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The ants only cost frames while actually on screen -- the
        # acceptance criterion is explicit that the timer must not keep
        # ticking (and scheduling repaints) once the overlay is hidden.
        self._ants_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._ants_timer.stop()

    def _advance_ants(self) -> None:
        """Advance the dashed stroke's offset by one animation frame.

        `ANTS_DASH` sums to the 14px one full dash-cycle the README's
        `stroke-dashoffset -> -14 over 700ms, linear` describes; each tick
        moves the offset by whatever fraction of that cycle one timer
        interval covers, so the total time for a full lap stays
        `ANTS_PERIOD_MS` regardless of the timer's own tick rate.
        """
        metric = design.tokens.Metric
        cycle = sum(metric.ANTS_DASH)
        step = cycle * self._ANTS_TIMER_INTERVAL_MS / metric.ANTS_PERIOD_MS
        self._dash_offset = (self._dash_offset + step) % cycle
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        handle = self._handle_at(event.position())
        if handle is None:
            if (
                self._eraser_active
                and self._selection is not None
                and QRectF(self._selection).contains(event.position())
            ):
                # No drag, per the spec's "Drawing": "eraser -- no drag." A
                # miss (nothing under the cursor) is already a safe no-op
                # inside erase_at itself.
                self.erase_at(event.position())
            # Every other tool's stroke-start is still a later ticket.
            # Falling through to this no-op -- rather than the handle
            # branch below -- is exactly what "stop event propagation at
            # the handle" needs from this method: a future stroke-start
            # only ever gets reached when a handle wasn't hit.
            return
        # Per the spec: a handle press is a resize, never a stroke, and
        # returning here means nothing past this point runs for it.
        self._active_handle = handle
        self._resize_anchor = QRect(self._selection)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._active_handle is not None:
            self._resize_selection(event.position())
            handle = self._active_handle
        else:
            handle = self._handle_at(event.position())
        if handle is not None:
            self.setCursor(_HANDLE_CURSORS[handle])
        elif self._selection is not None and QRectF(self._selection).contains(
            event.position()
        ):
            # Per docs/design/overlay-redesign.md's "Selection frame":
            # inside the selection, every tool shows a crosshair except the
            # eraser, which shows a pointer (SNX-38) -- so the cursor
            # itself reads as "click to remove" rather than "drag to draw."
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if self._eraser_active
                else Qt.CursorShape.CrossCursor
            )
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._active_handle = None
        self._resize_anchor = None

    def _resize_selection(self, pos: QPointF) -> None:
        """Apply one drag-move of `self._active_handle` to the selection.

        `self._resize_anchor` is the selection as it stood when the drag
        started; edges this handle doesn't free are read from it and never
        written below, which is what keeps the opposite edge/corner
        anchored for the whole drag. Clamps are applied in the order the
        README's "Re-framing" section gives -- minimum size, `x >= 0`,
        `y >= 52`, stays inside the window, room for the floating bar --
        with the one deliberate deviation the ticket calls for: the
        minimum is `tokens.Metric.SEL_MIN_W/H` (16x16, not the spec's
        200x140), and the floating-bar clamp gives way to that minimum
        instead of the other way round.
        """
        handle = self._active_handle
        anchor = QRectF(self._resize_anchor)
        metric = design.tokens.Metric

        left, top = anchor.left(), anchor.top()
        right, bottom = anchor.right(), anchor.bottom()
        free_left = handle in _LEFT_HANDLES
        free_right = handle in _RIGHT_HANDLES
        free_top = handle in _TOP_HANDLES
        free_bottom = handle in _BOTTOM_HANDLES

        # 1. Minimum size: the dragged edge stops MIN away from the anchor
        # edge it's measured against, which never itself moves.
        if free_left:
            left = min(pos.x(), right - metric.SEL_MIN_W)
        if free_right:
            right = max(pos.x(), left + metric.SEL_MIN_W)
        if free_top:
            top = min(pos.y(), bottom - metric.SEL_MIN_H)
        if free_bottom:
            bottom = max(pos.y(), top + metric.SEL_MIN_H)

        # 2. x >= 0
        if free_left:
            left = max(left, 0.0)
        # 3. y >= 52, clear of the top hint HUD.
        if free_top:
            top = max(top, self._TOP_CLEARANCE)
        # 4. Stays inside the window.
        if free_right:
            right = min(right, self.width())
        if free_bottom:
            bottom = min(bottom, self.height())
        # 5. Room for the floating bar below. height <= window_height - y -
        # BAR_ROOM is, since height is always bottom - top, the same bound
        # as bottom <= window_height - BAR_ROOM regardless of y -- but only
        # tightens the bottom edge when doing so wouldn't undercut the
        # minimum height step 1 already established (the ticket's
        # deliberate reordering of the spec's clamps).
        if free_bottom:
            bar_limit = self.height() - self._BAR_ROOM
            if bar_limit >= top + metric.SEL_MIN_H:
                bottom = min(bottom, bar_limit)

        self.set_selection(
            QRect(round(left), round(top), round(right - left), round(bottom - top))
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # Layer 1: the frozen frame, full window. This is also what keeps
        # the selection "undimmed and at 1:1": the scrim below punches a
        # hole out of this exact drawImage call rather than compositing a
        # second one, so the hole shows precisely these pixels, untouched.
        painter.drawImage(QRectF(self.rect()), self._frame.image)
        self._paint_scrim(painter)
        if self._selection is not None:
            # Ink before the stroke/handles, matching the design's layer
            # order within the selection: "undimmed pixmap, ink layer,
            # frame stroke, handles, chips".
            self._paint_marks(painter)
            # Smooths the dashed diagonal-adjacent stroke and the rounded
            # bracket/handle corners; the scrim above is a flat axis-aligned
            # fill and doesn't need it.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_selection_stroke(painter)
            self._paint_corner_brackets(painter)
            self._paint_edge_handles(painter)
        painter.end()

    def _paint_marks(self, painter: QPainter) -> None:
        """The ink layer: `_marks`, clipped to the selection.

        Marks are stored in this widget's own window coordinates -- the
        same space `_selection` lives in (see the class docstring) -- so a
        re-frame never touches a mark's own points, only where the clip
        rect sits over them. A mark whose points fall outside the clip
        simply isn't painted this frame; it stays in `_marks` and reappears
        the instant the selection grows back over it. Per
        docs/design/overlay-redesign.md's "Ink lives in screen
        coordinates": "in Qt just `painter.setClipRect(sel)` before drawing
        marks."
        """
        if not self._marks:
            return
        painter.save()
        painter.setClipRect(QRectF(self._selection))
        step_counter = 0
        for shape in self._marks:
            if isinstance(shape, StepMarker):
                step_counter += 1
                shape.number = step_counter
            if isinstance(shape, ObscuringShape):
                # Obscuring marks sample the frozen frame's own pixels
                # rather than paint onto a painter (see
                # shapes.ObscuringShape.draw()) -- compositing those live
                # against a resizable selection is a later ticket's
                # concern, same as the tool that would create them.
                # render_selection() (export) still handles them correctly,
                # via render()'s own special-casing.
                continue
            shape.draw(painter)
        painter.restore()

    def _paint_scrim(self, painter: QPainter) -> None:
        """Layer 2: dim everything outside the selection.

        Painted here, in this widget's own paintEvent, rather than as a
        translucent child widget stacked over the whole window -- per the
        spec, a full-window child would sit above the (future) ink layer in
        z-order and eat its mouse events. A single even-odd fill dims the
        window and punches the selection out in one call, so there's no
        separate "dim then punch a hole" step that could disagree with this
        one at the selection's edge.
        """
        widget_rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRect(widget_rect)
        if self._selection is not None:
            local_selection = QRectF(self._selection).intersected(widget_rect)
            if not local_selection.isEmpty():
                path.addRect(local_selection)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        # design.color() resolves tokens.Color.DIM + DIM_ALPHA together, per
        # its own docstring's "a colour and its alpha are never applied
        # separately" -- the literal is not re-typed here.
        painter.fillPath(path, design.color("DIM"))

    # -- selection frame: stroke -----------------------------------------

    def _stroke_pens(self) -> tuple[QPen, QPen]:
        """The two coincident 1px pens the selection rect is stroked with:
        a plain solid one underneath an animated dashed one.

        Per the README: "two coincident strokes... which reads as motion
        without either stroke dominating." Split out from the paint call so
        the dash pattern and colours -- the acceptance criterion's "using
        the dash pattern from tokens.py" -- can be asserted on directly
        instead of only inferred from rendered pixels.
        """
        metric = design.tokens.Metric
        solid = QPen(design.color("SEL_STROKE"))
        solid.setWidthF(metric.SEL_STROKE_W)

        dashed = QPen(design.color("SEL_ANTS"))
        dashed.setWidthF(metric.SEL_STROKE_W)
        dashed.setDashPattern(list(metric.ANTS_DASH))
        dashed.setDashOffset(self._dash_offset)
        return solid, dashed

    def _paint_selection_stroke(self, painter: QPainter) -> None:
        # Inset half a pixel, like the reference's `x=0.5 y=0.5` rect, so a
        # 1px pen lands on a crisp pixel line instead of straddling two.
        rect = QRectF(self._selection).adjusted(0.5, 0.5, -0.5, -0.5)
        solid, dashed = self._stroke_pens()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(solid)
        painter.drawRect(rect)
        painter.setPen(dashed)
        painter.drawRect(rect)

    # -- selection frame: corner brackets / handles ------------------------

    def _bracket_path(self, handle: Handle) -> QPainterPath:
        """The filled L-bracket for one corner, in this widget's local
        (window) coordinates.

        Built by mirroring the canonical top-left bracket from
        `_l_bracket_local_path` with a `QTransform`, rather than re-deriving
        each corner's outline by hand -- the four corners are the same
        shape reflected across one or both axes and anchored to that
        corner's point on the selection rect, offset outward by
        `_CORNER_BRACKET_OFFSET` so the bracket straddles the 1px stroke.
        """
        # QRectF, not the raw QRect `self._selection` -- QRect.right()/
        # bottom() are inclusive (left + width - 1), which would throw every
        # offset below off by one pixel against the QRectF math everywhere
        # else in this class.
        sel = QRectF(self._selection)
        metric = design.tokens.Metric
        local = _l_bracket_local_path(
            metric.CORNER_LEN, metric.CORNER_W, self._CORNER_BRACKET_RADIUS
        )

        flip_x = handle in (Handle.TOP_RIGHT, Handle.BOTTOM_RIGHT)
        flip_y = handle in (Handle.BOTTOM_LEFT, Handle.BOTTOM_RIGHT)
        off = self._CORNER_BRACKET_OFFSET
        anchor_x = sel.right() + off if flip_x else sel.left() - off
        anchor_y = sel.bottom() + off if flip_y else sel.top() - off

        transform = QTransform()
        transform.translate(anchor_x, anchor_y)
        transform.scale(-1 if flip_x else 1, -1 if flip_y else 1)
        return transform.map(local)

    def _edge_handle_rect(self, handle: Handle) -> QRectF:
        """The rounded bar for one edge handle, in local (window)
        coordinates: `HANDLE_LONG x HANDLE_SHORT` (or transposed for a
        vertical edge), centred on the edge and offset outward by
        `_EDGE_HANDLE_OFFSET` so it overhangs the stroke.
        """
        sel = QRectF(self._selection)  # see the QRectF note in _bracket_path
        metric = design.tokens.Metric
        long_, short = metric.HANDLE_LONG, metric.HANDLE_SHORT
        off = self._EDGE_HANDLE_OFFSET
        center = sel.center()

        if handle is Handle.TOP:
            return QRectF(center.x() - long_ / 2, sel.top() - off, long_, short)
        if handle is Handle.BOTTOM:
            return QRectF(center.x() - long_ / 2, sel.bottom() - (short - off), long_, short)
        if handle is Handle.LEFT:
            return QRectF(sel.left() - off, center.y() - long_ / 2, short, long_)
        if handle is Handle.RIGHT:
            return QRectF(sel.right() - (short - off), center.y() - long_ / 2, short, long_)
        raise ValueError(f"not an edge handle: {handle!r}")

    def _corner_hit_rect(self, handle: Handle) -> QRectF:
        """The invisible `HANDLE_HIT`-square hit target for one corner, in
        local (window) coordinates -- offset outward by
        `_CORNER_HIT_OFFSET` and centred on the corner point, same as the
        bracket it sits under. No chrome of its own: the bracket is the
        visible handle, per the README.
        """
        sel = QRectF(self._selection)  # see the QRectF note in _bracket_path
        size = design.tokens.Metric.HANDLE_HIT
        off = self._CORNER_HIT_OFFSET

        if handle is Handle.TOP_LEFT:
            return QRectF(sel.left() - off, sel.top() - off, size, size)
        if handle is Handle.TOP_RIGHT:
            return QRectF(sel.right() - size + off, sel.top() - off, size, size)
        if handle is Handle.BOTTOM_LEFT:
            return QRectF(sel.left() - off, sel.bottom() - size + off, size, size)
        if handle is Handle.BOTTOM_RIGHT:
            return QRectF(sel.right() - size + off, sel.bottom() - size + off, size, size)
        raise ValueError(f"not a corner handle: {handle!r}")

    def _handle_at(self, pos: QPointF) -> Handle | None:
        """Which handle (if any) a local-coordinate point falls in.

        Corners checked first: a very small selection could bring a corner
        hit target and an edge handle's rect into overlap, and the corner
        bracket is the more specific, visually on-top target of the two.
        """
        if self._selection is None:
            return None
        for handle in _CORNER_HANDLES:
            if self._corner_hit_rect(handle).contains(pos):
                return handle
        for handle in _EDGE_HANDLES:
            if self._edge_handle_rect(handle).contains(pos):
                return handle
        return None

    def _paint_corner_brackets(self, painter: QPainter) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("HANDLE"))
        for handle in _CORNER_HANDLES:
            painter.drawPath(self._bracket_path(handle))

    def _paint_edge_handles(self, painter: QPainter) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(design.color("HANDLE"))
        for handle in _EDGE_HANDLES:
            rect = self._edge_handle_rect(handle)
            painter.drawRoundedRect(rect, self._EDGE_HANDLE_RADIUS, self._EDGE_HANDLE_RADIUS)


def create_overlays(
    frame: Frame,
    monitor_geometries: list[QRectF],
    mode: SelectionMode = SelectionMode.RECTANGLE,
    geometry_provider: GeometryProvider | None = None,
) -> list[Overlay]:
    """Build one `Overlay` per monitor geometry.

    Geometries are absolute logical virtual-desktop rects, the same space
    `Frame` uses. Does not touch `QApplication.screens()` or show any
    window — the caller is responsible for sourcing real geometries and
    showing the windows, which keeps this module testable with synthetic
    geometries offscreen.

    A single `Overlay` only knows its own monitor's geometry, but "full
    screen" means the union of every monitor, so that union is computed once
    here and handed to each `Overlay` as `virtual_desktop_rect`.
    """
    virtual_desktop_rect = None
    for geometry in monitor_geometries:
        virtual_desktop_rect = (
            geometry if virtual_desktop_rect is None else virtual_desktop_rect.united(geometry)
        )
    return [
        Overlay(
            frame,
            geometry,
            mode=mode,
            geometry_provider=geometry_provider,
            virtual_desktop_rect=virtual_desktop_rect,
        )
        for geometry in monitor_geometries
    ]
